#!/usr/bin/env bash
# Setup script for SDN time sync project on Ubuntu 22.04
# Installs mininet, ovs, ryu controller and python deps

set -euo pipefail

echo "Starting SDN Time Synchronization Project Setup"
echo "Target: Ubuntu 22.04 LTS"

# check for root / sudo
if [[ $EUID -ne 0 ]]; then
   echo "[ERROR] This installation script must be run with root privileges (sudo)." >&2
   exit 1
fi

ACTUAL_USER="${SUDO_USER:-$USER}"
USER_HOME=$(getent passwd "$ACTUAL_USER" | cut -d: -f6)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[1/6] Updating APT repositories and installing prerequisites..."
apt-get update -y
apt-get install -y --no-install-recommends \
    software-properties-common \
    curl \
    git \
    net-tools \
    iproute2 \
    ethtool \
    tcpdump \
    iperf3 \
    build-essential \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev

echo "[2/6] Installing Open vSwitch and Mininet..."
apt-get install -y \
    openvswitch-switch \
    openvswitch-common \
    mininet

# start ovs service
systemctl enable openvswitch-switch
systemctl restart openvswitch-switch

echo "[3/6] Setting up Python environment..."
apt-get install -y \
    python3-pip \
    python3-dev \
    python3-venv \
    python3-setuptools

# Check Python version: Python 3.12+ (Ubuntu 24.04) completely removed the 'imp' module
# which Ryu requires. If host is >= 3.12, install Python 3.10 via deadsnakes PPA.
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

PYTHON_CMD="python3"
if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 12 ]; then
    echo "[!] Detected Python 3.${PY_MINOR}. Python 3.12+ removed 'imp' which Ryu requires."
    echo "[*] Adding deadsnakes PPA to install Python 3.10 for Ryu virtual environment..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
fi

VENV_DIR="${PROJECT_DIR}/venv"

echo "[4/6] Creating Python virtual environment at ${VENV_DIR} using ${PYTHON_CMD}..."
# Recreate venv cleanly
rm -rf "$VENV_DIR"
$PYTHON_CMD -m venv "$VENV_DIR"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"

echo "[5/6] Installing Ryu and pinned networking libraries into virtualenv..."
# run pip install in venv as user
sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" << 'EOF'
set -euo pipefail
VENV_DIR="$1"
source "${VENV_DIR}/bin/activate"

# Add compatibility shim for legacy packages expecting collections.MutableMapping
python -c "
import site, pathlib
p = pathlib.Path(site.getsitepackages()[0]) / 'sitecustomize.py'
p.write_text('''import collections, collections.abc
for n in ('MutableMapping','MutableSet','MutableSequence','Mapping','Sequence','Set','Iterable','Callable','Iterator','Container','Hashable','Sized'):
    if hasattr(collections.abc, n) and not hasattr(collections, n):
        setattr(collections, n, getattr(collections.abc, n))
''')
"

# upgrade packaging tools
pip install --upgrade pip setuptools wheel
pip install pbr

# install required dependencies with pre-built binary wheels
pip install "MarkupSafe==2.0.1"
pip install "eventlet==0.33.3"
pip install "greenlet>=1.1.2"
pip install "tinyrpc>=1.0.4"
pip install "webob>=1.2"
pip install "routes>=1.12.3"
pip install "oslo.config>=6.0.0"
pip install "msgpack>=0.5.3"
pip install "netaddr>=0.7.18"

# plotting libraries for experiments
pip install numpy matplotlib

# install Ryu directly from repo with compatibility patch for Python 3.10
echo "[*] Installing Ryu SDN Controller..."
rm -rf /tmp/ryu_install
git clone --depth 1 https://github.com/faucetsdn/ryu.git /tmp/ryu_install

# Safely dummy out the obsolete hooks without breaking setup.cfg syntax
cat << 'HOOKS_EOF' > /tmp/ryu_install/ryu/hooks.py
def setup_hook(config):
    pass

def save_orig():
    pass
HOOKS_EOF

cd /tmp/ryu_install
python setup.py install
cd -
rm -rf /tmp/ryu_install
EOF

echo "[6/6] Verifying installations..."
echo "--- Open vSwitch Version ---"
ovs-vsctl --version || true

echo "--- Mininet Version ---"
mn --version || true

echo "--- Ryu Controller Version in Virtualenv ---"
"${VENV_DIR}/bin/ryu-manager" --version || true

echo "--- iperf3 Version ---"
iperf3 --version || true

echo "Setup Completed Successfully!"
echo "Activate virtualenv: source ${VENV_DIR}/bin/activate"
echo "Test controller: ryu-manager ryu.app.simple_switch_13"
echo "Test Mininet: sudo mn --test pingall"
