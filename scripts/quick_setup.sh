#!/usr/bin/env bash
# quick_setup.sh
# One-shot setup for SDN Clock Sync project on Ubuntu 22.04
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
    echo "    Python 3.${PY_MINOR} detected. Installing Python 3.10..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y -q
    apt-get install -y -q python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
fi
echo "    [OK] Using ${PYTHON_CMD}"

# --- Step 3: Create venv ---
echo ""
echo "[3/5] Creating virtual environment..."
rm -rf "$VENV_DIR"
$PYTHON_CMD -m venv --system-site-packages "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
echo "    [OK] venv created at ${VENV_DIR}"

# --- Step 4: Install all Python packages ---
echo ""
echo "[4/5] Installing Python packages..."

sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" "$PROJECT_DIR" << 'PYEOF'
set -e
VENV_DIR="$1"
PROJECT_DIR="$2"
source "${VENV_DIR}/bin/activate"

echo "    Upgrading pip..."
pip install -q --upgrade pip wheel packaging

echo "    Installing eventlet and dependencies..."
pip install -q "eventlet==0.33.3"
pip install -q "pbr" "MarkupSafe==2.0.1" "webob" "routes"
pip install -q "oslo.config" "msgpack==1.0.5" "netaddr" "tinyrpc"
pip install -q "requests" "matplotlib" "pandas" "numpy"

echo "    Installing Ryu from source..."
rm -rf /tmp/ryu_build
git clone --quiet --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_build

# Patch ryu/hooks.py (import error)
cat > /tmp/ryu_build/ryu/hooks.py << 'HOOKS'
def setup_hook(config):
    pass
def save_orig():
    pass
HOOKS

cd /tmp/ryu_build
pip install -q --no-build-isolation . 2>&1 | grep -v "BuildBackendWarning" | grep -v "^\s*!!" | grep -v "^\s*\*\*\*" | grep -v "^$" | grep -v "setuptools" | grep -v "packaging" || true
cd -
rm -rf /tmp/ryu_build
echo "    [OK] Ryu installed"

# =====================================================
# IMPORTANT: Patch eventlet AFTER all installs
# (Ryu install can reinstall eventlet, wiping patches)
# =====================================================
echo "    Patching eventlet for Python 3.10 compatibility..."

python3 << 'PATCH'
import os, glob

# Find eventlet timeout.py in the venv
venv = os.environ.get('VIRTUAL_ENV', '')
candidates = glob.glob(os.path.join(venv, 'lib', 'python*', 'site-packages', 'eventlet', 'timeout.py'))

if not candidates:
    print("    WARNING: Could not find eventlet/timeout.py to patch")
else:
    for fpath in candidates:
        with open(fpath, 'r') as f:
            lines = f.readlines()

        new_lines = []
        i = 0
        patched = False
        while i < len(lines):
            line = lines[i]
            # Find the problematic line (with any indentation)
            stripped = line.rstrip()
            if 'base.is_timeout = property(lambda _: True)' in stripped and 'try:' not in (lines[i-1] if i > 0 else ''):
                indent = len(line) - len(line.lstrip())
                ind = ' ' * indent
                new_lines.append(f'{ind}try:\n')
                new_lines.append(f'{ind}    base.is_timeout = property(lambda _: True)\n')
                new_lines.append(f'{ind}except TypeError:\n')
                new_lines.append(f'{ind}    pass  # Python 3.10+: socket.timeout is immutable\n')
                patched = True
            else:
                new_lines.append(line)
            i += 1

        with open(fpath, 'w') as f:
            f.writelines(new_lines)

        if patched:
            print(f'    Patched eventlet: {fpath}')
        else:
            print(f'    Already patched or line not found: {fpath}')
PATCH

# Apply collections shim
echo "    Applying collections compatibility shim..."
SITE_DIR=$(python3 -c "import site; print(site.getsitepackages()[0])")
cat > "${SITE_DIR}/sitecustomize.py" << 'SHIM'
import collections, collections.abc
for _n in ['MutableMapping','MutableSet','MutableSequence','Mapping',
           'Sequence','Set','Iterable','Callable','Iterator','Container','Hashable','Sized']:
    if hasattr(collections.abc, _n) and not hasattr(collections, _n):
        setattr(collections, _n, getattr(collections.abc, _n))
SHIM

# Link system mininet into venv
python3 -c "
import site, pathlib
sp = pathlib.Path(site.getsitepackages()[0])
(sp / 'mininet_sys.pth').write_text('/usr/lib/python3/dist-packages\n')
print('    Mininet linked into venv')
"

echo "    [OK] All packages installed and patched"
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
echo "  TERMINAL 1:"
echo "    cd ${PROJECT_DIR}"
echo "    source venv/bin/activate"
echo "    ryu-manager controller/sync_controller.py --ofp-tcp-listen-port 6653"
echo ""
echo "  TERMINAL 2 (new window):"
echo "    cd ${PROJECT_DIR}"
echo "    sudo venv/bin/python3 topology/topo.py"
echo "================================================"
