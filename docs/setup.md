# Environment Setup & Deployment Guide

This guide provides step-by-step instructions for establishing the complete SDN and Time Synchronization emulation environment on **Ubuntu 22.04 LTS (x86_64)**.

---

## 1. System Requirements & Prerequisites
* **Operating System**: Ubuntu 22.04 LTS (native installation, dual boot, or VMware/VirtualBox VM).
* **RAM**: Minimum 4 GB (8 GB recommended).
* **CPU**: 2+ virtual cores.
* **Privileges**: Root / `sudo` access.

---

## 2. Step-by-Step Installation Commands

### Step 2.1: Update System Packages & Install Core Tools
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential git curl wget net-tools tcpdump iperf3 \
                    python3 python3-pip python3-dev python3-venv \
                    openvswitch-switch openvswitch-testcontroller
```

### Step 2.2: Install Mininet 2.3.0
```bash
# Clone the official Mininet repository
git clone https://github.com/mininet/mininet.git ~/mininet
cd ~/mininet

# Checkout the stable 2.3.0 release
git checkout -b 2.3.0 2.3.0

# Install Mininet core and Open vSwitch integration (-n installs core dependencies without downloading unnecessary third-party controllers)
sudo PYTHON=python3 ./util/install.sh -nv
```

### Step 2.3: Set Up Dedicated Python 3.10 Virtual Environment for Ryu
> [!IMPORTANT]
> Ryu SDN Controller requires Python 3.10 and a pinned version of `eventlet`. Python 3.11+ removed internal modules (`asyncore`/`asynchat`) that Ryu depends on.

```bash
# Verify Python version (Ubuntu 22.04 default is Python 3.10)
python3 --version  # Should output: Python 3.10.x

# Create virtual environment in home directory
python3 -m venv ~/ryu-env
source ~/ryu-env/bin/activate

# Upgrade pip, wheel, and setuptools
pip install --upgrade pip setuptools wheel
```

### Step 2.4: Install Ryu 4.34 and the Critical `eventlet` Patch
> [!WARNING]
> **The Known Ryu + Eventlet Incompatibility**:
> Ryu relies on `eventlet` for green-thread concurrency. Starting in `eventlet` version 0.31.0, internal DNS and timeout submodules were restructured and removed, which causes Ryu to crash with `ImportError: cannot import name 'dns' from 'eventlet.support'`.
> **Fix**: Pin `eventlet==0.30.2` and install modern Ryu requirements.

```bash
# Install the exact working eventlet version first
pip install eventlet==0.30.2

# Install dependencies needed by Ryu
pip install msgpack-python oslo.config routes webob requests matplotlib pandas

# Install Ryu SDN Framework
pip install ryu==4.34
```

---

## 3. Verification Checklist

Run each of the following verification commands to ensure your environment is fully operational:

| Component | Verification Command | Expected Output | Status Check |
| :--- | :--- | :--- | :--- |
| **Mininet** | `sudo mn --test pingall` | `0% dropped (2/2 received)` | [ ] Pass |
| **Open vSwitch** | `ovs-vsctl --version` | `ovs-vsctl (Open vSwitch) 2.17.x` (or newer) | [ ] Pass |
| **OVS Service** | `sudo systemctl status openvswitch-switch` | `Active: active (running)` | [ ] Pass |
| **Ryu Controller** | `source ~/ryu-env/bin/activate && ryu-manager --version` | `ryu-manager 4.34` | [ ] Pass |
| **iperf3** | `iperf3 --version` | `iperf 3.9+ (cJSON 1.7.x)` | [ ] Pass |

---

## 4. Troubleshooting Table (8 Most Common Errors & Fixes)

| Error Symptoms | Root Cause | Exact Solution / Command |
| :--- | :--- | :--- |
| **1. `Cannot bind to socket: Address already in use` or stale switches** | A previous Mininet crash left virtual interfaces and controller ports allocated. | Run the deep Mininet cleanup utility:<br>`sudo mn -c` |
| **2. Controller connection fails / Switches cannot connect** | OVS cannot reach TCP port 6653 or firewall is active. | Verify Ryu is listening: `sudo netstat -tlpn \| grep 6653`<br>Check UFW firewall: `sudo ufw allow 6653/tcp` |
| **3. OpenFlow version mismatch / `OFPHandler... unhandled message`** | OVS defaulted to OpenFlow 1.0 instead of 1.3. | In your Mininet script, explicitly specify:<br>`OVSSwitch(name, protocols='OpenFlow13')`<br>Or CLI: `ovs-vsctl set bridge s1 protocols=OpenFlow13` |
| **4. Controller port confusion (6653 vs 6633)** | Port 6633 is the deprecated legacy port. Port 6653 is the official IANA standard. | Always launch Ryu specifying port 6653 or verify RemoteController uses: `RemoteController('c0', ip='127.0.0.1', port=6653)` |
| **5. Ryu crashes with `ImportError: cannot import name 'dns'`** | Python environment pulled `eventlet >= 0.31.0`. | Downgrade eventlet immediately:<br>`pip install eventlet==0.30.2` |
| **6. `Permission denied` when running Mininet or creating veth** | Network namespace and kernel `tc` calls require root privilege. | Always run Mininet scripts with `sudo`:<br>`sudo python3 mininet/topology.py`<br>*(Note: Run Ryu without sudo)* |
| **7. `ovs-vsctl: unix:/var/run/openvswitch/db.sock: database connection failed`** | Open vSwitch daemon is not running. | Restart the OVS service:<br>`sudo systemctl start openvswitch-switch` |
| **8. Ryu crashes on Python 3.11+ with `ModuleNotFoundError: No module named 'asyncore'`** | Host OS has Python 3.11+ which removed deprecated Python modules. | Use Python 3.10 specifically inside a virtual environment (`python3.10 -m venv ~/ryu-env`). |

---

## 5. Integrating Project Files into the Mininet Environment

Ensure all files from the workspace are placed in a clean directory on your Ubuntu machine (e.g., `~/cn_project/`):

```bash
mkdir -p ~/cn_project/{docs,src,sdn,mininet,scripts,results}
```

Copy the source tree:
* `src/clock.py`: Simulated clock oscillator class with offset & drift.
* `src/sync_protocol.py`: Timestamp packet encoder, decoder, and offset math.
* `src/client.py`: Client daemon implementing periodic sync and servo filtering.
* `src/server.py`: Low-latency UDP reference time server.
* `sdn/controller.py`: Ryu OpenFlow 1.3 controller with QoS, monitoring, and failover.
* `sdn/rest_api.py`: Ryu WSGI REST endpoint service.
* `mininet/topology.py`: Dual-switch bottleneck and failover topology with HTB queues.

---

## 6. How Mininet Hosts Execute Commands

Mininet creates isolated Linux network namespaces for each host (`c1`, `srv`, `h_bg`, etc.), while sharing the underlying Linux filesystem and CPU.

### 6.1 Method 1: Using the Interactive Mininet CLI
When running Mininet interactively (`sudo python3 mininet/topology.py`):
```bash
# Syntax: <host_name> <command>
mininet> srv python3 src/server.py --port 5005 &
mininet> c1 python3 src/client.py --server 10.0.0.100 --id 1 &
mininet> h_sink iperf3 -s &
mininet> h_bg iperf3 -c 10.0.0.101 -u -b 10M -t 30 &
```

### 6.2 Method 2: Programmatically in Automated Python Scripts
Inside experiment automation scripts (e.g., `scripts/run_all.sh` or `experiments/runner.py`), use the `host.cmd()` or `host.popen()` API:
```python
# Synchronous execution (waits for completion)
output = client_node.cmd('python3 src/client.py --server 10.0.0.100 --id 1 --cycles 50')

# Asynchronous execution (runs in background)
server_proc = server_node.popen('python3 src/server.py --port 5005', shell=True)
bg_proc = bg_node.popen('iperf3 -c 10.0.0.101 -u -b 12M -t 60', shell=True)
```
