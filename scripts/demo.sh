#!/usr/bin/env bash
# demo.sh - Runs the full automated demo (E1-E4 experiments + analysis).
# Run this in Terminal 2 AFTER Terminal 1 has the Ryu controller running.
# Usage: bash scripts/demo.sh

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

VENV_PYTHON="${PROJECT_DIR}/venv/bin/python3"
ROUNDS=40
INTERVAL=0.2
CLIENTS=3

echo "================================================"
echo "  SDN Clock Sync - Full Demo Runner"
echo "================================================"
echo ""

# Check Ryu controller is running
echo "[*] Checking Ryu controller is up on port 6653..."
for i in $(seq 1 10); do
    if ss -tuln 2>/dev/null | grep -q ":6653 " || \
       netstat -tuln 2>/dev/null | grep -q ":6653 "; then
        echo "    [OK] Ryu controller is running."
        break
    fi
    if [ "$i" -eq 10 ]; then
        echo ""
        echo "[ERROR] Ryu controller not found on port 6653!"
        echo "        Please start it in Terminal 1 first:"
        echo "        source venv/bin/activate"
        echo "        ryu-manager controller/sync_controller.py --ofp-tcp-listen-port 6653"
        exit 1
    fi
    sleep 1
done

mkdir -p results

# Helper to set controller mode
set_mode() {
    curl -s -X POST http://127.0.0.1:8080/sdn/mode \
        -H "Content-Type: application/json" \
        -d "{\"priority\": \"$1\", \"dynamic\": \"$2\"}" > /dev/null 2>&1 || true
    echo "    -> Controller: priority=$1, dynamic=$2"
}

# Clean old Mininet state
echo ""
echo "[*] Cleaning old Mininet state..."
sudo mn -c > /dev/null 2>&1 || true

# -----------------------------------------------
echo ""
echo "================================================"
echo " EXPERIMENT E1: Low Traffic, No Priority"
echo "================================================"
set_mode "off" "off"
sudo "$VENV_PYTHON" topology/topo.py \
    --scripted --no-load --clients "$CLIENTS" \
    --dscp 0 --rounds "$ROUNDS" --interval "$INTERVAL" \
    --results-dir results/E1

# -----------------------------------------------
echo ""
echo "================================================"
echo " EXPERIMENT E2: Low Traffic, Priority ON"
echo "================================================"
set_mode "on" "off"
sudo "$VENV_PYTHON" topology/topo.py \
    --scripted --no-load --clients "$CLIENTS" \
    --dscp 46 --rounds "$ROUNDS" --interval "$INTERVAL" \
    --results-dir results/E2

# -----------------------------------------------
echo ""
echo "================================================"
echo " EXPERIMENT E3: Congestion, No Priority (Baseline)"
echo "================================================"
set_mode "off" "off"
sudo "$VENV_PYTHON" topology/topo.py \
    --scripted --clients "$CLIENTS" \
    --dscp 0 --rounds "$ROUNDS" --interval "$INTERVAL" \
    --results-dir results/E3

# -----------------------------------------------
echo ""
echo "================================================"
echo " EXPERIMENT E4: Congestion, SDN Priority ON"
echo "================================================"
set_mode "on" "off"
sudo "$VENV_PYTHON" topology/topo.py \
    --scripted --clients "$CLIENTS" \
    --dscp 46 --rounds "$ROUNDS" --interval "$INTERVAL" \
    --results-dir results/E4

# -----------------------------------------------
echo ""
echo "================================================"
echo " ANALYSIS: Generating Summary Table and Graphs"
echo "================================================"
source "${PROJECT_DIR}/venv/bin/activate"
python3 experiments/analyze.py --results-dir results/

echo ""
echo "================================================"
echo "  Demo Complete!"
echo ""
echo "  Results saved in:  results/"
echo "  Graphs:"
echo "    results/comparison_chart.png"
echo "    results/convergence_plot.png"
echo ""
echo "  To view graphs:"
echo "    xdg-open results/comparison_chart.png"
echo "    xdg-open results/convergence_plot.png"
echo "================================================"
