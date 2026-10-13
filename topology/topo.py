#!/usr/bin/env python3
"""
================================================================================
Network Time Synchronization Topology and Experiment Orchestrator
================================================================================

Target Environment:
- OS: Ubuntu 22.04 LTS
- Python: 3.8 - 3.10
- Mininet: 2.3.0
- Open vSwitch: 2.17+
- OpenFlow: 1.3
- Ryu Controller: 4.34 (running on 127.0.0.1:6653)

Network Architecture:
---------------------
  [ c1 ] (10.0.0.1)  ---+
  [ c2 ] (10.0.0.2)  ---|
       ...              +--- [ s1 ] ========= Primary Link (Bottleneck) ========= [ s2 ] ---+--- [ srv ] (10.0.0.100, Time Server)
  [ cN ] (10.0.0.N)  ---|    (dpid=1)                                             (dpid=2)  +--- [ ls ]  (10.0.0.201, Load Sink)
  [ lg ] (10.0.0.200) --+       |                                                    |
  (Load Generator)              +............ Backup Link (Failover) ................+

Key Design Principles & Networking Theory:
------------------------------------------
1. Why the Bottleneck Exists:
   Time synchronization protocols (e.g., NTP, PTP/IEEE 1588, Cristian's algorithm)
   calculate clock offset using round-trip delay estimates under the assumption of
   symmetric network propagation delay:
       Offset = ((T2 - T1) + (T3 - T4)) / 2
   On uncongested high-speed links, packets traverse switches near line rate with
   sub-millisecond propagation and minimal jitter. Under asymmetric congestion
   (e.g., heavy file transfers or background UDP traffic), packets wait in switch
   queues. If the forward path (c -> srv) experiences queuing delay while the return
   path (srv -> c) does not (or vice versa), the delay asymmetry induces severe
   time skew estimation errors.
   The bottleneck link between s1 and s2 (default 10 Mbit/s) deliberately creates
   a contention point where cross-traffic (lg -> ls) causes queue buildup and
   packet drop, demonstrating the need for SDN QoS queueing and DSCP prioritization.

2. Why Bidirectional Queue Configuration is Necessary:
   In Linux traffic control (tc) and Open vSwitch, queuing disciplines (qdisc)
   such as HTB (Hierarchical Token Bucket) only govern packet scheduling at the
   EGRESS interface. Ingress shaping cannot prioritize packets already in flight.
   Therefore:
   - Client-to-Server sync requests queue at s1's egress interface towards s2.
   - Server-to-Client sync replies queue at s2's egress interface towards s1.
   Both directions must have QoS configured to prevent delay asymmetry on both legs.

3. Port Naming in Mininet:
   Mininet names switch interfaces based on the order links are attached:
   e.g., s1-eth1, s1-eth2, etc. We programmatically extract the exact interface
   names via `node1.connectionsTo(node2)` so that queue setup scripts and `tc`
   manipulations target the exact network interface every time.
================================================================================
"""

import argparse
import os
import sys
import time
import subprocess
from pathlib import Path

# Mininet core modules
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info, warn, error


# ==============================================================================
# 1. Topology Definition Class
# ==============================================================================

class TimeSyncTopo(Topo):
    """
    Mininet topology for Network Time Synchronization experiments.
    Connects N sync clients and a load generator to switch s1, and a time server
    plus load sink to switch s2, separated by primary and optional backup bottleneck links.
    """

    def __init__(self, n_clients=3, bw=10, delay='5ms', loss=0, queue_size=100, backup=True, **opts):
        """
        Initialize TimeSyncTopo.

        :param n_clients: Number of time synchronization client hosts (default 3)
        :param bw: Bottleneck link bandwidth in Mbit/s (default 10)
        :param delay: Bottleneck propagation delay (default '5ms')
        :param loss: Bottleneck packet loss percentage (default 0)
        :param queue_size: Max buffer queue size in packets (default 100)
        :param backup: Boolean flag to create a secondary backup link between s1 and s2
        """
        self.n_clients = n_clients
        self.bw = bw
        self.delay = delay
        self.loss = loss
        self.queue_size = queue_size
        self.backup = backup

        # Initialize parent Topo
        super(TimeSyncTopo, self).__init__(**opts)

    def build(self):
        """Build switches, hosts, and links with static IPs, MACs, and TCLink parameters."""

        # 1. Add OpenFlow 1.3 Open vSwitches with fixed DPIDs
        # DPID 1 for client-side switch s1, DPID 2 for server-side switch s2
        s1 = self.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')

        # 2. Primary Bottleneck Link between s1 and s2 (Fixed on Port 1: s1-eth1 <-> s2-eth1)
        # Added first so OpenFlow port 1 is deterministically the primary inter-switch link
        # regardless of the number of client hosts.
        self.addLink(s1, s2, port1=1, port2=1, cls=TCLink,
                     bw=self.bw,
                     delay=self.delay,
                     loss=self.loss,
                     max_queue_size=self.queue_size)

        # 3. Backup Link between s1 and s2 (Fixed on Port 2: s1-eth2 <-> s2-eth2)
        if self.backup:
            self.addLink(s1, s2, port1=2, port2=2, cls=TCLink,
                         bw=self.bw,
                         delay=self.delay,
                         loss=self.loss,
                         max_queue_size=self.queue_size)

        # 4. Add Time Server (srv) and Load Sink (ls) on switch s2 (Ports 3, 4)
        # Static IP: 10.0.0.100, Static MAC: 00:00:00:00:00:64
        srv = self.addHost('srv', ip='10.0.0.100/24', mac='00:00:00:00:00:64')
        self.addLink(srv, s2, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        # Static IP: 10.0.0.201, Static MAC: 00:00:00:00:00:c9
        ls = self.addHost('ls', ip='10.0.0.201/24', mac='00:00:00:00:00:c9')
        self.addLink(ls, s2, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        # 5. Add Sync Clients (c1..cN) and Load Generator (lg) on switch s1 (Ports 3..N+3)
        for i in range(1, self.n_clients + 1):
            client_ip = f'10.0.0.{i}/24'
            client_mac = f'00:00:00:00:00:{i:02x}'
            c = self.addHost(f'c{i}', ip=client_ip, mac=client_mac)
            self.addLink(c, s1, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        # Load Generator (lg): Static IP 10.0.0.200, MAC 00:00:00:00:00:c8
        lg = self.addHost('lg', ip='10.0.0.200/24', mac='00:00:00:00:00:c8')
        self.addLink(lg, s1, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)


# ==============================================================================
# 2. Queue Configuration Utilities
# ==============================================================================

def setup_ovs_queues(net, bw_mbps=10):
    """
    Configures Linux Hierarchical Token Bucket (HTB) queues on Open vSwitch egress ports
    connecting switches s1 and s2 (both primary and backup inter-switch links).

    Queue Allocation:
    - Queue 0: Default best-effort traffic
    - Queue 1: High Priority (Time Sync / DSCP 46 / EF): min-rate=80%, max-rate=100%
    - Queue 2: Default Background Traffic: min-rate=20%, max-rate=100%
    - Queue 3: Throttled Cross-traffic: min-rate=500Kbps, max-rate=20%
    """
    s1 = net.get('s1')
    s2 = net.get('s2')

    connections = s1.connectionsTo(s2)
    if not connections:
        error("[ERROR] No direct link found between switch s1 and s2 for queue setup.\n")
        return False

    proj_root = Path(__file__).resolve().parent.parent
    script_path = proj_root / 'scripts' / 'setup_queues.sh'

    for idx, (s1_intf, s2_intf) in enumerate(connections):
        port_s1 = s1_intf.name
        port_s2 = s2_intf.name
        link_label = "Primary" if idx == 0 else "Backup"
        info(f"*** Configuring OVS HTB Queues on {link_label} link: {port_s1} <--> {port_s2} (BW={bw_mbps}Mbps)\n")

        if script_path.exists():
            cmd = f"bash {script_path} {bw_mbps} {port_s1} {port_s2}"
            res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                info(f"*** Queue setup script executed on {port_s1} / {port_s2}.\n")
                continue
            else:
                warn(f"[WARN] setup_queues.sh failed (code {res.returncode}): {res.stderr}. Falling back to inline OVS commands.\n")

        max_rate = int(bw_mbps * 1000 * 1000)
        q1_min = int(bw_mbps * 800 * 1000)
        q1_max = max_rate
        q2_min = int(bw_mbps * 200 * 1000)
        q2_max = max_rate
        q3_min = 500000
        q3_max = int(bw_mbps * 200 * 1000)

        for port in [port_s1, port_s2]:
            ovs_cmd = (
                f"ovs-vsctl --if-exists clear Port {port} qos -- "
                f"-- set Port {port} qos=@newqos -- "
                f"--id=@newqos create QoS type=linux-htb other-config:max-rate={max_rate} "
                f"queues:0=@q0 queues:1=@q1 queues:2=@q2 queues:3=@q3 -- "
                f"--id=@q0 create Queue other-config:min-rate=1000000 other-config:max-rate={max_rate} -- "
                f"--id=@q1 create Queue other-config:min-rate={q1_min} other-config:max-rate={q1_max} -- "
                f"--id=@q2 create Queue other-config:min-rate={q2_min} other-config:max-rate={q2_max} -- "
                f"--id=@q3 create Queue other-config:min-rate={q3_min} other-config:max-rate={q3_max}"
            )
            subprocess.run(ovs_cmd, shell=True, check=False)
            info(f"    Configured QoS on port {port}\n")

    return True


# ==============================================================================
# 3. Dynamic Helper Functions for Experimentation
# ==============================================================================

def bring_link_down(net, node1_name, node2_name, link_index=0):
    """
    Simulates a link failure by bringing down the interface connecting node1 and node2.
    """
    node1 = net.get(node1_name)
    node2 = net.get(node2_name)
    connections = node1.connectionsTo(node2)
    if not connections or len(connections) <= link_index:
        warn(f"[WARN] Link {link_index} between {node1_name} and {node2_name} not found.\n")
        return False

    intf1, intf2 = connections[link_index]
    info(f"*** Bringing DOWN link between {node1_name}:{intf1.name} and {node2_name}:{intf2.name}\n")
    node1.cmd(f"ip link set {intf1.name} down")
    node2.cmd(f"ip link set {intf2.name} down")
    return True


def bring_link_up(net, node1_name, node2_name, link_index=0):
    """
    Restores a failed link by bringing up the interfaces connecting node1 and node2.
    """
    node1 = net.get(node1_name)
    node2 = net.get(node2_name)
    connections = node1.connectionsTo(node2)
    if not connections or len(connections) <= link_index:
        warn(f"[WARN] Link {link_index} between {node1_name} and {node2_name} not found.\n")
        return False

    intf1, intf2 = connections[link_index]
    info(f"*** Bringing UP link between {node1_name}:{intf1.name} and {node2_name}:{intf2.name}\n")
    node1.cmd(f"ip link set {intf1.name} up")
    node2.cmd(f"ip link set {intf2.name} up")
    return True


def change_link_params(net, node1_name, node2_name, bw=None, delay=None, loss=None, link_index=0):
    """
    Dynamically adjusts bottleneck link parameters (delay, loss, bandwidth) on the fly
    using Linux Traffic Control (tc). Useful for jitter and delay-asymmetry tests.
    """
    node1 = net.get(node1_name)
    node2 = net.get(node2_name)
    connections = node1.connectionsTo(node2)
    if not connections or len(connections) <= link_index:
        warn(f"[WARN] Link between {node1_name} and {node2_name} not found.\n")
        return False

    intf1, intf2 = connections[link_index]
    info(f"*** Updating link parameters on {intf1.name} <--> {intf2.name}: delay={delay}, loss={loss}, bw={bw}\n")

    for intf in [intf1, intf2]:
        netem_args = []
        if delay is not None:
            netem_args.append(f"delay {delay}")
        if loss is not None:
            netem_args.append(f"loss {loss}%")

        if netem_args:
            args_str = " ".join(netem_args)
            cmd = f"tc qdisc change dev {intf.name} parent 1:1 handle 10: netem {args_str} 2>/dev/null || tc qdisc add dev {intf.name} root handle 1: netem {args_str}"
            subprocess.run(cmd, shell=True, check=False)

    return True


def start_iperf_load(net, rate_mbps=9.0, duration=86400, use_loadgen=True):
    """
    Generates background UDP cross-traffic from Load Generator (lg: 10.0.0.200)
    to Load Sink (ls: 10.0.0.201) on UDP port 5201 to saturate the bottleneck link.
    Integrates Role 3's src/loadgen.py directly (with iperf3 fallback).
    """
    lg = net.get('lg')
    ls = net.get('ls')
    proj_root = Path(__file__).resolve().parent.parent
    loadgen_script = proj_root / 'src' / 'loadgen.py'

    stop_iperf_load(net)

    if use_loadgen and loadgen_script.exists():
        info(f"*** Starting Role 3 UDP load generator (src/loadgen.py): {lg.name} -> {ls.name} ({rate_mbps} Mbit/s, port 5201)\n")
        ls.cmd(f"python3 {loadgen_script} --sink --port 5201 > /tmp/loadsink.log 2>&1 &")
        time.sleep(0.3)
        lg.cmd(f"python3 {loadgen_script} --host 10.0.0.201 --port 5201 --mbps {rate_mbps} --duration {duration} > /tmp/loadgen.log 2>&1 &")
    else:
        info(f"*** Starting background iperf3 UDP cross-traffic: {lg.name} -> {ls.name} ({rate_mbps} Mbit/s)\n")
        ls.cmd('iperf3 -s -p 5201 -D')
        time.sleep(0.5)
        lg.cmd(f'iperf3 -c 10.0.0.201 -p 5201 -u -b {rate_mbps}M -t {duration} > /tmp/iperf_client.log 2>&1 &')

    info(f"*** Background load active at {rate_mbps} Mbit/s.\n")


def stop_iperf_load(net):
    """Stops any running loadgen.py or iperf3 load generator and sink instances."""
    lg = net.get('lg')
    ls = net.get('ls')
    info("*** Stopping background cross-traffic...\n")
    lg.cmd('pkill -f loadgen.py 2>/dev/null; killall -9 iperf3 2>/dev/null')
    ls.cmd('pkill -f loadgen.py 2>/dev/null; killall -9 iperf3 2>/dev/null')


def start_sync(net, n_clients=3, skews=None, dscp=46, rounds=50, interval=0.2, results_dir='results'):
    """
    Spawns the patched Role 1 time synchronization server on srv (10.0.0.100:9999)
    and client sync processes on c1..cN, reporting telemetry to the Ryu controller.
    Logs time skew, RTT, jitter, and sync error to CSV files in results_dir.
    """
    srv = net.get('srv')
    proj_root = Path(__file__).resolve().parent.parent
    core_script = proj_root / 'src' / 'timesync_core.py'

    os.makedirs(results_dir, exist_ok=True)

    if not core_script.exists():
        warn(f"[WARN] Timesync core script not found at {core_script}. Please ensure src/timesync_core.py exists.\n")
        return

    # Warm up L2 MAC learning across switches before starting sync
    for i in range(1, n_clients + 1):
        c = net.get(f'c{i}')
        c.cmd('ping -c 1 -W 1 10.0.0.100 >/dev/null 2>&1')
    lg = net.get('lg')
    lg.cmd('ping -c 1 -W 1 10.0.0.201 >/dev/null 2>&1')

    info(f"*** Launching Time Server on {srv.name} (IP: 10.0.0.100, UDP 9999, DSCP={dscp})...\n")
    srv.cmd('pkill -f timesync_core.py 2>/dev/null')
    server_cmd = f"python3 {core_script} server --port 9999 --dscp {dscp} > {results_dir}/server.log 2>&1 &"
    srv.cmd(server_cmd)
    time.sleep(1.0)

    # Default realistic skews in seconds (matching Role 1 test_multiclient.py)
    default_pool = [0.35, -0.35, 0.80, -1.20, 0.50, -0.60, 0.90, -0.45, 0.65, -0.85]
    if not skews:
        skews = [default_pool[(i - 1) % len(default_pool)] for i in range(1, n_clients + 1)]

    client_procs = []
    for i in range(1, n_clients + 1):
        c = net.get(f'c{i}')
        skew_val = skews[(i - 1) % len(skews)]
        log_file = os.path.join(results_dir, f"client_c{i}.csv")

        info(f"*** Launching Sync Client {c.name} (id={i}, skew={skew_val:+.2f}s, dscp={dscp}, rounds={rounds})...\n")
        client_cmd = (
            f"python3 {core_script} client "
            f"--host 10.0.0.100 --port 9999 --id {i} --dscp {dscp} "
            f"--skew {skew_val} --rounds {rounds} --interval {interval} "
            f"--controller http://127.0.0.1:8080/sync/report "
            f"--log {log_file} > {results_dir}/client_{c.name}.log 2>&1 &"
        )
        c.cmd(client_cmd)
        client_procs.append((c, log_file))

    return client_procs


# ==============================================================================
# 4. Scripted Experiment Mode
# ==============================================================================

def run_scripted_experiment(net, args):
    """
    Executes an automated, reproducible benchmark experiment (E1..E6):
    1. Sets up OVS priority queues on primary and backup links
    2. Optionally starts background UDP traffic (Role 3 loadgen.py / iperf3)
    3. Runs time synchronization protocol across all clients
    4. Optionally triggers primary link failure & recovery mid-run (--failover)
    5. Gathers CSV telemetry and prints evaluation summary
    """
    info("\n============================================================\n")
    info(" Starting Scripted Automated Experiment\n")
    info("============================================================\n")

    results_dir = os.path.abspath(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)

    # 1. Setup Queues
    setup_ovs_queues(net, bw_mbps=args.bw)

    # 2. Parse client skews
    if args.skews:
        client_skews = [float(s.strip()) for s in args.skews.split(',') if s.strip()]
    else:
        default_pool = [0.35, -0.35, 0.80, -1.20, 0.50, -0.60, 0.90, -0.45, 0.65, -0.85]
        client_skews = [default_pool[i % len(default_pool)] for i in range(args.clients)]

    # 3. Start background load generator (unless --no-load is set for E1/E2)
    if not args.no_load:
        load_rate = args.load_mbps if args.load_mbps is not None else round(args.bw * 0.9, 1)
        start_iperf_load(net, rate_mbps=load_rate)
        time.sleep(2.0)
    else:
        info("*** Running in low/no background traffic mode (--no-load).\n")

    # 4. Start synchronization
    start_sync(
        net=net,
        n_clients=args.clients,
        skews=client_skews,
        dscp=args.dscp,
        rounds=args.rounds,
        interval=args.interval,
        results_dir=results_dir
    )

    # Wait for completion (with optional mid-experiment link failure for E6)
    total_wait = max(6, int(args.rounds * args.interval) + 4)
    info(f"*** Waiting {total_wait}s for sync clients to complete {args.rounds} rounds...\n")

    if args.failover and not args.no_backup:
        third = max(2, total_wait // 3)
        time.sleep(third)
        info("*** [E6 Challenge] Simulating Primary Bottleneck Link FAILURE (s1-eth1 <-> s2-eth1)...\n")
        bring_link_down(net, 's1', 's2', link_index=0)
        time.sleep(third)
        info("*** [E6 Challenge] Restoring Primary Bottleneck Link UP (s1-eth1 <-> s2-eth1)...\n")
        bring_link_up(net, 's1', 's2', link_index=0)
        time.sleep(max(2, total_wait - 2 * third))
    else:
        step = 5
        for sec in range(total_wait, 0, -step):
            info(f"    Remaining: {sec}s...\n")
            time.sleep(min(step, sec))

    # 5. Stop cross-traffic and sync server
    if not args.no_load:
        stop_iperf_load(net)
    net.get('srv').cmd('pkill -INT -f timesync_core.py 2>/dev/null')
    time.sleep(0.5)

    # 6. Parse and summarize results
    info("\n============================================================\n")
    info(" Experiment Complete. Results Summary:\n")
    info(f" Output Directory: {results_dir}\n")
    info("============================================================\n")

    for i in range(1, args.clients + 1):
        csv_path = os.path.join(results_dir, f"client_c{i}.csv")
        if os.path.exists(csv_path):
            with open(csv_path, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
            data_rows = [l for l in lines if not l.startswith('#') and not l.startswith('timestamp')]
            info(f"  [+] Client c{i}: {len(data_rows)} rounds logged -> {csv_path}\n")
        else:
            warn(f"  [-] Client c{i}: Log file {csv_path} not found.\n")

    info("============================================================\n")


# ==============================================================================
# 5. CLI Argument Parsing and Main Entrypoint
# ==============================================================================

def parse_cli_args():
    """Defines and parses command-line arguments for topology execution."""
    parser = argparse.ArgumentParser(
        description="SDN Network Time Synchronization Mininet Topology",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument('--clients', type=int, default=3,
                        help="Number of synchronization client hosts (1 to 10)")
    parser.add_argument('--bw', type=float, default=10.0,
                        help="Bottleneck link bandwidth in Mbit/s")
    parser.add_argument('--delay', type=str, default='5ms',
                        help="Bottleneck propagation delay (e.g., '5ms')")
    parser.add_argument('--loss', type=float, default=0.0,
                        help="Bottleneck packet loss percentage (0-100)")
    parser.add_argument('--queue-size', type=int, default=100,
                        help="Bottleneck interface max queue size in packets")
    parser.add_argument('--no-backup', action='store_true',
                        help="Disable redundant backup link between s1 and s2")
    parser.add_argument('--scripted', action='store_true',
                        help="Run non-interactive automated experiment and exit")
    parser.add_argument('--no-load', action='store_true',
                        help="Disable background UDP cross-traffic in scripted mode (for E1/E2 baseline)")
    parser.add_argument('--load-mbps', type=float, default=None,
                        help="Background UDP load rate in Mbit/s (default: 90%% of --bw)")
    parser.add_argument('--failover', action='store_true',
                        help="Trigger primary link failure and recovery mid-experiment (for E6 challenge)")
    parser.add_argument('--skews', type=str, default=None,
                        help="Comma-separated initial clock skews in seconds (e.g. '0.35,-0.35,0.80')")
    parser.add_argument('--dscp', type=int, default=46,
                        help="DSCP traffic class value for time sync packets (46 = Expedited Forwarding, 0 = none)")
    parser.add_argument('--rounds', type=int, default=50,
                        help="Number of synchronization rounds per client")
    parser.add_argument('--interval', type=float, default=0.2,
                        help="Interval between sync rounds in seconds")
    parser.add_argument('--results-dir', type=str, default='results',
                        help="Directory to save experimental CSV logs")

    return parser.parse_args()


def main():
    setLogLevel('info')
    args = parse_cli_args()

    info("============================================================\n")
    info(" Initializing SDN Time Synchronization Topology\n")
    info(f" Clients: {args.clients} | Bottleneck BW: {args.bw}M | Delay: {args.delay}\n")
    info(f" Backup Path: {'Disabled' if args.no_backup else 'Enabled'}\n")
    info("============================================================\n")

    # Instantiate topology
    topo = TimeSyncTopo(
        n_clients=args.clients,
        bw=args.bw,
        delay=args.delay,
        loss=args.loss,
        queue_size=args.queue_size,
        backup=(not args.no_backup)
    )

    # Controller configuration: Ryu OpenFlow 1.3 controller on localhost:6653
    controller = RemoteController('c0', ip='127.0.0.1', port=6653)

    # Construct Mininet network with Open vSwitch in OpenFlow 1.3 mode
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        controller=controller,
        link=TCLink,
        autoSetMacs=False,
        autoStaticArp=True
    )

    try:
        info("*** Starting network...\n")
        net.start()

        # Always setup egress QoS queues on the switches
        setup_ovs_queues(net, bw_mbps=args.bw)

        if args.scripted:
            # Scripted experiment mode: runs benchmark and terminates
            run_scripted_experiment(net, args)
        else:
            # Interactive CLI mode
            info("\n============================================================\n")
            info(" Mininet Interactive CLI Ready\n")
            info(" Useful Testing Commands in CLI:\n")
            info("   mininet> pingall\n")
            info("   mininet> c1 ping -c 3 srv\n")
            info("   mininet> lg iperf3 -c 10.0.0.201 -u -b 8M -t 10 &\n")
            info("   mininet> dpctl dump-flows -O OpenFlow13\n")
            info("   mininet> sh ovs-ofctl queue-stats s1 -O OpenFlow13\n")
            info(" Python Helpers available inside CLI via `py`:\n")
            info("   mininet> py bring_link_down(net, 's1', 's2')\n")
            info("   mininet> py bring_link_up(net, 's1', 's2')\n")
            info("   mininet> py change_link_params(net, 's1', 's2', delay='20ms')\n")
            info("   mininet> py start_iperf_load(net, 8)\n")
            info("   mininet> py stop_iperf_load(net)\n")
            info("============================================================\n\n")

            # Inject helper methods into CLI context
            CLI.net = net
            CLI.bring_link_down = bring_link_down
            CLI.bring_link_up = bring_link_up
            CLI.change_link_params = change_link_params
            CLI.start_iperf_load = start_iperf_load
            CLI.stop_iperf_load = stop_iperf_load

            CLI(net)

    except Exception as e:
        error(f"[ERROR] Exception during execution: {e}\n")
        import traceback
        traceback.print_exc()
    finally:
        info("*** Stopping network...\n")
        stop_iperf_load(net)
        net.stop()
        info("*** Mininet stopped cleanly.\n")


if __name__ == '__main__':
    main()
