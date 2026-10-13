#!/usr/bin/env bash
# ==============================================================================
# scripts/install.sh
# Installation and Environment Setup Script for SDN Network Time Sync Project
# Target OS: Ubuntu 22.04 LTS
#
# Pinned Versions:
# - Python: 3.9 (or 3.10) with venv
# - Mininet: 2.3.0
# - Open vSwitch: 2.17+ (system package)
# - Ryu SDN Controller: 4.34
# - Eventlet: 0.30.2 (Critical: eventlet >= 0.31 deprecates/removes ALPN/monkey_patch
#   internals that Ryu 4.34 relies on, causing immediate runtime crashes on startup)
# - iperf3: system package
# ==============================================================================

set -euo pipefail

echo "============================================================"
echo " Starting SDN Time Synchronization Project Setup"
echo " Target: Ubuntu 22.04 LTS"
echo "============================================================"

# Ensure script is executed with root or sudo privileges
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

# Start and enable Open vSwitch service
systemctl enable openvswitch-switch
systemctl restart openvswitch-switch

echo "[3/6] Setting up Python 3.9 / 3.10 environment..."
# Ubuntu 22.04 ships with Python 3.10 as standard. We install python3-pip, python3-dev, python3-venv.
apt-get install -y \
    python3-pip \
    python3-dev \
    python3-venv \
    python3-setuptools

VENV_DIR="${PROJECT_DIR}/venv"

echo "[4/6] Creating Python virtual environment at ${VENV_DIR}..."
# Create virtual environment owned by the invoking user
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$VENV_DIR"
fi

echo "[5/6] Installing Ryu and pinned networking libraries into virtualenv..."
# Switch to non-root user when installing python packages inside venv
sudo -u "$ACTUAL_USER" bash << 'EOF'
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
source "${VENV_DIR}/bin/activate"

# Upgrade pip and wheel first to avoid legacy setup.py build errors
pip install --upgrade pip setuptools wheel

# Critical Note on Ryu and Eventlet:
# eventlet 0.30.2 is strictly required because eventlet >= 0.31 breaks Ryu's WSGI hub
# and SSL monkey patching. Also pin greenlet and tinyrpc compatible with Ryu 4.34.
pip install "eventlet==0.30.2"
pip install "greenlet==1.1.2"
pip install "tinyrpc==1.1.4"
pip install "webob>=1.2"
pip install "routes>=1.12.3"
pip install "oslo.config>=6.0.0"
pip install "msgpack>=0.5.3"
pip install "netaddr>=0.7.18"

# Install Ryu 4.34
pip install "ryu==4.34"

# Project utilities: matplotlib, numpy, scipy for log parsing and analysis if needed
pip install numpy matplotlib
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

echo "============================================================"
echo " Setup Completed Successfully!"
echo " Activate virtualenv: source ${VENV_DIR}/bin/activate"
echo " Test controller: ryu-manager ryu.app.simple_switch_13"
echo " Test Mininet: sudo mn --test pingall"
echo "============================================================"
