#!/usr/bin/env bash
# Configures Open vSwitch HTB queues on the bottleneck ports.
# Sets up high priority (Q1) for time sync and lower priority (Q2/Q3) for background traffic.
# Usage: sudo ./scripts/setup_queues.sh [BW_MBPS] [S1_PORT] [S2_PORT]

set -euo pipefail

# default parameters
BW="${1:-10}"            # 10 Mbps default
PORT_S1="${2:-s1-eth1}"  # s1 egress port
PORT_S2="${3:-s2-eth1}"  # s2 egress port

echo "Configuring Open vSwitch HTB Queues"
echo "Bottleneck Bandwidth: ${BW} Mbit/s"
echo "Ports: ${PORT_S1} (s1 egress) and ${PORT_S2} (s2 egress)"

# calculate queue bandwidth rates in bps
MAX_RATE=$((BW * 1000 * 1000))
Q1_MIN_RATE=$((BW * 800 * 1000))       # 80%
Q1_MAX_RATE=${MAX_RATE}                 # 100%
Q2_MIN_RATE=$((BW * 200 * 1000))       # 20%
Q2_MAX_RATE=${MAX_RATE}                 # 100%
Q3_MIN_RATE=500000                     # 500 Kbps
Q3_MAX_RATE=$((BW * 200 * 1000))       # 20%

# configure HTB qos on an interface
configure_port_qos() {
    local PORT="$1"
    echo "[*] Configuring QoS on interface ${PORT}..."

    # check if port exists in ovs
    if ! ovs-vsctl list port "${PORT}" >/dev/null 2>&1; then
        echo "    [WARN] Port ${PORT} does not exist in OVS. Skipping..."
        return 0
    fi

    # clear existing qos on this port
    ovs-vsctl --if-exists clear Port "${PORT}" qos

    # create qos and queues 0-3 using linux-htb
    ovs-vsctl -- set Port "${PORT}" qos=@newqos \
        -- --id=@newqos create QoS type=linux-htb \
           other-config:max-rate="${MAX_RATE}" \
           queues:0=@q0 queues:1=@q1 queues:2=@q2 queues:3=@q3 \
        -- --id=@q0 create Queue other-config:min-rate=1000000 other-config:max-rate="${MAX_RATE}" \
        -- --id=@q1 create Queue other-config:min-rate="${Q1_MIN_RATE}" other-config:max-rate="${Q1_MAX_RATE}" \
        -- --id=@q2 create Queue other-config:min-rate="${Q2_MIN_RATE}" other-config:max-rate="${Q2_MAX_RATE}" \
        -- --id=@q3 create Queue other-config:min-rate="${Q3_MIN_RATE}" other-config:max-rate="${Q3_MAX_RATE}"

    echo "    [OK] QoS profile successfully bound to ${PORT}"
}

# apply qos on s1 and s2 ports
configure_port_qos "${PORT_S1}"
configure_port_qos "${PORT_S2}"

echo ""
echo "Queue Setup Summary & Verification Commands:"
echo "To verify OVS QoS records:"
echo "  sudo ovs-vsctl list qos"
echo "  sudo ovs-vsctl list queue"
echo ""
echo "To verify Linux kernel tc classes on the port:"
echo "  tc -s qdisc show dev ${PORT_S1}"
echo "  tc -s class show dev ${PORT_S1}"
echo ""
echo "To map flows to queues in OpenFlow 1.3:"
echo "  actions=set_queue:1,output:<egress_port>"
