#!/usr/bin/env bash
# ==============================================================================
# experiments/run_experiments.sh
# End-to-End Automated Experiment Runner (E1 to E6) — Role 1 + Role 2 + Role 3
# ==============================================================================
# Executes the complete benchmark suite defined in the Project Guide:
#   - E1: Low traffic (no background load), Priority OFF
#   - E2: Low traffic (no background load), Priority ON
#   - E3: High congestion (9 Mbps UDP load), Priority OFF (Baseline)
#   - E4: High congestion (9 Mbps UDP load), Static Priority ON (DSCP 46)
#   - E5: High congestion (9 Mbps UDP load), Dynamic SDN Control ON
#   - E6: High congestion + Primary Link Failure & Automatic Failover
#
# Usage:
#   sudo bash experiments/run_experiments.sh [ROUNDS] [INTERVAL]
# Example:
#   sudo bash experiments/run_experiments.sh 40 0.2
# ==============================================================================

set -euo pipefail

ROUNDS="${1:-40}"
INTERVAL="${2:-0.2}"
CLIENT_COUNTS=(3 5 10)

PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJ_ROOT}"

mkdir -p results

echo "============================================================"
echo " SDN Network Time Synchronization — Full Experiment Suite"
echo " Rounds per run: ${ROUNDS} | Interval: ${INTERVAL}s"
echo "============================================================"

# Clean up any leftover Mininet or controller state
sudo mn -c >/dev/null 2>&1 || true
pkill -9 -f ryu-manager >/dev/null 2>&1 || true
pkill -9 -f timesync_core.py >/dev/null 2>&1 || true
pkill -9 -f loadgen.py >/dev/null 2>&1 || true
sleep 1

# Activate Ryu virtualenv if present
if [ -f "${HOME}/ryu-venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${HOME}/ryu-venv/bin/activate"
fi

# Start Ryu SDN Controller in background
echo "[*] Starting Ryu SDN Controller (OpenFlow 1.3 + REST API on :8080)..."
ryu-manager controller/sync_controller.py > results/ryu_experiments.log 2>&1 &
RYU_PID=$!

cleanup() {
    echo "[*] Cleaning up controller (PID ${RYU_PID}) and Mininet..."
    kill -9 "${RYU_PID}" >/dev/null 2>&1 || true
    sudo mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Wait for REST API on port 8080
for _ in $(seq 1 20); do
    if curl -s http://127.0.0.1:8080/sdn/status >/dev/null 2>&1; then
        echo "[OK] Ryu Controller REST API is online."
        break
    fi
    sleep 0.5
done

set_controller_mode() {
    local prio="$1"
    local dyn="$2"
    curl -s -X POST http://127.0.0.1:8080/sdn/mode \
        -H "Content-Type: application/json" \
        -d "{\"priority\": \"${prio}\", \"dynamic\": \"${dyn}\"}" >/dev/null
    echo "    -> Controller mode set: priority=${prio}, dynamic=${dyn}"
}

for N in "${CLIENT_COUNTS[@]}"; do
    echo ""
    echo "************************************************************"
    echo " Running Experiments E1..E6 for N=${N} Clients"
    echo "************************************************************"

    # E1: Low traffic, Priority OFF
    echo "[E1] Low Traffic | Priority OFF | Clients=${N}"
    set_controller_mode "off" "off"
    sudo python3 topology/topo.py --scripted --no-load --clients "${N}" \
        --dscp 0 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E1_${N}clients"

    # E2: Low traffic, Priority ON
    echo "[E2] Low Traffic | Priority ON | Clients=${N}"
    set_controller_mode "on" "off"
    sudo python3 topology/topo.py --scripted --no-load --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E2_${N}clients"

    # E3: High traffic (congested), Priority OFF
    echo "[E3] High Congestion | Priority OFF (Baseline) | Clients=${N}"
    set_controller_mode "off" "off"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 0 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E3_${N}clients"

    # E4: High traffic (congested), Static Priority ON
    echo "[E4] High Congestion | Static Priority ON | Clients=${N}"
    set_controller_mode "on" "off"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E4_${N}clients"

    # E5: High traffic (congested), Dynamic SDN Control ON
    echo "[E5] High Congestion | Dynamic SDN Control ON | Clients=${N}"
    set_controller_mode "on" "on"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E5_${N}clients"

    # E6: Challenging Scenario (Congestion + Primary Link Failure & Failover)
    echo "[E6] Challenge: High Congestion + Primary Link Failure | Clients=${N}"
    set_controller_mode "on" "on"
    sudo python3 topology/topo.py --scripted --failover --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E6_${N}clients"
done

echo ""
echo "============================================================"
echo " Running Post-Experiment Analysis & Graph Generation"
echo "============================================================"
python3 experiments/analyze.py --results-dir results/

echo "[OK] All experiments E1-E6 and graphs completed in results/"
