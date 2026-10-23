#!/usr/bin/env bash
# Runs all benchmark experiments (E1 to E6) across different client counts.
# Launches the Ryu controller, runs topology tests with varying loads, and plots results.
# Usage: sudo bash experiments/run_experiments.sh [ROUNDS] [INTERVAL]

set -euo pipefail

ROUNDS="${1:-40}"
INTERVAL="${2:-0.2}"
CLIENT_COUNTS=(3 5 10)

PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJ_ROOT}"

mkdir -p results

echo "SDN Network Time Synchronization — Full Experiment Suite"
echo "Rounds per run: ${ROUNDS} | Interval: ${INTERVAL}s"

# clean up old state
sudo mn -c >/dev/null 2>&1 || true
pkill -9 -f ryu-manager >/dev/null 2>&1 || true
pkill -9 -f timesync_core.py >/dev/null 2>&1 || true
pkill -9 -f loadgen.py >/dev/null 2>&1 || true
sleep 1

# activate ryu venv if present
if [ -f "${HOME}/ryu-venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${HOME}/ryu-venv/bin/activate"
fi

# start ryu controller in background
echo "[*] Starting Ryu SDN Controller (OpenFlow 1.3 + REST API on :8080)..."
ryu-manager controller/sync_controller.py > results/ryu_experiments.log 2>&1 &
RYU_PID=$!

# cleanup function on exit
cleanup() {
    echo "[*] Cleaning up controller (PID ${RYU_PID}) and Mininet..."
    kill -9 "${RYU_PID}" >/dev/null 2>&1 || true
    sudo mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

# wait for controller REST API
for _ in $(seq 1 20); do
    if curl -s http://127.0.0.1:8080/sdn/status >/dev/null 2>&1; then
        echo "[OK] Ryu Controller REST API is online."
        break
    fi
    sleep 0.5
done

# helper to set controller mode via REST
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
    echo "Running Experiments E1..E6 for N=${N} Clients"

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

    # E3: High traffic, Priority OFF (baseline)
    echo "[E3] High Congestion | Priority OFF (Baseline) | Clients=${N}"
    set_controller_mode "off" "off"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 0 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E3_${N}clients"

    # E4: High traffic, Static Priority ON
    echo "[E4] High Congestion | Static Priority ON | Clients=${N}"
    set_controller_mode "on" "off"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E4_${N}clients"

    # E5: High traffic, Dynamic SDN Control ON
    echo "[E5] High Congestion | Dynamic SDN Control ON | Clients=${N}"
    set_controller_mode "on" "on"
    sudo python3 topology/topo.py --scripted --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E5_${N}clients"

    # E6: High traffic + Link Failover
    echo "[E6] Challenge: High Congestion + Primary Link Failure | Clients=${N}"
    set_controller_mode "on" "on"
    sudo python3 topology/topo.py --scripted --failover --clients "${N}" \
        --dscp 46 --rounds "${ROUNDS}" --interval "${INTERVAL}" \
        --results-dir "results/E6_${N}clients"
done

echo ""
echo "Running Post-Experiment Analysis & Graph Generation..."
python3 experiments/analyze.py --results-dir results/

echo "[OK] All experiments E1-E6 and graphs completed in results/"
