# Comprehensive Test Cases & Verification Document

**Course:** Computer Networks — Jackfruit Mini Project (SDN + Socket Programming)  
**Topic No.:** 23 — Network Time Synchronization System  

---

## 1. Socket Application Test Cases (Role 1)

| Test ID | Test Case | Command | Expected Output / Pass Criterion | Status |
| :--- | :--- | :--- | :--- | :--- |
| **TC-S1** | **Multi-Client Clock Convergence** | `python3 src/test_multiclient.py` | 4 concurrent clients with initial skews (`+0.35s`, `-0.35s`, `+0.80s`, `-1.20s`) converge to mean absolute error $< 1.0\text{ ms}$ over final 10 rounds (`0.02–0.04 ms` observed). | **PASS** |
| **TC-S2** | **DSCP Socket Marking & Readback** | `python3 src/test_multiclient.py` (Test 2) | `setsockopt(IPPROTO_IP, IP_TOS, dscp << 2)` for DSCP `0` (`0x00`) and DSCP `46` (`0xB8`) reads back exact matching TOS byte via `getsockopt`. | **PASS** |
| **TC-S3** | **Multi-Threaded Single-Process Mode (`clients`)** | `python3 src/timesync_core.py clients --n 3 --rounds 15` | Spawns 3 independent client threads with separate UDP sockets and CSV logs; all converge by factor $(1 - \text{gain}) = 0.6$ per round. | **PASS** |
| **TC-S4** | **Server Priority Queue & Return-Path Delay** | `python3 src/timesync_core.py server --priority-queue --delay-ms 15 --jitter-ms 5` | Server queues requests by priority and injects artificial delay strictly after $T_3$, increasing measured RTT by $\sim 15\text{ ms}$. | **PASS** |
| **TC-S5** | **Non-Blocking Controller Telemetry** | `python3 src/timesync_core.py client --controller http://127.0.0.1:8080/sync/report` | Background daemon thread sends JSON metrics (`rtt_ms`, `offset_ms`, `error_ms`); if controller is offline, logs one warning and never blocks sync loop. | **PASS** |

---

## 2. Mininet & SDN Layer Test Cases (Role 2)

Executed automatically via `sudo python3 tests/test_network.py` (or `sudo bash tests/test_network.sh`):

| Test ID | Test Case | Verification Mechanism | Pass Criterion | Status |
| :--- | :--- | :--- | :--- | :--- |
| **TC-N1** | **End-to-End Host Connectivity** | `pingAll` between `c1..cN`, `lg`, `srv`, `ls` | $0\%$ packet loss across all hosts; backup inter-switch port blocked to prevent L2 broadcast loops. | **PASS** |
| **TC-N2** | **OpenFlow 1.3 Flow Rule Installation** | `sudo ovs-ofctl dump-flows s1 -O OpenFlow13` | Table-miss rule (priority `0`), blocked standby port drop rule (priority `500`), DSCP-46 sync rule (`ip_dscp=46,udp,tp_dst=9999 -> set_queue:1`, priority `100`), and background rule (`udp,tp_dst=5201 -> set_queue:2`, priority `50`) present. | **PASS** |
| **TC-N3** | **OVS HTB Queue Configuration** | `sudo ovs-vsctl list qos` & `tc -s class show dev s1-eth1` | `linux-htb` QoS configured on egress bottleneck interfaces (`s1-eth1`, `s2-eth1`) with Queue 1 (min 80% BW), Queue 2 (min 20% BW), and Queue 3 (max 20% BW throttled). | **PASS** |
| **TC-N4** | **Priority Queueing Under Congestion** | Compare sync RTT/error with `src/loadgen.py --mbps 9` active: Priority OFF vs Priority ON | With Priority ON, DSCP-46 packets bypass Queue 2/3 congestion and maintain low RTT jitter and sub-millisecond sync error. | **PASS** |
| **TC-N5** | **Dynamic Congestion Detection & Mitigation** | Poll `/sdn/status` and `results/controller_log.csv` during 9 Mbps flood | When bottleneck utilization exceeds $80\%$ for 3 consecutive 1-second samples, controller logs `CONGESTION_DETECTED` and moves background traffic to throttled `Queue 3`; recovers below $50\%$. | **PASS** |
| **TC-N6** | **Controller REST API Endpoints** | `GET /sdn/status`, `POST /sdn/mode`, `POST /sync/report` | HTTP 200 JSON responses; runtime toggling of `priority` and `dynamic` modes cleanly reinstalls switch flows. | **PASS** |
| **TC-N7** | **Primary Link Failure & Automatic Failover** | Bring `s1-eth1` down (`ip link set s1-eth1 down`) | `EventOFPPortStatus` triggers `LINK_FAILOVER` in $< 50\text{ ms}$, unblocks `s1-eth2 <-> s2-eth2` backup path, and restores connectivity; failback occurs when `s1-eth1` returns up. | **PASS** |
| **TC-N8** | **Wire-Level DSCP Verification (`tcpdump`)** | `sudo tcpdump -i s1-eth1 -v -c 5 'udp port 9999 and (ip[1] & 0xfc) == 0xb8'` | Captures live UDP 9999 frames on wire with IPv4 header `tos 0xb8` (DSCP 46 EF). | **PASS** |

---

## 3. Load Generation & Analysis Test Cases (Role 3)

| Test ID | Test Case | Command | Expected Output / Pass Criterion | Status |
| :--- | :--- | :--- | :--- | :--- |
| **TC-E1** | **UDP Load Generator & Sink** | `python3 src/loadgen.py --sink --port 5201` & `python3 src/loadgen.py --host 127.0.0.1 --port 5201 --mbps 5 --duration 3` | Sender paces UDP frames at target bitrate; sink receives packets without ICMP Port Unreachable errors and reports received Mbps. | **PASS** |
| **TC-E2** | **Role 3 Statistical & Graph Generator** | `python3 experiments/analyze.py --results-dir results/` | Computes Mean, Min, Max, Std Dev for `rtt_ms`, `jitter_ms`, and `sync_error_ms`; saves `results/rtt_ms.png`, `results/jitter_ms.png`, `results/sync_error_ms.png`, and `results/experiment_summary.csv`. | **PASS** |
| **TC-E3** | **Cross-Experiment Comparison Plotter** | `python3 experiments/analyze_role2.py --results-dir results/` | Generates `results/comparison_chart.png` and `results/convergence_comparison.png` with summary table. | **PASS** |
