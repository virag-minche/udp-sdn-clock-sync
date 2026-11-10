#!/usr/bin/env bash
# quick_setup.sh
# Sets up Ryu + Mininet for SDN Clock Sync project
# Uses Python 3.8 (required) + ryu from GitHub source (PyPI version is broken)
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
    libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev \
    software-properties-common
systemctl enable openvswitch-switch 2>/dev/null || true
systemctl restart openvswitch-switch 2>/dev/null || true
echo "    [OK] System packages installed."

# --- Step 2: Install Python 3.8 ---
echo ""
echo "[2/5] Installing Python 3.8..."
# Ryu + eventlet only work correctly on Python 3.8
# Python 3.9+ has breaking changes in socket/collections that break eventlet
add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
apt-get update -y -q
apt-get install -y -q python3.8 python3.8-venv python3.8-dev python3.8-distutils
echo "    [OK] Python 3.8 ready."

# --- Step 3: Create venv with Python 3.8 ---
echo ""
echo "[3/5] Creating Python 3.8 virtual environment..."
rm -rf "$VENV_DIR"
python3.8 -m venv --system-site-packages "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
echo "    [OK] venv at ${VENV_DIR} (Python 3.8)"

# --- Step 4: Install all Python packages ---
echo ""
echo "[4/5] Installing packages..."

sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" "$PROJECT_DIR" << 'PYEOF'
set -e
VENV_DIR="$1"
PROJECT_DIR="$2"
source "${VENV_DIR}/bin/activate"

echo "    Python: $(python --version)"

# Upgrade pip but DO NOT upgrade setuptools yet
# (we need to control setuptools version for Ryu)
pip install -q --upgrade pip
pip install -q wheel

# Install Ryu dependencies first
echo "    Installing core dependencies..."
pip install -q "eventlet==0.30.2"
pip install -q "webob" "routes" "oslo.config"
pip install -q "msgpack>=0.6.0" "netaddr" "tinyrpc"
pip install -q "requests" "six"
pip install -q "matplotlib" "pandas" "numpy"

# Clone and install Ryu from GitHub source
# PyPI ryu==4.34 is broken with modern pip - use faucetsdn fork
echo "    Cloning Ryu from GitHub (faucetsdn/ryu)..."
rm -rf /tmp/ryu_build
git clone --quiet --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_build

# CRITICAL: Patch hooks.py which calls easy_install.get_script_args
# This was removed from setuptools and causes install to fail
cat > /tmp/ryu_build/ryu/hooks.py << 'HOOKS'
# Patched: removed easy_install dependency (removed in modern setuptools)
def setup_hook(config):
    pass

def save_orig():
    pass
HOOKS

echo "    Installing Ryu from source..."
cd /tmp/ryu_build
# --no-build-isolation: use our pinned setuptools, not build system's
pip install -q --no-build-isolation . 2>&1 | \
    grep -v "BuildBackendWarning" | \
    grep -v "^\s*!!" | \
    grep -v "^\s*\*\*\*\*" | \
    grep -v "setuptools.dist" | \
    grep -E "(error|Error|ERROR|warning: |OK|Installing|Ryu)" || true
cd -
rm -rf /tmp/ryu_build
echo "    [OK] Ryu installed from GitHub source"

# Apply collections compatibility shim (Python 3.8 safety net)
echo "    Applying compatibility shim..."
SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])")
cat > "${SITE_DIR}/sitecustomize.py" << 'SHIM'
import collections, collections.abc
for _n in ['MutableMapping', 'MutableSet', 'MutableSequence', 'Mapping',
           'Sequence', 'Set', 'Iterable', 'Callable', 'Iterator',
           'Container', 'Hashable', 'Sized']:
    if hasattr(collections.abc, _n) and not hasattr(collections, _n):
        setattr(collections, _n, getattr(collections.abc, _n))
SHIM

# Link system mininet into venv
python -c "
import site, pathlib
sp = pathlib.Path(site.getsitepackages()[0])
(sp / 'mininet_sys.pth').write_text('/usr/lib/python3/dist-packages\n')
print('    [OK] Mininet linked')
"
echo "    [OK] All packages ready"
PYEOF

# --- Step 5: Verify ---
echo ""
echo "[5/5] Verifying installation..."

echo -n "    Open vSwitch: "
ovs-vsctl --version 2>/dev/null | head -1 || echo "NOT FOUND"

echo -n "    Mininet:      "
mn --version 2>&1 | head -1 || echo "NOT FOUND"

echo -n "    Ryu:          "
"${VENV_DIR}/bin/ryu-manager" --version 2>&1 || echo "FAILED"

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
echo "  TERMINAL 2 (open new window):"
echo "    cd ${PROJECT_DIR}"
echo "    sudo venv/bin/python3 topology/topo.py"
echo "================================================"
