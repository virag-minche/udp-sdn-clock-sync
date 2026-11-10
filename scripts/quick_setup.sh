#!/usr/bin/env bash
# quick_setup.sh - Run this once on Ubuntu to install everything needed.
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

systemctl enable openvswitch-switch
systemctl restart openvswitch-switch
echo "    [OK] System packages installed."

# --- Step 2: Check Python version ---
echo ""
echo "[2/5] Checking Python version..."
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PYTHON_CMD="python3"

if [ "$PY_MINOR" -ge 12 ]; then
    echo "    Python 3.${PY_MINOR} detected - installing Python 3.10 for Ryu compatibility..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y -q
    apt-get install -y -q python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
else
    echo "    Python 3.${PY_MINOR} - OK"
fi

# --- Step 3: Create venv ---
echo ""
echo "[3/5] Creating virtual environment..."
rm -rf "$VENV_DIR"
$PYTHON_CMD -m venv --system-site-packages "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
echo "    [OK] Virtual environment created at ${VENV_DIR}"

# --- Step 4: Install Python packages ---
echo ""
echo "[4/5] Installing Python packages (Ryu + dependencies)..."

sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" << 'PYEOF'
set -e
VENV_DIR="$1"
source "${VENV_DIR}/bin/activate"

echo "    Upgrading pip and setuptools..."
# Use older setuptools - newer ones break ryu's setup.py metadata
pip install -q "setuptools==59.6.0" "wheel" "pip>=22,<23"

echo "    Installing core dependencies..."
pip install -q "eventlet==0.30.2"
pip install -q "pbr"
pip install -q "MarkupSafe==2.0.1"
pip install -q "webob>=1.2"
pip install -q "routes>=1.12.3"
pip install -q "oslo.config>=6.0.0"
pip install -q "msgpack==1.0.5"
pip install -q "netaddr"
pip install -q "tinyrpc"
pip install -q "requests"
pip install -q "matplotlib" "pandas" "numpy"

echo "    Installing Ryu SDN Controller..."
# Clone from faucetsdn fork which has Python 3.10 fixes
rm -rf /tmp/ryu_build
git clone --quiet --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_build

# Patch hooks.py to remove broken import
cat > /tmp/ryu_build/ryu/hooks.py << 'HOOKS'
def setup_hook(config):
    pass
def save_orig():
    pass
HOOKS

cd /tmp/ryu_build
python setup.py install -q
cd -
rm -rf /tmp/ryu_build

echo "    Applying collections compatibility shim..."
# Fix collections.MutableMapping error for Python 3.10+
SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])")
cat > "${SITE_DIR}/sitecustomize.py" << 'SHIM'
import collections, collections.abc
for _n in ('MutableMapping','MutableSet','MutableSequence','Mapping',
           'Sequence','Set','Iterable','Callable','Iterator','Container'):
    if hasattr(collections.abc, _n) and not hasattr(collections, _n):
        setattr(collections, _n, getattr(collections.abc, _n))
SHIM

# Link system mininet into venv
python -c "
import site, pathlib
sp = pathlib.Path(site.getsitepackages()[0])
pth = sp / 'mininet_sys.pth'
pth.write_text('/usr/lib/python3/dist-packages\n')
"

echo "    [OK] All packages installed."
PYEOF

# --- Step 5: Verify ---
echo ""
echo "[5/5] Verifying installation..."
echo -n "    Open vSwitch: "
ovs-vsctl --version | head -1

echo -n "    Mininet:      "
mn --version 2>&1 | head -1

echo -n "    Ryu:          "
"${VENV_DIR}/bin/ryu-manager" --version 2>&1

mkdir -p "${PROJECT_DIR}/results"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "${PROJECT_DIR}/results"

echo ""
echo "================================================"
echo "  Setup Complete!"
echo ""
echo "  To run the demo:"
echo "    Terminal 1:  source venv/bin/activate"
echo "                 ryu-manager controller/sync_controller.py --ofp-tcp-listen-port 6653"
echo ""
echo "    Terminal 2:  sudo venv/bin/python3 topology/topo.py"
echo "================================================"
