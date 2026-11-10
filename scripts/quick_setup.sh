#!/usr/bin/env bash
# quick_setup.sh
# Sets up Ryu + Mininet using Python 3.9
# - Python 3.9: works with faucetsdn/ryu (3.9+ syntax) AND eventlet 0.30.2
# - Ryu from faucetsdn/ryu GitHub (PyPI version broken with modern setuptools)
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

# --- Step 2: Install Python 3.9 ---
echo ""
echo "[2/5] Installing Python 3.9..."
# Python 3.9 is the sweet spot:
#   - faucetsdn/ryu uses 3.9+ syntax (ABCMeta subscriptable)
#   - eventlet 0.30.2 works (socket.timeout not yet immutable like in 3.10+)
add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
apt-get update -y -q
apt-get install -y -q python3.9 python3.9-venv python3.9-dev python3.9-distutils
echo "    [OK] Python 3.9 installed: $(python3.9 --version)"

# --- Step 3: Create venv with Python 3.9 ---
echo ""
echo "[3/5] Creating Python 3.9 virtual environment..."
rm -rf "$VENV_DIR"
python3.9 -m venv --system-site-packages "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
echo "    [OK] venv at ${VENV_DIR} ($(${VENV_DIR}/bin/python --version))"

# --- Step 4: Install all Python packages ---
echo ""
echo "[4/5] Installing packages..."

sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" "$PROJECT_DIR" << 'PYEOF'
set -e
VENV_DIR="$1"
PROJECT_DIR="$2"
source "${VENV_DIR}/bin/activate"
echo "    Active Python: $(python --version)"

# Upgrade pip only (not setuptools - ryu needs older version)
pip install -q --upgrade pip 2>/dev/null || true
pip install -q wheel

echo "    Installing eventlet and dependencies..."
pip install -q "eventlet==0.30.2"
pip install -q "webob" "routes" "oslo.config"
pip install -q "msgpack>=0.6.0" "netaddr"
pip install -q "requests" "six"
pip install -q "pkgutil_resolve_name"   # fixes jsonschema warning on Python < 3.9
pip install -q "tinyrpc==0.9.4"         # pin older version that doesn't use 3.9+ syntax in wheels
pip install -q "matplotlib" "pandas" "numpy"

echo "    Cloning faucetsdn/ryu from GitHub..."
rm -rf /tmp/ryu_build
git clone --quiet --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_build
cd /tmp/ryu_build

# Patch hooks.py: removes easy_install.get_script_args (removed in modern setuptools)
cat > ryu/hooks.py << 'HOOKS'
# Patched: removed easy_install dependency (not in modern setuptools)
def setup_hook(config):
    pass

def save_orig():
    pass
HOOKS

echo "    Installing Ryu (this takes ~2 mins)..."
# --no-build-isolation: use our installed packages, not PEP517 build env
if ! pip install --no-build-isolation . 2>/tmp/ryu_install.log; then
    echo ""
    echo "    ERROR: Ryu install failed. Last 20 lines of log:"
    tail -20 /tmp/ryu_install.log
    exit 1
fi

cd -
rm -rf /tmp/ryu_build

# Confirm ryu-manager binary exists
if [ ! -f "${VENV_DIR}/bin/ryu-manager" ]; then
    echo "    ERROR: ryu-manager not found after install - Ryu install failed silently"
    exit 1
fi
echo "    [OK] Ryu installed: $(${VENV_DIR}/bin/ryu-manager --version 2>&1)"

# Apply collections compatibility shim (safety net)
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

# Link system mininet into venv site-packages
python -c "
import site, pathlib
sp = pathlib.Path(site.getsitepackages()[0])
(sp / 'mininet_sys.pth').write_text('/usr/lib/python3/dist-packages\n')
print('    [OK] Mininet linked into venv')
"

echo "    [OK] All packages installed successfully"
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
