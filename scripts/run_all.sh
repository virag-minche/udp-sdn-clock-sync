#!/usr/bin/env bash
# Main script to run the SDN controller and Mininet topology.
# Starts Ryu on port 6653, waits for it, and then launches the topology.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python3"
VENV_RYU="${PROJECT_DIR}/venv/bin/ryu-manager"

# use venv if present, otherwise system commands
if [[ ! -x "$VENV_PYTHON" ]]; then
    VENV_PYTHON="python3"
fi
if [[ ! -x "$VENV_RYU" ]]; then
    VENV_RYU="ryu-manager"
fi

CONTROLLER_APP="${PROJECT_DIR}/src/controller.py"
# fallback to simple switch if controller script is missing
if [[ ! -f "$CONTROLLER_APP" ]]; then
    CONTROLLER_APP="ryu.app.simple_switch_13"
fi

TOPO_SCRIPT="${PROJECT_DIR}/topology/topo.py"
RYU_LOG="${PROJECT_DIR}/ryu.log"
RYU_PID=""

# kill controller and clean mininet on exit
cleanup() {
    echo ""
    echo "[*] Cleaning up Mininet and Controller processes..."
    if [[ -n "$RYU_PID" ]] && kill -0 "$RYU_PID" 2>/dev/null; then
        echo "    Stopping Ryu controller (PID: $RYU_PID)..."
        kill "$RYU_PID" 2>/dev/null || true
    fi
    pkill -f "ryu-manager" 2>/dev/null || true
    sudo mn -c >/dev/null 2>&1 || true
    echo "[*] Cleanup complete."
}

trap cleanup EXIT INT TERM

echo "SDN Network Time Synchronization Runner"
echo "Project Directory: ${PROJECT_DIR}"

# clean up any old runs
echo "[1/4] Cleaning previous Mininet topologies and controllers..."
sudo mn -c >/dev/null 2>&1 || true
pkill -f "ryu-manager" 2>/dev/null || true
pkill -f "iperf3" 2>/dev/null || true
pkill -f "timesync_core.py" 2>/dev/null || true

# start ryu controller
echo "[2/4] Starting Ryu OpenFlow 1.3 Controller with ${CONTROLLER_APP}..."
$VENV_RYU --ofp-tcp-listen-port 6653 "$CONTROLLER_APP" > "$RYU_LOG" 2>&1 &
RYU_PID=$!
echo "    Controller started with PID: $RYU_PID (Logging to ${RYU_LOG})"

# wait until ryu is listening on 6653
echo "[3/4] Waiting for Ryu controller to bind to port 6653..."
READY=0
for i in {1..30}; do
    if netstat -tuln 2>/dev/null | grep -q ":6653 "; then
        READY=1
        break
    elif ss -tuln 2>/dev/null | grep -q ":6653 "; then
        READY=1
        break
    fi
    sleep 0.5
done

if [[ $READY -eq 0 ]]; then
    echo "[ERROR] Ryu controller failed to start or bind to port 6653 in 15 seconds." >&2
    echo "Controller log contents:"
    cat "$RYU_LOG" || true
    exit 1
fi
echo "    [OK] Ryu controller is ready on 127.0.0.1:6653."

# launch mininet
echo "[4/4] Launching Mininet Topology..."
sudo "$VENV_PYTHON" "$TOPO_SCRIPT" "$@"
