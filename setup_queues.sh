#!/usr/bin/env bash
# ==============================================================================
# scripts/setup_queues.sh
# Open vSwitch Hierarchical Token Bucket (HTB) Queue Configuration
# Network Time Synchronization Under Congestion
#
# Target Platform: Ubuntu 22.04 LTS, Open vSwitch 2.17+, Mininet 2.3.0
# ==============================================================================
#
# CRITICAL NETWORKING ARCHITECTURE NOTES:
#
# 1. Why Bottleneck & Queues Exist:
#    In a LAN or high-speed fabric with ample bandwidth, sync packets (NTP/PTP)
#    experience virtually zero queuing delay (sub-millisecond jitter). Under heavy
#    cross-traffic (e.g., massive file transfers, UDP floods), packets get queued
#    behind large bulk frames (bufferbloat), introducing severe queueing delay jitter
#    that destroys clock synchronization accuracy.
#    By constraining the inter-switch bottleneck and creating prioritized queues,
#    we can isolate latency-critical time-sync packets from background bursts.
#
# 2. Why Queues Act on EGRESS Only:
#    In Linux networking and Open vSwitch tc architecture, queuing disciplines (qdisc)
#    and packet scheduling (HTB, CBQ, HFSC) only govern packet transmission leaving
#    an interface (EGRESS). Ingress traffic cannot be buffered and scheduled in the
#    same way without dropping or redirecting to an Intermediate Functional Block (IFB).
#    Therefore:
#    - Traffic flowing s1 -> s2 queues on switch s1's egress port towards s2 (s1-eth1).
#    - Traffic flowing s2 -> s1 queues on switch s2's egress port towards s1 (s2-eth1).
#    To ensure bidirectional protection (sync requests AND sync responses), both
#    egress interfaces MUST be configured with matching QoS profiles!
#
# 3. How to Identify Mininet Port Names:
#    In Mininet with standard Open vSwitch naming:
#    Switch ports follow the pattern: <switch_name>-eth<port_number>
#    To verify existing ports on switch s1:
#        sudo ovs-vsctl list-ports s1
#    To inspect OpenFlow port numbers corresponding to interfaces:
#        sudo ovs-ofctl show s1 -O OpenFlow13
#
# 4. Queue Definitions & Bandwidth Allocations:
#    Bottleneck bandwidth $BW (default 10 Mbit/s):
#    - Queue 0: Default egress queue (fallback)
#    - Queue 1: High Priority (Time Sync / DSCP 46 / EF):
#               min-rate = 80% of BW (8 Mbit/s), max-rate = 100% of BW (10 Mbit/s)
#               Ensures dedicated guaranteed bandwidth and minimum latency.
#    - Queue 2: Default Background Traffic:
#               min-rate = 20% of BW (2 Mbit/s), max-rate = 100% of BW (10 Mbit/s)
#    - Queue 3: Throttled / Scavenger Background:
#               min-rate = 500 Kbit/s, max-rate = 20% of BW (2 Mbit/s)
#
# Usage:
#   sudo ./scripts/setup_queues.sh [BOTTLENECK_BW_MBPS] [S1_PORT] [S2_PORT]
# Example:
#   sudo ./scripts/setup_queues.sh 10 s1-eth1 s2-eth1
# ==============================================================================

set -euo pipefail

# Parameters
BW="${1:-10}"            # Default: 10 Mbit/s
PORT_S1="${2:-s1-eth1}"  # Default s1 primary egress port towards s2
PORT_S2="${3:-s2-eth1}"  # Default s2 primary egress port towards s1

echo "============================================================"
echo " Configuring Open vSwitch HTB Queues"
echo " Bottleneck Bandwidth: ${BW} Mbit/s"
echo " Ports: ${PORT_S1} (s1 egress) and ${PORT_S2} (s2 egress)"
echo "============================================================"

# Calculate bandwidth limits in bits per second
MAX_RATE=$((BW * 1000 * 1000))
Q1_MIN_RATE=$((BW * 800 * 1000))       # 80% (8 Mbit/s for BW=10)
Q1_MAX_RATE=${MAX_RATE}                 # 100%
Q2_MIN_RATE=$((BW * 200 * 1000))       # 20% (2 Mbit/s for BW=10)
Q2_MAX_RATE=${MAX_RATE}                 # 100%
Q3_MIN_RATE=500000                     # 500 Kbit/s
Q3_MAX_RATE=$((BW * 200 * 1000))       # 2 Mbit/s

configure_port_qos() {
    local PORT="$1"
    echo "[*] Configuring QoS on interface ${PORT}..."

    # Idempotent cleanup: Check if port exists in OVS
    if ! ovs-vsctl list port "${PORT}" >/dev/null 2>&1; then
        echo "    [WARN] Port ${PORT} does not exist in OVS. Skipping..."
        return 0
    fi

    # Clear any existing QoS table associated with this port
    ovs-vsctl --if-exists clear Port "${PORT}" qos

    # Build the atomic QoS and Queue configuration transaction
    # Uses linux-htb type
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

# Configure queues on s1 egress towards s2
configure_port_qos "${PORT_S1}"

# Configure queues on s2 egress towards s1
configure_port_qos "${PORT_S2}"

echo ""
echo "============================================================"
echo " Queue Setup Summary & Verification Commands"
echo "============================================================"
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
echo "============================================================"
