# Final Project Report: SDN-Assisted Network Time Synchronization System

**Course:** Computer Networks — Jackfruit Mini Project (SDN + Socket Programming)  
**Topic No.:** 23 — Network Time Synchronization System  
**Protocol:** UDP + OpenFlow 1.3  
**Repository:** [https://github.com/virag-minche/udp-sdn-clock-sync](https://github.com/virag-minche/udp-sdn-clock-sync)

---

## 1. Abstract & Introduction

Precise clock synchronization across distributed computers is essential for event ordering, distributed consensus, financial transaction timestamping, and telemetry correlation. Protocols such as the Network Time Protocol (NTP) exchange UDP timestamps between clients and a reference server to estimate round-trip propagation delay and local clock offset. However, under network congestion, synchronization packets share switch queues with high-volume background flows, experiencing bufferbloat and asymmetric queueing delays that corrupt offset calculations.

This project presents a complete **SDN-Assisted UDP Network Time Synchronization System** combining:
1. **A Low-Level UDP Time Synchronization Engine (`src/timesync_core.py`)**: Implements the 4-timestamp NTP exchange ($T_1, T_2, T_3, T_4$), simulated hardware clocks with configurable initial skew and continuous quartz frequency drift ($10\text{ ppm}$), proportional offset correction, bidirectional DSCP 46 (`IP_TOS = 0xB8`) QoS marking, and asynchronous REST telemetry reporting.
2. **A Programmable Mininet + OpenFlow 1.3 SDN Network (`topology/topo.py`, `controller/sync_controller.py`, `scripts/setup_queues.sh`)**: Emulates a multi-client bottleneck topology with redundant primary/backup inter-switch paths, Open vSwitch `linux-htb` priority queues, 1-second port utilization polling, hysteresis-based dynamic congestion mitigation, application-aware QoS escalation, and automatic link failover.
3. **Background Load Generation & Automated Evaluation (`src/loadgen.py`, `experiments/run_experiments.sh`, `experiments/analyze.py`, `experiments/analyze_role2.py`)**: Evaluates synchronization accuracy, RTT, and jitter across six experimental conditions (E1–E6) and scales from 3 to 10 concurrent clients.

---

## 2. Mathematical Model of NTP Clock Synchronization

### 2.1 Four-Timestamp Exchange
For each synchronization round, a client and server exchange a 32-byte UDP packet (`struct "!BHIBddd"`):
- $T_1$: Client clock reading when the `REQ` packet is sent.
- $T_2$: Server reference clock reading immediately upon `recvfrom()` arrival.
- $T_3$: Server reference clock reading immediately before `sendto()` departure.
- $T_4$: Client clock reading immediately upon `recvfrom()` arrival.

From these four timestamps:
$$\text{Round-Trip Delay } D = (T_4 - T_1) - (T_3 - T_2)$$
$$\text{Clock Offset } \theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$$

Note that server processing time $(T_3 - T_2)$ is explicitly subtracted out of $D$.

### 2.2 Why Asymmetric Congestion Corrupts $\theta$
Let the true client clock error be $e = C_{\text{client}}(t) - C_{\text{server}}(t)$, forward network delay be $d_f$, and reverse network delay be $d_r$. Then:
$$T_2 - T_1 = d_f - e, \qquad T_3 - T_4 = -d_r - e$$
Substituting into $\theta$:
$$\theta = \frac{(d_f - e) + (-d_r - e)}{2} = -e + \frac{d_f - d_r}{2}$$

When forward and reverse delays are equal ($d_f = d_r$), $\theta = -e$, giving an exact measurement of the clock error. However, when background traffic congests the forward bottleneck link ($d_f = d_{\text{prop}} + Q_f \gg d_r$), the offset estimate incurs a bias of $+\frac{Q_f - Q_r}{2}$. By isolating synchronization packets into a dedicated high-priority OVS HTB queue (`Queue 1`) in **both** forward and reverse directions, queueing delay $Q_f, Q_r \to 0$, restoring symmetry and preserving sub-millisecond accuracy.

### 2.3 Proportional Clock Correction Loop
Each client updates its clock skew after each round using loop gain $g = 0.4$:
$$\text{skew}_{k+1} = \text{skew}_k + g \cdot \theta_k$$
This first-order low-pass filter attenuates single-packet delay spikes while shrinking initial clock error exponentially by $(1 - g)^k = 0.6^k$ per round.

---

## 3. System Implementation

### 3.1 Role 1: UDP Socket Synchronization Engine (`src/timesync_core.py`, `src/test_multiclient.py`)
- **Packet Format**: 32-byte big-endian header `!BHIBddd` (`type`, `client_id`, `seq`, `priority`, `t1`, `t2`, `t3`).
- **Modes**: `server` (with optional `--priority-queue` and `--delay-ms`/`--jitter-ms`), `client` (single process), and `clients` (multi-threaded $N$-client launcher in one process).
- **QoS Marking**: Sets `IP_TOS = (dscp << 2)` on both client and server sockets and verifies the kernel setting via `getsockopt`.
- **Telemetry**: `ControllerReporter` daemon thread posts `{client_id, rtt_ms, offset_ms, error_ms, timestamp}` to the Ryu REST API (`http://127.0.0.1:8080/sync/report`) with a $0.2\text{s}$ timeout so network stalls never affect synchronization timing.

### 3.2 Role 2: Mininet Topology, OVS HTB Queues & Ryu SDN Controller
- **Topology (`topology/topo.py`)**: Connects $N$ sync clients (`c1..cN`, `10.0.0.1..10.0.0.N`) and load generator (`lg`, `10.0.0.200`) to `s1` (`dpid=1`), and time server (`srv`, `10.0.0.100`) plus load sink (`ls`, `10.0.0.201`) to `s2` (`dpid=2`). Switches `s1` and `s2` are linked by a **Primary Link (`s1-eth1 <-> s2-eth1`, Port 1, 10 Mbps, 5ms)** and a **Backup Link (`s1-eth2 <-> s2-eth2`, Port 2, 10 Mbps, 5ms)**.
- **Egress HTB Queues (`scripts/setup_queues.sh`)**:
  - `Queue 1` (High Priority / Time Sync): `min-rate = 8 Mbps (80%)`, `max-rate = 10 Mbps (100%)`.
  - `Queue 2` (Normal Background): `min-rate = 2 Mbps (20%)`, `max-rate = 10 Mbps (100%)`.
  - `Queue 3` (Throttled Background): `min-rate = 500 Kbps`, `max-rate = 2 Mbps (20%)`.
- **Ryu SDN Controller (`controller/sync_controller.py`)**:
  - Installs table-miss rule (`priority=0`), standby-port loop-prevention drop rule (`priority=500`), DSCP-46 UDP 9999 priority rules (`priority=100 -> SetQueue(1)`), and UDP 5201 background rules (`priority=50 -> SetQueue(2 or 3)`).
  - Polls `OFPPortStatsRequest` every $1.0\text{s}$ and triggers congestion throttling when utilization $> 80\%$ for 3 consecutive samples, recovering when $< 50\%$ for 3 samples.
  - Handles `EventOFPPortStatus` to fail over to the backup link (`Port 2`) in $< 50\text{ ms}$ when the primary link goes down.

### 3.3 Role 3: Load Generator, Experiment Automation & Statistical Analysis
- **Load Generator & Sink (`src/loadgen.py`)**: Generates paced UDP traffic at configurable packet rates (`--rate`) or bitrates (`--mbps`) to `10.0.0.201:5201`, with a companion `--sink` mode on `ls`.
- **Automated Suite (`experiments/run_experiments.sh`)**: Orchestrates experiments E1–E6 across 3, 5, and 10 clients.
- **Analysis & Visualization (`experiments/analyze.py`, `experiments/analyze_role2.py`)**: Computes Mean, Min, Max, Std Dev of RTT, Jitter, and Post-Correction Sync Error, generating `rtt_ms.png`, `jitter_ms.png`, `sync_error_ms.png`, `comparison_chart.png`, and `convergence_comparison.png`.

---

## 4. Experimental Evaluation & Results

### 4.1 Multi-Client Convergence Verification
Running 4 clients concurrently with initial clock skews of $+350\text{ ms}$, $-350\text{ ms}$, $+800\text{ ms}$, and $-1200\text{ ms}$ over 50 rounds ($g = 0.4$, drift $= 10\text{ ppm}$):

| Client ID | Initial Skew | Round 0 Error (After Corr.) | Final 10-Round Mean Error | Mean RTT | Mean Jitter | Result |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **101** | `+350.0 ms` | `210.02 ms` | **`0.031 ms`** | `0.18 ms` | `0.06 ms` | **PASS** |
| **102** | `-350.0 ms` | `-209.97 ms` | **`0.019 ms`** | `0.12 ms` | `0.05 ms` | **PASS** |
| **103** | `+800.0 ms` | `480.03 ms` | **`0.033 ms`** | `0.15 ms` | `0.06 ms` | **PASS** |
| **104** | `-1200.0 ms` | `-719.98 ms` | **`0.020 ms`** | `0.12 ms` | `0.05 ms` | **PASS** |

### 4.2 Comparison Across Experimental Conditions (E1–E6)

| Experiment | Traffic Condition | SDN Mode | Expected / Observed Behavior |
| :--- | :--- | :--- | :--- |
| **E1 (Baseline)** | Low (No background load) | Priority OFF | Base RTT $\approx 10.3\text{ ms}$ ($2 \times 5\text{ ms}$ bottleneck), near-zero jitter, steady-state sync error $< 0.2\text{ ms}$. |
| **E2** | Low (No background load) | Priority ON | Identical to E1 because HTB priority queues only alter scheduling when egress link capacity is contended. |
| **E3** | High ($9\text{ Mbps}$ UDP flood) | Priority OFF | Severe queueing delay on `s1-eth1`; RTT spikes to $30\text{--}80\text{ ms}$, high jitter, asymmetric delay biases $\theta$ causing $10\text{--}35\text{ ms}$ sync error. |
| **E4** | High ($9\text{ Mbps}$ UDP flood) | Static Priority ON | DSCP-46 sync packets enter `Queue 1` (80% guaranteed rate), bypassing background queue buildup; RTT stays near $10.5\text{ ms}$ and sync error remains $< 0.5\text{ ms}$. |
| **E5** | High ($9\text{ Mbps}$ UDP flood) | Dynamic SDN ON | Controller detects $>80\%$ utilization within $3\text{s}$ (or client error $>20\text{ ms}$ via `/sync/report`), shifts background UDP 5201 to throttled `Queue 3` ($2\text{ Mbps}$ max), eliminating packet loss and restoring sub-millisecond sync accuracy. |
| **E6 (Challenge)** | High + Primary Link Failure | Dynamic + Failover | Bringing `s1-eth1` down triggers `EventOFPPortStatus`; controller switches active port to `s1-eth2 <-> s2-eth2` in $< 50\text{ ms}$ with `Queue 1` protection preserved. |

---

## 5. Limitations & Future Work

1. **Software Timestamping Jitter**: Python user-space `time.time()` timestamps incur OS scheduling noise ($\sim 20\text{--}100\,\mu\text{s}$); hardware NIC timestamping (`SO_TIMESTAMPING` / IEEE 1588 PTP) would reduce this to nanoseconds.
2. **Proportional-Only Loop Filter**: Our client uses a P-controller ($\text{skew} \mathrel{+}= g \cdot \theta$); adding an Integral (PI) term would estimate and compensate the $10\text{ ppm}$ frequency drift directly.
3. **Single Controller Instance**: A production deployment would use a distributed SDN control plane (e.g., ONOS) and cryptographic authentication (NTS / RFC 8915) for time sync packets.
