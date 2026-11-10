#!/usr/bin/env bash
# quick_setup.sh
# One-shot setup script for the SDN Clock Sync project on Ubuntu 22.04
# Usage: sudo bash scripts/quick_setup.sh

set -e

ACTUAL_USER="${SUDO_USER:-$USER}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"

echo "================================================"
echo "  SDN Clock Sync - Quick Setup"
echo "  Project: ${PROJECT_DIR}"
echo "================================================"

# --- Step 1: System packages ---
echo ""
echo "[1/5] Installing system packages..."
apt-get update -y -q
apt-get install -y -q \
    build-essential git curl wget net-tools iproute2 \
    tcpdump iperf3 openvswitch-switch mininet \
    python3-pip python3-dev python3-venv python3-setuptools \
    libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev
systemctl enable openvswitch-switch 2>/dev/null || true
systemctl restart openvswitch-switch 2>/dev/null || true
echo "    [OK] System packages installed."

# --- Step 2: Check Python version ---
echo ""
echo "[2/5] Checking Python version..."
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PYTHON_CMD="python3"
if [ "$PY_MINOR" -ge 12 ]; then
    echo "    Python 3.${PY_MINOR} detected. Installing Python 3.10 for Ryu compatibility..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y -q
    apt-get install -y -q python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
    echo "    [OK] Will use python3.10 for venv"
else
    echo "    [OK] Python 3.${PY_MINOR} is compatible"
fi

# --- Step 3: Create venv ---
echo ""
echo "[3/5] Creating virtual environment..."
rm -rf "$VENV_DIR"
$PYTHON_CMD -m venv --system-site-packages "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
echo "    [OK] venv created at ${VENV_DIR}"

# --- Step 4: Install Python packages ---
echo ""
echo "[4/5] Installing Python packages..."

sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" "$PROJECT_DIR" << 'PYEOF'
set -e
VENV_DIR="$1"
PROJECT_DIR="$2"
source "${VENV_DIR}/bin/activate"

echo "    Upgrading pip..."
# Do NOT pin setuptools — just upgrade pip normally
pip install -q --upgrade pip
pip install -q --upgrade wheel

echo "    Installing eventlet (pinned for Ryu compatibility)..."
pip install -q "eventlet==0.30.2"

echo "    Installing Ryu dependencies..."
pip install -q "pbr"
pip install -q "MarkupSafe==2.0.1"
pip install -q "webob"
pip install -q "routes"
pip install -q "oslo.config"
pip install -q "msgpack==1.0.5"
pip install -q "netaddr"
pip install -q "tinyrpc"
pip install -q "requests"
pip install -q "matplotlib" "pandas" "numpy"

echo "    Installing Ryu SDN controller from source..."
rm -rf /tmp/ryu_build
git clone --quiet --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_build

# Patch ryu/hooks.py which causes import errors on newer Python
cat > /tmp/ryu_build/ryu/hooks.py << 'HOOKS'
def setup_hook(config):
    pass
def save_orig():
    pass
HOOKS

cd /tmp/ryu_build
# Use --no-build-isolation to avoid setuptools version conflicts
pip install -q --no-build-isolation .
cd -
rm -rf /tmp/ryu_build
echo "    [OK] Ryu installed from source"

echo "    Applying collections compatibility shim..."
# Fix: Python 3.10 removed collections.MutableMapping etc.
SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])")
cat > "${SITE_DIR}/sitecustomize.py" << 'SHIM'
import collections
import collections.abc
_names = [
    'MutableMapping', 'MutableSet', 'MutableSequence',
    'Mapping', 'Sequence', 'Set', 'Iterable',
    'Callable', 'Iterator', 'Container', 'Hashable', 'Sized'
]
for _n in _names:
    if hasattr(collections.abc, _n) and not hasattr(collections, _n):
        setattr(collections, _n, getattr(collections.abc, _n))
SHIM

# Link system mininet into venv site-packages
python -c "
import site, pathlib
sp = pathlib.Path(site.getsitepackages()[0])
pth = sp / 'mininet_sys.pth'
pth.write_text('/usr/lib/python3/dist-packages\n')
print('    Mininet path linked into venv')
"

echo "    [OK] All Python packages installed"
PYEOF

# --- Step 5: Verify ---
echo ""
echo "[5/5] Verifying installation..."

echo -n "    Open vSwitch: "
ovs-vsctl --version 2>/dev/null | head -1 || echo "NOT FOUND"

echo -n "    Mininet:      "
mn --version 2>&1 | head -1 || echo "NOT FOUND"

echo -n "    Ryu:          "
"${VENV_DIR}/bin/ryu-manager" --version 2>&1 || echo "NOT FOUND"

echo -n "    iperf3:       "
iperf3 --version 2>/dev/null | head -1 || echo "NOT FOUND"

mkdir -p "${PROJECT_DIR}/results"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "${PROJECT_DIR}/results" 2>/dev/null || true

echo ""
echo "================================================"
echo "  SETUP COMPLETE!"
echo ""
echo "  TERMINAL 1 (Ryu Controller):"
echo "    cd ${PROJECT_DIR}"
echo "    source venv/bin/activate"
echo "    ryu-manager controller/sync_controller.py --ofp-tcp-listen-port 6653"
echo ""
echo "  TERMINAL 2 (Mininet + Demo):"
echo "    cd ${PROJECT_DIR}"
echo "    sudo venv/bin/python3 topology/topo.py"
echo "  OR run full automated demo:"
echo "    bash scripts/demo.sh"
echo "================================================"
