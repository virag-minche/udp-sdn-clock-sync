#!/usr/bin/env python3
# Mininet topology for network time synchronization experiments.
# Sets up s1 and s2 with a bottleneck link, clients c1..cN,
# a time server, load generator, and load sink.

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


# Topology definition
class TimeSyncTopo(Topo):
    # custom topology with clients, server, and cross traffic hosts

    def __init__(self, n_clients=3, bw=10, delay='5ms', loss=0, queue_size=100, backup=True, **opts):
        self.n_clients = n_clients
        self.bw = bw
        self.delay = delay
        self.loss = loss
        self.queue_size = queue_size
        self.backup = backup

        super(TimeSyncTopo, self).__init__(**opts)

    def build(self):
        # switches s1 and s2 with openflow 1.3
        s1 = self.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')

        # primary bottleneck link on port 1
        self.addLink(s1, s2, port1=1, port2=1, cls=TCLink,
                     bw=self.bw,
                     delay=self.delay,
                     loss=self.loss,
                     max_queue_size=self.queue_size)

        # optional backup link on port 2
        if self.backup:
            self.addLink(s1, s2, port1=2, port2=2, cls=TCLink,
                         bw=self.bw,
                         delay=self.delay,
                         loss=self.loss,
                         max_queue_size=self.queue_size)

        # server and load sink on s2
        srv = self.addHost('srv', ip='10.0.0.100/24', mac='00:00:00:00:00:64')
        self.addLink(srv, s2, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        ls = self.addHost('ls', ip='10.0.0.201/24', mac='00:00:00:00:00:c9')
        self.addLink(ls, s2, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        # client hosts and load generator on s1
        for i in range(1, self.n_clients + 1):
            client_ip = f'10.0.0.{i}/24'
            client_mac = f'00:00:00:00:00:{i:02x}'
            c = self.addHost(f'c{i}', ip=client_ip, mac=client_mac)
            self.addLink(c, s1, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)

        lg = self.addHost('lg', ip='10.0.0.200/24', mac='00:00:00:00:00:c8')
        self.addLink(lg, s1, cls=TCLink, bw=100, delay='1ms', max_queue_size=1000)


# set up htb queues on ovs ports connecting s1 and s2
def setup_ovs_queues(net, bw_mbps=10):
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

        # fallback to ovs-vsctl if script fails or is missing
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


# bring down link interfaces to simulate link down
def bring_link_down(net, node1_name, node2_name, link_index=0):
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


# bring link interfaces back up
def bring_link_up(net, node1_name, node2_name, link_index=0):
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


# change link delay and loss using tc netem
def change_link_params(net, node1_name, node2_name, bw=None, delay=None, loss=None, link_index=0):
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


# start background udp traffic from lg to ls
def start_iperf_load(net, rate_mbps=9.0, duration=86400, use_loadgen=True):
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


# kill any running loadgen or iperf3 processes
def stop_iperf_load(net):
    lg = net.get('lg')
    ls = net.get('ls')
    info("*** Stopping background cross-traffic...\n")
    lg.cmd('pkill -f loadgen.py 2>/dev/null; killall -9 iperf3 2>/dev/null')
    ls.cmd('pkill -f loadgen.py 2>/dev/null; killall -9 iperf3 2>/dev/null')


# start time server and client sync processes
def start_sync(net, n_clients=3, skews=None, dscp=46, rounds=50, interval=0.2, results_dir='results'):
    srv = net.get('srv')
    proj_root = Path(__file__).resolve().parent.parent
    core_script = proj_root / 'src' / 'timesync_core.py'

    os.makedirs(results_dir, exist_ok=True)

    if not core_script.exists():
        warn(f"[WARN] Timesync core script not found at {core_script}. Please ensure src/timesync_core.py exists.\n")
        return

    # warm up mac tables before starting sync
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

    # default skews in seconds
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


# runs a full test run without interactive cli
def run_scripted_experiment(net, args):
    info("\n============================================================\n")
    info(" Starting Scripted Automated Experiment\n")
    info("============================================================\n")

    results_dir = os.path.abspath(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)

    # setup queues
    setup_ovs_queues(net, bw_mbps=args.bw)

    # parse client skews
    if args.skews:
        client_skews = [float(s.strip()) for s in args.skews.split(',') if s.strip()]
    else:
        default_pool = [0.35, -0.35, 0.80, -1.20, 0.50, -0.60, 0.90, -0.45, 0.65, -0.85]
        client_skews = [default_pool[i % len(default_pool)] for i in range(args.clients)]

    # start background load unless disabled
    if not args.no_load:
        load_rate = args.load_mbps if args.load_mbps is not None else round(args.bw * 0.9, 1)
        start_iperf_load(net, rate_mbps=load_rate)
        time.sleep(2.0)
    else:
        info("*** Running in low/no background traffic mode (--no-load).\n")

    # start sync
    start_sync(
        net=net,
        n_clients=args.clients,
        skews=client_skews,
        dscp=args.dscp,
        rounds=args.rounds,
        interval=args.interval,
        results_dir=results_dir
    )

    # wait for sync to finish, handle failover if enabled
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

    # clean up processes
    if not args.no_load:
        stop_iperf_load(net)
    net.get('srv').cmd('pkill -INT -f timesync_core.py 2>/dev/null')
    time.sleep(0.5)

    # check results
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


# parse command line args
def parse_cli_args():
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

    # instantiate topology
    topo = TimeSyncTopo(
        n_clients=args.clients,
        bw=args.bw,
        delay=args.delay,
        loss=args.loss,
        queue_size=args.queue_size,
        backup=(not args.no_backup)
    )

    # ryu controller
    controller = RemoteController('c0', ip='127.0.0.1', port=6653)

    # create mininet net
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

        # configure queues
        setup_ovs_queues(net, bw_mbps=args.bw)

        if args.scripted:
            # run scripted test
            run_scripted_experiment(net, args)
        else:
            # interactive mode
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

            # add helpers to cli
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
