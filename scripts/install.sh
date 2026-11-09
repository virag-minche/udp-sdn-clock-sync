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

echo "[3/6] Setting up Python 3.9 / 3.10 environment..."
apt-get install -y \
    python3-pip \
    python3-dev \
    python3-venv \
    python3-setuptools

VENV_DIR="${PROJECT_DIR}/venv"

echo "[4/6] Creating Python virtual environment at ${VENV_DIR}..."
# create virtualenv if activate script doesn't exist
if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
fi

echo "[5/6] Installing Ryu and pinned networking libraries into virtualenv..."
# run pip install in venv as user
sudo -u "$ACTUAL_USER" bash -s "$VENV_DIR" << 'EOF'
set -euo pipefail
VENV_DIR="$1"
source "${VENV_DIR}/bin/activate"

# upgrade pip and install build helpers
pip install --upgrade "pip<24" wheel
pip install "setuptools<=67.6.1" pbr

# install required dependencies with pre-built binary wheels
pip install "MarkupSafe==2.0.1"
pip install "eventlet==0.30.2" || pip install "eventlet==0.33.3"
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

# Disable obsolete setuptools save_orig hook which fails on modern environments
sed -i 's/^import ryu\.hooks/# import ryu.hooks/' /tmp/ryu_install/setup.py
sed -i 's/^ryu\.hooks\.save_orig()/# ryu.hooks.save_orig()/' /tmp/ryu_install/setup.py
sed -i 's/.*_orig_get_script_args.*/    pass/' /tmp/ryu_install/ryu/hooks.py 2>/dev/null || true

pip install --no-deps /tmp/ryu_install
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
