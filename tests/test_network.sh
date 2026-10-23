#!/bin/bash
# Network diagnostic script for the SDN time sync project.
# Checks controller, ovs, tc queues, and runs python network tests.
# Usage: sudo bash tests/test_network.sh [SWITCH_NAME] [BOTTLENECK_PORT]

set -o errexit
set -o nounset
set -o pipefail

# colors for terminal output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SWITCH="${1:-s1}"
BOTTLENECK_PORT="${2:-s1-eth1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_TEST_SCRIPT="${PROJECT_ROOT}/tests/test_network.py"

echo -e "${BOLD}${CYAN}SDN Network Time Sync Diagnostics${NC}"
echo -e "Target Switch   : ${BOLD}${SWITCH}${NC}"
echo -e "Bottleneck Port : ${BOLD}${BOTTLENECK_PORT}${NC}"
echo -e "Project Root    : ${PROJECT_ROOT}"
echo -e "Date & Time     : $(date)\n"

# check root privileges
echo -e "${BOLD}[STEP 1/5] Verifying Execution Privileges...${NC}"
if [ "${EUID}" -ne 0 ]; then
    echo -e "${RED}[FAIL] This script requires administrative (root) privileges.${NC}"
    echo -e "Reason: OVS inspection, network namespaces, tc qdisc, and tcpdump require root."
    echo -e "Please re-run using: ${YELLOW}sudo bash $0${NC}\n"
    exit 1
fi
echo -e "${GREEN}[PASS] Running with superuser (root) privileges.${NC}\n"

# check ryu and ovs status
echo -e "${BOLD}[STEP 2/5] Checking Controller and Mininet Process Status...${NC}"

# check for ryu manager
if pgrep -f "ryu-manager" > /dev/null 2>&1; then
    RYU_PID=$(pgrep -f "ryu-manager" | head -n 1)
    echo -e "${GREEN}[PASS] Ryu SDN Controller active (PID: ${RYU_PID}).${NC}"
else
    echo -e "${YELLOW}[WARN] Ryu controller process ('ryu-manager') not detected in process table.${NC}"
    echo -e "       Ensure the Ryu controller is running: ryu-manager ryu.app.ofctl_rest <app_name>"
fi

# check for open vswitch daemon
if pgrep -f "ovs-vswitchd" > /dev/null 2>&1; then
    echo -e "${GREEN}[PASS] Open vSwitch daemon (ovs-vswitchd) is active.${NC}"
else
    echo -e "${RED}[FAIL] ovs-vswitchd is NOT running. Start with: sudo systemctl start openvswitch-switch${NC}"
    exit 1
fi

# check if switch bridge exists
if ovs-vsctl br-exists "${SWITCH}" 2>/dev/null; then
    echo -e "${GREEN}[PASS] Switch bridge '${SWITCH}' exists in OVS database.${NC}"
else
    echo -e "${YELLOW}[WARN] Switch bridge '${SWITCH}' does not currently exist.${NC}"
    echo -e "       Ensure Mininet topology is active: sudo python3 topology/mesh_topo.py"
fi
echo ""

# dump ovs and tc stats
echo -e "${BOLD}[STEP 3/5] Executing Standalone OVS & TC Diagnostics...${NC}"

echo -e "${BLUE}--- Diagnostic A: ovs-vsctl show ---${NC}"
ovs-vsctl show || echo -e "${YELLOW}ovs-vsctl show returned error${NC}"
echo ""

echo -e "${BLUE}--- Diagnostic B: ovs-ofctl dump-flows ${SWITCH} -O OpenFlow13 ---${NC}"
if ovs-vsctl br-exists "${SWITCH}" 2>/dev/null; then
    ovs-ofctl dump-flows "${SWITCH}" -O OpenFlow13 || true
else
    echo -e "${YELLOW}Skipping flow dump: bridge '${SWITCH}' not active.${NC}"
fi
echo ""

echo -e "${BLUE}--- Diagnostic C: tc -s class show dev ${BOTTLENECK_PORT} ---${NC}"
if ip link show "${BOTTLENECK_PORT}" > /dev/null 2>&1; then
    tc -s class show dev "${BOTTLENECK_PORT}" || true
else
    echo -e "${YELLOW}Interface '${BOTTLENECK_PORT}' not present. Available interfaces:${NC}"
    ip -br link show
fi
echo ""

# test dscp 46 packet capture
echo -e "${BOLD}[STEP 4/5] Testing DSCP 46 (TOS 0xb8) Wire Capture...${NC}"
if ip link show "${BOTTLENECK_PORT}" > /dev/null 2>&1; then
    PCAP_FILE="/tmp/test_dscp_capture.pcap"
    rm -f "${PCAP_FILE}"

    echo -e "Starting background tcpdump on ${BOTTLENECK_PORT} (filter: ip[1] == 0xb8)..."
    tcpdump -i "${BOTTLENECK_PORT}" -c 1 -w "${PCAP_FILE}" "ip[1] == 0xb8 or ip[1] & 0xfc == 184" -nn > /dev/null 2>&1 &
    TCPDUMP_PID=$!

    sleep 1

    # try pinging from h1 if namespace exists
    if pgrep -f "mininet:h1" > /dev/null 2>&1; then
        H1_PID=$(pgrep -f "mininet:h1" | head -n 1)
        echo "Injecting test packet with TOS 184 from h1 namespace..."
        mnexec -a "${H1_PID}" ping -c 1 -Q 184 10.0.0.100 > /dev/null 2>&1 || true
    fi

    # wait for tcpdump
    sleep 2
    kill "${TCPDUMP_PID}" 2>/dev/null || true

    if [ -s "${PCAP_FILE}" ]; then
        echo -e "${GREEN}[PASS] DSCP 46 (TOS 0xb8) packet successfully captured on ${BOTTLENECK_PORT}.${NC}"
        tcpdump -r "${PCAP_FILE}" -v -n 2>/dev/null | head -n 5
    else
        echo -e "${YELLOW}[NOTE] No DSCP 46 packets captured during standalone window (will test during Python suite).${NC}"
    fi
    rm -f "${PCAP_FILE}"
else
    echo -e "${YELLOW}Skipping standalone tcpdump: interface '${BOTTLENECK_PORT}' not found.${NC}"
fi
echo ""

# run python tests
echo -e "${BOLD}[STEP 5/5] Launching Comprehensive Python Test Suite (tests/test_network.py)...${NC}"
if [ -f "${PYTHON_TEST_SCRIPT}" ]; then
    python3 "${PYTHON_TEST_SCRIPT}" --switch "${SWITCH}" --interface "${BOTTLENECK_PORT}"
    TEST_EXIT_CODE=$?
else
    echo -e "${RED}[FAIL] Could not locate test script: ${PYTHON_TEST_SCRIPT}${NC}"
    TEST_EXIT_CODE=1
fi

echo ""
if [ "${TEST_EXIT_CODE}" -eq 0 ]; then
    echo -e "${BOLD}${GREEN}   ALL NETWORK DIAGNOSTIC TESTS PASSED SUCCESSFULLY!${NC}"
else
    echo -e "${BOLD}${RED}   NETWORK TESTS COMPLETED WITH FAILURES (EXIT CODE: ${TEST_EXIT_CODE})${NC}"
fi
echo ""

exit "${TEST_EXIT_CODE}"
