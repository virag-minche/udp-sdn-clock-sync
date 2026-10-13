# Viva Examination & Defense Guide: SDN Controller & Time Synchronization

This document is prepared for undergraduate Computer Networks viva voce examination, project defense, and technical walkthroughs. It connects theoretical concepts directly to implementation details, formulas, and code references.

---

# Section 1: From the Basics (Plain-Language Explanations)

### 1. Control Plane vs. Data Plane
* **The Analogy (GPS Navigation vs. Roads)**:
  * **Data Plane**: Think of the physical highway system, road intersections, and traffic lights. Cars (data packets) physically travel along asphalt from point A to point B. The road sign at an intersection executes a simple rule: "Turn left for Airport".
  * **Control Plane**: Think of Waze or Google Maps running in the cloud. It monitors global traffic conditions, determines optimal routes, and signals the driver/signs where to go.
  * In traditional networking, every router has its own little brain (control plane and data plane combined inside each box). In **SDN**, we physically extract the brain out of the switches and centralize it into a software program called the **SDN Controller** (Ryu). The switches (Open vSwitch) become fast, programmable forwarding engines (the data plane) that only execute flow table instructions programmed by the controller.

### 2. OpenFlow Protocol Overview
OpenFlow is the standardized south-bound communication protocol between the SDN controller and OpenFlow switches over a secure TCP channel (default port 6653). Essential message types include:
* **`OFPT_PACKET_IN` (Packet-In)**: Sent by the switch to the controller when a packet arrives at an interface that does not match any entry in its flow table (or explicitly matches a table-miss rule).
* **`OFPT_FLOW_MOD` (Flow-Mod)**: Sent by the controller to the switch to add, modify, or delete entries in the switch's flow tables.
* **`OFPT_PORT_STATS_REQUEST` & `OFPT_PORT_STATS_REPLY`**: Sent periodically by the controller to poll transmit/receive byte and packet counters from specific physical or virtual switch ports.
* **`OFPT_PORT_STATUS`**: An asynchronous notification sent immediately by the switch to the controller whenever an interface link goes physically `UP` or `DOWN` or administrative configurations change.

### 3. Flow Table Structure
Every OpenFlow switch maintains one or more flow tables. An individual flow entry contains five key components:
1. **Match Fields**: Header fields against which incoming packets are compared (e.g., ingress port, source MAC, destination MAC, IP protocol, UDP port, IP DSCP).
2. **Priority**: A 16-bit integer (0 to 65535). When a packet matches multiple flow entries, the rule with the highest priority is strictly executed.
3. **Actions / Instructions**: Operations applied to matching packets (e.g., `OUTPUT:port`, `SET_QUEUE:1`, `DROP`, `SET_FIELD`).
4. **Counters**: Maintained by switch hardware/kernel to track how many packets and bytes have matched this rule.
5. **Timeouts**: `idle_timeout` (expires if no packets match within $X$ seconds) and `hard_timeout` (expires strictly after $Y$ seconds regardless of traffic).

### 4. Packet-In Event
When an unknown frame enters an Open vSwitch port:
1. The hardware/kernel datapath cannot match any existing entry in the flow tables.
2. The packet hits the lowest priority catch-all rule (priority 0).
3. The switch encapsulates the frame (or its header) in an `OFPT_PACKET_IN` message and dispatches it over TCP to Ryu.
4. Ryu's `@set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)` handler extracts the ingress port, source MAC, and destination MAC. Ryu learns that the source host resides on that ingress port, and instructs the switch to either flood the frame or install a direct forwarding rule.

### 5. Table-Miss Rule
* A wildcard rule with **Priority 0** matching any packet (`match = parser.OFPMatch()`).
* **Action**: `actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]`.
* Without an explicit table-miss rule in OpenFlow 1.3, unhandled packets would be silently dropped by default.

### 6. DSCP vs. TOS Byte Layout
* In IPv4, byte 1 of the header is historically named the **Type of Service (TOS)** field.
* Under modern DiffServ architecture (RFC 2474), the TOS byte is partitioned into:
  * **Bits 0–5 (Upper 6 bits)**: Differentiated Services Code Point (**DSCP**).
  * **Bits 6–7 (Lower 2 bits)**: Explicit Congestion Notification (**ECN**).
* To set DSCP 46 (**Expedited Forwarding**, binary `101110`, hex `0x2E`):
  * Shift left by 2 bits to occupy the upper 6 bits of the TOS byte:
    $$\text{TOS Byte} = \text{DSCP} \ll 2 = 46 \times 4 = 184 = \text{0xB8}$$
  * In Python socket programming:
    ```python
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 184)
    ```

### 7. Hierarchical Token Bucket (HTB) Queues
* Linux Traffic Control (`tc`) uses the Hierarchical Token Bucket queuing discipline to enforce rate limiting and class-based scheduling.
* Tokens accumulate at a specified rate (link bandwidth). A packet can only depart if sufficient tokens are available in the bucket.
* In our OVS setup:
  * **Queue 0 (Default / Bulk Traffic)**: Limited to 9 Mbps with a large buffer. Under congestion, excess bulk packets wait in the buffer, suffering large queuing latency or buffer drops.
  * **Queue 1 (High Priority / Sync Traffic)**: Strictly guaranteed 1 Mbps with a minimal buffer limit. Sync packets bypass Queue 0 completely, experiencing zero buffer-bloat delay.

### 8. Why Priority Helps ONLY on Egress and ONLY Under Congestion
* **Ingress is Already in Memory**: Once an Ethernet frame hits an incoming network card, it has already traversed the physical wire. Prioritizing it at ingress cannot undo propagation or transmission delays already incurred.
* **Under Low Load**: If a 10 Mbps link carries only 1 Mbps of total traffic, the output buffer is empty 90% of the time. Every packet is transmitted immediately. Both Queue 0 and Queue 1 packets experience the same latency.
* **Under Congestion**: When traffic generation exceeds 10 Mbps, packets accumulate in switch buffers. Without priority, time sync packets sit behind hundreds of bulk data packets (buffer bloat). With egress priority scheduling, Queue 1 packets jump straight to the head of the output link transmitter.

### 9. Why Asymmetric Delay Breaks NTP Offset
The fundamental NTP offset estimation formula is:
$$\text{Offset } \theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$$
Let $d_{\text{fwd}}$ be the forward transit time ($T_2 - T_1$) and $d_{\text{rev}}$ be the reverse transit time ($T_4 - T_3$).
If both directions have identical delays ($d_{\text{fwd}} = d_{\text{rev}}$), the transmission delays cancel out perfectly, yielding the true clock offset:
$$\theta_{\text{estimated}} = \theta_{\text{true}}$$
However, if asymmetric congestion causes $d_{\text{fwd}} \neq d_{\text{rev}}$:
$$\theta_{\text{estimated}} = \theta_{\text{true}} + \frac{d_{\text{fwd}} - d_{\text{rev}}}{2}$$
The estimated offset is biased by exactly half of the difference between forward and reverse transit times! This is why priority queueing must be symmetrically applied to **both** request packets and server response packets.

---

# Section 2: 28 Likely Viva Questions with Model Answers

#### Q1: Why UDP instead of TCP for time synchronization?
* **Answer**: TCP introduces connection handshakes (3-way handshake), cumulative acknowledgments, and retransmission timeouts (RTO) upon packet drops. If a sync packet is dropped or delayed, TCP buffers data and retransmits it later, causing unpredictable and highly variable latency that ruins microsecond-precision clock estimation. UDP is connectionless and lightweight; an old, lost time packet is discarded because a new timestamp is always vastly more valuable than a retransmitted stale timestamp.
* *Code Reference*: `src/client.py` (`socket.socket(socket.AF_INET, socket.SOCK_DGRAM)`), `src/server.py`.

#### Q2: Explain T1, T2, T3, T4 and derive the NTP offset formula.
* **Answer**:
  * $T_1$: Timestamp when client transmits request (client's local clock).
  * $T_2$: Timestamp when server receives request (server's reference clock).
  * $T_3$: Timestamp when server sends reply (server's reference clock).
  * $T_4$: Timestamp when client receives reply (client's local clock).
  * Let $\theta$ be client clock offset relative to server ($T_{\text{client}} = T_{\text{server}} + \theta$).
  * Forward delay: $d_{\text{fwd}} = (T_2 - \theta) - T_1 \implies T_2 - T_1 = d_{\text{fwd}} + \theta$.
  * Return delay: $d_{\text{rev}} = T_4 - (T_3 - \theta) \implies T_4 - T_3 = d_{\text{rev}} - \theta$.
  * Subtracting the two equations: $(T_2 - T_1) - (T_4 - T_3) = (d_{\text{fwd}} - d_{\text{rev}}) + 2\theta$.
  * Assuming symmetric delay ($d_{\text{fwd}} \approx d_{\text{rev}}$):
    $$\theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$$
* *Code Reference*: `src/sync_protocol.py` in function `calculate_offset_and_delay(t1, t2, t3, t4)`.

#### Q3: What is the round-trip delay formula and why does it cancel server processing time?
* **Answer**:
  $$\text{Delay } \delta = (T_4 - T_1) - (T_3 - T_2)$$
  $(T_4 - T_1)$ measures the total elapsed time on the client's clock between transmission and reception. $(T_3 - T_2)$ measures the server's internal software processing and turnaround time. By subtracting $(T_3 - T_2)$, the server's dwell time is eliminated, isolating purely the two-way network transit delay.
* *Code Reference*: `src/sync_protocol.py` in `calculate_offset_and_delay()`.

#### Q4: What happens if forward and return delays are different?
* **Answer**: As demonstrated in Section 1.9, an asymmetric network delay creates a systematic error bias equal to $\frac{d_{\text{fwd}} - d_{\text{rev}}}{2}$. If forward congestion adds 20 ms of queueing delay while the return path has 0 ms queueing, the client clock will calculate an offset shifted by $+10\text{ ms}$, incorrectly adjusting its local clock.
* *Code Reference*: `sdn/controller.py` installs matching Queue 1 priority rules on both Switch 1 and Switch 2 to preserve forward and reverse path symmetry.

#### Q5: How does the Clock class simulate a wrong clock?
* **Answer**: The `Clock` class maintains internal variables: `base_offset` (an initial static error in seconds, e.g., $+2.5\text{ s}$) and `drift_rate` (oscillator drift in parts per million or seconds/second, e.g., $+50\,\mu\text{s/s}$). When `clock.now()` is queried, it computes:
  $$\text{simulated\_time} = \text{system\_time} + \text{current\_offset} + (\text{drift\_rate} \times \Delta t)$$
  This accurately models real quartz oscillator inaccuracies without altering the host OS system clock.
* *Code Reference*: `src/clock.py` in `Clock.now()` and `Clock.adjust()`.

#### Q6: What does gain=0.4 mean and why not gain=1.0?
* **Answer**: The clock servo uses an exponential moving average / proportional filter:
  $$\text{new\_offset} = \text{current\_offset} - (\text{gain} \times \theta)$$
  A gain of $0.4$ acts as a low-pass filter, absorbing high-frequency network jitter and transient spikes over multiple cycles. If `gain=1.0` were used, any momentary delay spike or packet jitter would be applied 100% directly to the clock, causing clock overshoots, instability, and oscillation.
* *Code Reference*: `src/client.py` in `SyncClient.step()`.

#### Q7: How does the server mark T2 and T3 to minimize processing delay?
* **Answer**: In `src/server.py`, $T_2$ is captured immediately after the blocking UDP socket `recvfrom()` call returns: `t2 = time.time()`. The server prepares the payload, captures $T_3$ right before transmitting: `t3 = time.time()`, and immediately calls `sock.sendto()`. This ensures the difference $(T_3 - T_2)$ represents minimal internal operating system overhead.
* *Code Reference*: `src/server.py` in `TimeServer.handle_request()`.

#### Q8: What is DSCP 46 and how does the switch use it?
* **Answer**: DSCP 46 is the Expedited Forwarding (EF) PHB. The switch flow table has a flow rule matching IPv4 packets with IP protocol 17 (UDP) and `ip_dscp=46`. When matched, the action `set_queue:1` directs the packet into the hardware/kernel priority queue (Queue 1) on the egress port.
* *Code Reference*: `sdn/controller.py` in `add_sync_flow()`.

#### Q9: What flow rules does your controller install?
* **Answer**:
  1. Priority 0: Table-miss sending unknown frames to Ryu.
  2. Priority 10: ARP broadcast forwarding.
  3. Priority 20: L2 learned unicast forwarding.
  4. Priority 100: Time sync request/reply matching `ip_dscp=46` and port 5005 mapped to `SET_QUEUE:1`.
  5. Priority 150: Dynamic throttle rule routing bulk UDP to Queue 0 when utilization exceeds 80%.
  6. Priority 200: Link failover reroute redirecting flows to backup ports when primary link is down.
* *Code Reference*: `sdn/controller.py` in `install_static_flows()` and `_port_status_handler()`.

#### Q10: How does your controller detect congestion?
* **Answer**: A periodic background greenlet sends `OFPPortStatsRequest` every 1 second to inspect the bottleneck egress port. It computes the delta in `tx_bytes`:
  $$\text{Utilization} = \frac{(\text{tx\_bytes}_{\text{now}} - \text{tx\_bytes}_{\text{prev}}) \times 8}{1.0 \times 10{,}000{,}000} \times 100\%$$
* *Code Reference*: `sdn/controller.py` in `_monitor_port_stats()`.

#### Q11: What is the hysteresis mechanism and why is it needed?
* **Answer**: Hysteresis prevents rapid cycling or "flapping" between normal and throttled states caused by momentary bursts. A dual-threshold rule is applied: congestion triggers at $>80\%$ utilization (requiring 2 consecutive samples), but recovery requires utilization to drop below $<60\%$ (for 3 consecutive samples).
* *Code Reference*: `sdn/controller.py` in `check_congestion_state()`.

#### Q12: How do OVS queues work? What is HTB?
* **Answer**: Open vSwitch binds to Linux `tc-htb`. The physical/virtual port is configured with an HTB qdisc having root bandwidth (10 Mbps). Child classes are created: Class `1:1` (Queue 0: default, max 9 Mbps) and Class `1:2` (Queue 1: priority, min 1 Mbps, strict low latency). The OpenFlow action `set_queue:X` sets the packet's `skb->priority`, steering it to that HTB class.
* *Code Reference*: `mininet/topology.py` in `setup_ovs_queues()`.

#### Q13: What happens when the primary link fails?
* **Answer**: OVS sends an `OFPT_PORT_STATUS` message with `OFPPR_MODIFY` and `OFPPS_LINK_DOWN`. Ryu's port status handler catches this, flushes flows directing traffic to the primary port, and installs Priority 200 flows diverting traffic to the standby backup link (s1-eth6 / s2-eth4).
* *Code Reference*: `sdn/controller.py` in `_port_status_handler()`.

#### Q14: How does the backup path avoid loops?
* **Answer**: Under normal operation, the backup link interfaces are kept inactive or have no installed flow forwarding rules (any packet entering the backup port is dropped or unmapped). When failover activates, primary flows are revoked before backup flows are injected, ensuring strictly one active acyclic forwarding path at any instant.
* *Code Reference*: `sdn/controller.py` in `activate_backup_path()`.

#### Q15: What REST endpoints does your controller expose?
* **Answer**:
  * `GET /sdn/status`: Returns current switch stats, link utilization %, and active path (PRIMARY vs BACKUP).
  * `POST /sdn/mode`: Accepts JSON `{"priority": "on"|"off"}` or `{"dynamic": "on"|"off"}` to toggle QoS behaviors dynamically during experiments.
  * `POST /sdn/report`: Ingests real-time telemetry from sync clients (client ID, current offset, delay, sync state).
* *Code Reference*: `sdn/rest_api.py` in `SDNRestController`.

#### Q16: How does the client report to the controller?
* **Answer**: After calculating offset and RTT, the client spawns a background non-blocking HTTP worker that issues a `POST` request to `http://127.0.0.1:8080/sdn/report` with a payload of `{"client_id": 1, "offset": -0.0012, "delay": 0.0105}` and a strict 200 ms timeout.
* *Code Reference*: `src/client.py` in `SyncClient.send_telemetry()`.

#### Q17: What is a table-miss rule?
* **Answer**: A wildcard match rule at priority 0 that instructs the switch to send all un-matched packets to the SDN controller via `OFPP_CONTROLLER` in an `OFPT_PACKET_IN` message.
* *Code Reference*: `sdn/controller.py` in `switch_features_handler()`.

#### Q18: Why use OpenFlow 1.3 specifically?
* **Answer**: OpenFlow 1.0 lacked support for multiple flow tables, group tables, explicit IPv6/DSCP matching, and standardized queue/meter instructions. OpenFlow 1.3 is the universally accepted stable Long-Term Support (LTS) version supported natively across Linux Open vSwitch kernel modules.
* *Code Reference*: `sdn/controller.py` (`OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]`).

#### Q19: How do you verify DSCP marking on the wire?
* **Answer**: Using `tcpdump` on the Mininet host interface:
  ```bash
  sudo tcpdump -i s1-eth5 -v -n "udp port 5005"
  ```
  Look for the IP header field: `tos 0xb8` or `diffserv: 46 (EF)`. Alternatively, inspect OVS queue counters: `ovs-ofctl queue-stats s1 5`.
* *Code Reference*: `scripts/verify_dscp.sh`.

#### Q20: What is the difference between E3 (no priority) and E4 (with priority)?
* **Answer**:
  * **E3 (No Priority)**: Under bulk iperf congestion, sync traffic shares Queue 0 with bulk packets. As queue buffers fill, sync packets experience severe queuing jitter ($50\text{--}100\text{ ms}$) and packet drops, degrading client clock offset accuracy.
  * **E4 (With Priority)**: Sync packets are tagged DSCP 46 and placed into Queue 1. They bypass the congested buffer completely, keeping transit delays constant ($RTT \approx 10\text{ ms}$) and clock synchronization error within sub-millisecond bounds.
* *Code Reference*: `experiments/run_experiments.py`.

#### Q21: What does the convergence graph show?
* **Answer**: It plots Client Clock Offset (y-axis, milliseconds) versus Time / Sync Cycles (x-axis). It shows an initial large error (e.g., $2500\text{ ms}$) exponentially decreasing across cycles until settling in a tight steady-state band ($\pm 0.5\text{ ms}$) around zero.
* *Code Reference*: `scripts/plot_results.py`.

#### Q22: Why does the server also mark replies with DSCP?
* **Answer**: If only the client tags the request with DSCP, the forward path is prioritized, but the return path (server to client) is queued behind bulk traffic on switch `s2`. This creates massive asymmetric delay ($d_{\text{fwd}} \ll d_{\text{rev}}$), which corrupts the offset calculation ($\frac{d_{\text{fwd}} - d_{\text{rev}}}{2}$). Both paths must be prioritized.
* *Code Reference*: `src/server.py` in `sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 184)`.

#### Q23: How does the priority queue mode in the server work?
* **Answer**: The server sets the IP TOS socket option to `184` before responding. When its outgoing packet enters switch `s2`, OpenFlow matches `udp_src=5005` and `ip_dscp=46`, executing `set_queue:1` onto bottleneck port `s2-eth3`.
* *Code Reference*: `src/server.py` and `sdn/controller.py`.

#### Q24: What is the `--delay-ms` option for?
* **Answer**: It specifies the artificial software delay introduced by the client between successive synchronization cycles (e.g., `--delay-ms 1000` means sending a sync request once every second).
* *Code Reference*: `src/client.py` main CLI argument parser.

#### Q25: How would you scale this to 100 clients?
* **Answer**:
  1. Increase sync intervals with randomized jitter to avoid synchronization query collisions.
  2. Use OpenFlow proactive flow rules instead of reactive packet-in learning to eliminate controller bottlenecks.
  3. Deploy hierarchical time servers (stratum architecture) and multi-threaded controllers.
* *Code Reference*: `docs/sdn_layer.md` Section 6.

#### Q26: What are the limitations of your system?
* **Answer**: Single-threaded Ryu controller event loop, symmetric delay assumption in NTP, lack of cryptographic authentication on UDP and REST endpoints, and reliance on software-emulated OVS queues rather than dedicated PTP hardware timestamping.
* *Code Reference*: `docs/sdn_layer.md` Section 6.

#### Q27: Why Ryu and not another controller?
* **Answer**: Ryu is 100% pure Python, highly readable, well-integrated with Python network scripts, and natively supports the full OpenFlow 1.3 specification with a clean event-driven plugin architecture.
* *Code Reference*: `sdn/controller.py`.

#### Q28: What is eventlet and why does Ryu need it?
* **Answer**: `eventlet` is a concurrent Python networking library that uses greenlets (cooperative green threads) to handle thousands of concurrent network I/O operations non-blockingly on a single OS thread. Ryu utilizes eventlet for its OpenFlow connection listeners and periodic polling timers.
* *Code Reference*: `sdn/controller.py` (`from ryu.lib import hub`).

---

# Section 3: 5-Minute Demonstration Script

| Time | Action (What to Run) | Talking Points (What to Say) | Screen Focus (What to Point At) |
| :--- | :--- | :--- | :--- |
| **0:00 - 0:45** | `ryu-manager sdn/controller.py`<br>`sudo python3 mininet/topology.py` | "We launch the Ryu OpenFlow 1.3 controller and initialize our two-switch Mininet topology with an active 10 Mbps bottleneck and standby backup link." | Point out Ryu listening on port 6653 and Mininet output showing switches `s1` and `s2` connected. |
| **0:45 - 1:30** | In Mininet CLI:<br>`mininet> pingall`<br>`sh ovs-ofctl dump-flows s1 -O OpenFlow13` | "We verify full L2 reachability. Now we dump flow rules: observe the Priority 0 table-miss rule and Priority 100 DSCP 46 rules directing time traffic to Queue 1." | Point to `ip_dscp=46`, `actions=set_queue:1` in the flow table dump. |
| **1:30 - 2:30** | In host windows:<br>`srv python3 src/server.py &`<br>`c1 python3 src/client.py --id 1 &` | "Sync Client 1 starts with a simulated offset of +2500 ms. Watch the terminal output: within 10 cycles, the servo pulls the offset down to sub-millisecond precision." | Point to the printed `Offset: 0.12ms, Delay: 10.2ms` in the client terminal. |
| **2:30 - 3:30** | Run bulk load:<br>`h_sink iperf3 -s &`<br>`h_bg iperf3 -c 10.0.0.101 -u -b 12M -t 30 &` | "We inject 12 Mbps UDP congestion over our 10 Mbps link. The Ryu controller detects utilization $>80\%$ and triggers dynamic hysteresis rules. Notice that client sync offset remains completely unaffected because sync packets jump Queue 0 into Queue 1." | Point to controller terminal showing: `[CONGESTION DETECTED] Bottleneck utilization: 96.4%` and client offset remaining $<1\text{ ms}$. |
| **3:30 - 4:15** | In Mininet CLI:<br>`mininet> link s1 s2 down` | "We physically take down the primary link. The switch fires an OpenFlow `PortStatus` event. Ryu immediately detects the drop, deletes primary rules, and installs Priority 200 backup flows via the 15 ms delay path without dropping synchronization." | Point to controller log: `[FAILOVER] Primary down. Switching to backup link.` Point to client delay increasing from 10 ms to 30 ms while offset remains stable. |
| **4:15 - 5:00** | Query REST & graphs:<br>`curl http://127.0.0.1:8080/sdn/status`<br>`python3 scripts/plot_results.py` | "Finally, we query the controller REST API showing real-time port metrics and open the generated convergence graph comparing E3 (no QoS) vs E4 (with SDN QoS)." | Point to JSON response and the divergence in the matplotlib graphs showing clear QoS superiority. |

---

# Section 4: Tricky Questions (Deep Technical Validation)

#### 1. What happens if the Open vSwitch disconnects and reconnects to Ryu mid-experiment?
* **Answer**: When an OVS switch reconnects, OpenFlow state is purged. Ryu receives a fresh `EventOFPSwitchFeatures` event. The controller's `@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)` function triggers immediately, re-installing the table-miss rule, ARP forwarding, and all proactive DSCP QoS flow entries, restoring network state seamlessly.

#### 2. Why use an 80% utilization threshold for congestion detection and not 90% or 95%?
* **Answer**: Queuing delay does not scale linearly; according to $M/M/1$ queueing theory, queue wait time escalates asymptotically as utilization approaches 100%:
  $$W_q = \frac{\rho}{\mu(1 - \rho)}$$
  At 90%, buffers are already filling rapidly. An 80% watermark provides necessary headroom and reaction time for the controller to poll, compute, and install throttle rules before output buffers drop packets.

#### 3. What happens if a client's HTTP telemetry report to the controller is delayed or the controller crashes?
* **Answer**: In `src/client.py`, telemetry reporting executes inside a dedicated daemon thread with a strict 200 ms socket timeout (`timeout=0.2`). If the REST endpoint is unreachable or delayed, the exception is caught, logged, and dropped without blocking the main synchronization exchange loop.

#### 4. Why must the time server ALSO mark its reply packets with DSCP 46?
* **Answer**: If only the client marks requests, the return packet from the server gets classified as best-effort bulk traffic on switch `s2`. Under congestion, return packets will sit in Queue 0 buffers, introducing massive asymmetric delay ($d_{\text{fwd}} \ll d_{\text{rev}}$). The NTP offset formula assumes symmetric transit; asymmetric queuing introduces an offset error of $\frac{d_{\text{fwd}} - d_{\text{rev}}}{2}$. Both directions must be marked.

#### 5. What happens if you configure `gain = 1.0` in the client clock servo?
* **Answer**: A gain of 1.0 attempts to eliminate 100% of the measured offset in a single step. Due to unavoidable network jitter and software timestamping delay variations, the client clock will constantly overcorrect, leading to severe ringing, overshoot, and oscillator instability. A gain of 0.4 provides low-pass damping.

#### 6. What happens if two clients accidentally use the same Client ID?
* **Answer**: In `src/sync_protocol.py`, requests and replies are tracked using `(client_id, sequence_number)`. If two clients share the same ID, they can inadvertently match each other's sequence numbers or corrupt the controller's `/sdn/report` telemetry table, causing cross-contamination.

#### 7. Why is `time.time()` used for server timestamps T2 and T3, while `clock.now()` is used for client timestamps T1 and T4?
* **Answer**: The server represents the absolute physical "reference ground truth" (the true wall-clock time), so it samples the true OS time `time.time()`. The client, on the other hand, is the device under test whose clock is skewed and drifting; it must query its simulated oscillator `clock.now()` to demonstrate that our algorithm can discipline a drifting software clock.
