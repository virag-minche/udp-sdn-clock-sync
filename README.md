# SDN-Assisted Network Time Synchronization System

**Computer Networks Jackfruit Mini Project | SDN + Socket Programming | Topic 23 | Protocol: UDP + OpenFlow 1.3**

## Overview

A complete, end-to-end **NTP-Style UDP Network Time Synchronization System** integrated with an **OpenFlow 1.3 Ryu SDN Controller** and **Mininet / Open vSwitch (OVS)** topology. Multiple clients (each with simulated clock skew and $10\text{ ppm}$ oscillator drift) synchronize against a reference UDP time server using the 4-timestamp NTP algorithm ($T_1, T_2, T_3, T_4$). The SDN controller classifies DSCP-46 (`IP_TOS = 0xB8`) synchronization traffic into high-priority OVS `linux-htb` queues, dynamically throttles background cross-traffic under congestion, reacts to client-reported sync errors via REST telemetry, and automatically reroutes around link failures.

---

## Repository Structure

```text
udp-sdn-clock-sync/
├── README.md                              # Project overview & reproduction guide
├── src/                                   # Role 1 & Role 3: Socket App + Load Generator
│   ├── timesync_core.py                   # UDP time sync server, client & multi-client engine
│   ├── test_multiclient.py                # Automated multi-client convergence & DSCP test suite
│   └── loadgen.py                         # Configurable UDP background traffic generator & sink
├── controller/                            # Role 2: OpenFlow 1.3 SDN Controller
│   └── sync_controller.py                 # Ryu controller (QoS, congestion hysteresis, REST, failover)
├── topology/                              # Role 2: Mininet Network Topology
│   └── topo.py                            # Dual-switch bottleneck + backup topology & orchestrator
├── scripts/                               # Role 2: Environment & Queue Setup Scripts
│   ├── install.sh                         # Ubuntu 22.04 installer (Mininet, OVS, Ryu 4.34, eventlet)
│   ├── setup_queues.sh                    # Idempotent OVS linux-htb egress queue configurator
│   └── run_all.sh                         # One-command startup (cleanup + Ryu + Mininet)
├── tests/                                 # Role 2: Verification & Wire-Level Probes
│   ├── test_network.py                    # 8-point automated SDN & Mininet integration test suite
│   ├── test_network.sh                    # Bash wrapper with OVS/tc/tcpdump diagnostics
│   └── probe.py                           # Standalone DSCP-46 UDP echo client/server probe
├── experiments/                           # Role 2 & Role 3: Experiment Automation & Analysis
│   ├── run_experiments.sh                 # Automated runner for Experiments E1–E6 (3, 5, 10 clients)
│   └── analyze.py                         # Unified statistical analyzer, comparison bar chart & convergence plotter
├── docs/                                  # Complete Project Documentation, Viva Prep & Guides
│   ├── architecture.md                    # System architecture, problem statement & packet format
│   ├── setup.md                           # Ubuntu 22.04 installation & troubleshooting guide
│   ├── sdn_layer.md                       # OpenFlow 1.3 flow table spec & state machine diagrams
│   ├── test_cases.md                      # Verification test plan & results across Roles 1, 2, 3
│   ├── network_interface_for_role3.md     # REST API, CLI & CSV interface reference
│   ├── viva_role2.md                      # 28 viva Q&As, basics guide & 5-minute demo script
│   ├── report.md                          # Complete final project report
│   └── guide/                             # University project guide & rubrics documents
└── results/                               # Generated CSV telemetry & publication-ready graphs
```

---

## Team Roles & Contributions

| Role | Member Focus | Core Files |
| :--- | :--- | :--- |
| **Role 1** | **UDP Socket Application Owner** | `src/timesync_core.py`, `src/test_multiclient.py` |
| **Role 2** | **Mininet Topology & SDN Controller Owner** | `controller/sync_controller.py`, `topology/topo.py`, `scripts/*`, `tests/*` |
| **Role 3** | **Load Generator, Automation & Evaluation Owner** | `src/loadgen.py`, `experiments/run_experiments.sh`, `experiments/analyze.py`, `docs/report.md` |

---

## Quick Start Guide

### 1. Local Socket Convergence & DSCP Test (Runs on Windows / macOS / Linux)
```bash
python3 src/test_multiclient.py
```

### 2. Full Mininet + SDN Environment Setup (Ubuntu 22.04 LTS / WSL2)
```bash
# Install Mininet, Open vSwitch, iperf3, and Ryu 4.34 virtualenv
sudo bash scripts/install.sh
source ~/ryu-venv/bin/activate

# Start Ryu Controller + Mininet Topology + OVS HTB Queues
sudo bash scripts/run_all.sh
```

### 3. Run Automated SDN Integration Tests (8 Checks)
```bash
sudo bash tests/test_network.sh
```

### 4. Run Full Benchmark Suite (E1–E6 with 3, 5, 10 Clients) & Generate Graphs
```bash
sudo bash experiments/run_experiments.sh
python3 experiments/analyze.py --results-dir results/
```

---

## Default Network & Protocol Parameters

| Parameter | Value |
| :--- | :--- |
| **Time Server Address** | `10.0.0.100` (`srv` on switch `s2`), UDP Port `9999` |
| **Sync Traffic Marking** | `DSCP 46` (Expedited Forwarding / EF), `IP_TOS = 0xB8` (`184`) |
| **Background Load Traffic** | `lg` (`10.0.0.200`) $\to$ `ls` (`10.0.0.201`), UDP Port `5201`, `DSCP 0` |
| **Inter-Switch Links** | Primary: `s1-eth1 <-> s2-eth1` (Port 1, `10 Mbps`, `5ms` delay)<br>Backup: `s1-eth2 <-> s2-eth2` (Port 2, `10 Mbps`, `5ms` delay) |
| **OpenFlow Controller** | `127.0.0.1:6653` (OpenFlow 1.3), REST API on `http://127.0.0.1:8080` |
| **OVS Egress HTB Queues** | `Queue 1`: Sync Priority (`8–10 Mbps`)<br>`Queue 2`: Normal Background (`2–10 Mbps`)<br>`Queue 3`: Throttled Background (`0.5–2 Mbps`) |
