#!/usr/bin/env python3
# UDP Network Time Synchronization using NTP 4-timestamp algorithm
# Implements client and server with clock skew correction and DSCP QoS

import argparse
import csv
import json
import os
import queue
import random
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import nullcontext

# Packet format: type (1B), client id (2B), seq (4B), prio (1B), t1, t2, t3 (8B each) -> 32 bytes
FMT = "!BHIBddd"
SIZE = struct.calcsize(FMT)
REQ, RESP = 1, 2


def setup_dscp(sock, dscp, role="Socket"):
    # configure IP_TOS (DSCP is shifted by 2 bits)
    if dscp < 0 or dscp > 63:
        raise ValueError(f"DSCP value must be in range 0-63, got {dscp}")

    tos = (dscp & 0x3F) << 2
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
        read_tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
        read_dscp = (read_tos >> 2) & 0x3F
        print(f"[{role}] DSCP set to {dscp} (TOS: 0x{tos:02X}) | Readback TOS: 0x{read_tos:02X} (DSCP: {read_dscp})")
        return read_tos
    except OSError as err:
        print(f"[{role}] Warning: Failed to set/read DSCP {dscp} (TOS: 0x{tos:02X}): {err}. "
              f"(Note: Windows may restrict IP_TOS modification without Administrative QoS Group Policies).")
        return None


class ControllerReporter:
    # sends telemetry data to SDN controller in the background
    def __init__(self, controller_url):
        self.url = controller_url
        self.queue = queue.Queue(maxsize=500)
        self.warned = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def report(self, payload):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass  # drop if queue is full

    def _worker(self):
        while True:
            payload = self.queue.get()
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    self.url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=0.2):
                    pass
            except Exception as err:
                if not self.warned:
                    print(f"[Controller] Warning: Telemetry POST to {self.url} failed: {err} "
                          f"(further reporting errors will be suppressed)")
                    self.warned = True
            finally:
                self.queue.task_done()


def run_server(port, dscp=46, use_prio_queue=False, delay_ms=0.0, jitter_ms=0.0):
    # run UDP time sync server
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    setup_dscp(sock, dscp, role="Server")

    sock.bind(("0.0.0.0", port))
    print(f"[Server] listening on UDP port {port} (DSCP={dscp}, PriorityQueue={use_prio_queue}, "
          f"Delay={delay_ms}ms, Jitter={jitter_ms}ms)")

    client_stats = {}
    stats_lock = threading.Lock()
    stop_event = threading.Event()

    def apply_artificial_delay():
        # add simulated network delay/jitter after T3
        if delay_ms > 0.0 or jitter_ms > 0.0:
            jitter = random.uniform(-jitter_ms, jitter_ms) if jitter_ms > 0.0 else 0.0
            total_delay = max(0.0, (delay_ms + jitter) / 1000.0)
            if total_delay > 0.0:
                time.sleep(total_delay)

    if use_prio_queue:
        # server priority queue: higher priority requests get handled first
        prio_q = queue.PriorityQueue()
        seq_counter = 0

        def worker():
            while not stop_event.is_set():
                try:
                    # item: (-priority, seq_counter, addr, cid, seq, prio, t1, t2)
                    item = prio_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                _, _, addr, cid, seq, prio, t1, t2 = item
                t3 = time.time()  # server transmit timestamp
                reply = struct.pack(FMT, RESP, cid, seq, prio, t1, t2, t3)
                apply_artificial_delay()
                try:
                    sock.sendto(reply, addr)
                    with stats_lock:
                        client_stats[cid] = client_stats.get(cid, 0) + 1
                except OSError:
                    pass
                finally:
                    prio_q.task_done()

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except (socket.timeout, ConnectionResetError, OSError):
                continue

            t2 = time.time()  # server receive timestamp
            if len(data) != SIZE:
                continue
            typ, cid, seq, prio, t1, _, _ = struct.unpack(FMT, data)
            if typ != REQ:
                continue

            if use_prio_queue:
                seq_counter += 1
                prio_q.put((-prio, seq_counter, addr, cid, seq, prio, t1, t2))
            else:
                t3 = time.time()
                reply = struct.pack(FMT, RESP, cid, seq, prio, t1, t2, t3)
                apply_artificial_delay()
                sock.sendto(reply, addr)
                with stats_lock:
                    client_stats[cid] = client_stats.get(cid, 0) + 1

    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
    finally:
        stop_event.set()
        sock.close()
        print("\n" + "=" * 42)
        print("         SERVER REQUEST STATISTICS        ")
        print("=" * 42)
        print(f" {'Client ID':>12} | {'Requests Handled':>18}")
        print("-" * 42)
        with stats_lock:
            if not client_stats:
                print(" No client requests were recorded.")
            else:
                for cid in sorted(client_stats.keys()):
                    print(f" {cid:>12} | {client_stats[cid]:>18}")
                print("-" * 42)
                print(f" {'Total Requests':>12} | {sum(client_stats.values()):>18}")
        print("=" * 42 + "\n")


class Clock:
    # fake clock with initial skew and drift
    def __init__(self, skew, drift_ppm=10.0):
        self.skew = skew
        self.drift = drift_ppm * 1e-6
        self.start = time.time()

    def now(self):
        t = time.time()
        return t + self.skew + self.drift * (t - self.start)

    def error(self):
        # difference between simulated clock and real time in seconds
        return self.now() - time.time()


def exchange(sock, addr, clock, cid, seq, timeout=1.0, priority=1):
    # send one request and wait for matching response
    target_ip = socket.gethostbyname(addr[0])
    target_addr = (target_ip, addr[1])

    t1 = clock.now()
    sock.sendto(struct.pack(FMT, REQ, cid, seq, priority, t1, 0.0, 0.0), target_addr)

    deadline = time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            data, src = sock.recvfrom(2048)
        except (socket.timeout, ConnectionResetError, OSError):
            return None
        t4 = clock.now()

        if src[0] != target_addr[0] or src[1] != target_addr[1] or len(data) != SIZE:
            continue
        typ, rcid, rseq, _, _, t2, t3 = struct.unpack(FMT, data)
        if typ == RESP and rcid == cid and rseq == seq:
            return t1, t2, t3, t4


def run_client(host, port, cid, skew, rounds, interval, log,
               gain=0.4, drift_ppm=10.0, dscp=46, priority=1,
               controller=None, report_every=1, stop_event=None):
    # run single NTP time sync client
    addr = (host, port)
    clock = Clock(skew, drift_ppm=drift_ppm)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    setup_dscp(sock, dscp, role=f"Client {cid}")

    reporter = ControllerReporter(controller) if controller else None

    # prepare CSV log file
    log_context = open(log, "w", newline="") if log else nullcontext()
    with log_context as f:
        writer = csv.writer(f) if f else None
        if writer:
            writer.writerow([
                "timestamp",
                "client_id",
                "seq",
                "rtt_ms",
                "offset_ms",
                "true_error_ms",
                "true_error_before_ms",
                "true_error_after_ms",
                "full_correction_error_ms",
                "jitter_ms",
                "sync_error_ms"
            ])

        prev_rtt_ms = None
        try:
            for seq in range(rounds):
                if stop_event and stop_event.is_set():
                    break

                result = exchange(sock, addr, clock, cid, seq, priority=priority)
                if result is None:
                    print(f"[Client {cid}] seq {seq:02d}: timeout")
                else:
                    t1, t2, t3, t4 = result
                    rtt = (t4 - t1) - (t3 - t2)
                    offset = ((t2 - t1) + (t3 - t4)) / 2
                    rtt_ms = rtt * 1000.0
                    jitter_ms = abs(rtt_ms - prev_rtt_ms) if prev_rtt_ms is not None else 0.0
                    prev_rtt_ms = rtt_ms

                    # measure error before and after clock correction
                    err_before = clock.error()
                    full_corr_err = err_before + offset
                    clock.skew += gain * offset
                    err_after = clock.error()

                    print(f"[Client {cid}] seq {seq:02d} | rtt {rtt_ms:7.2f} ms | "
                          f"offset {offset*1000:8.2f} ms | before {err_before*1000:8.2f} ms | "
                          f"after {err_after*1000:8.2f} ms")

                    if writer:
                        writer.writerow([
                            time.time(),
                            cid,
                            seq,
                            rtt_ms,
                            offset * 1000,
                            err_before * 1000,
                            err_before * 1000,
                            err_after * 1000,
                            full_corr_err * 1000,
                            jitter_ms,
                            abs(err_after * 1000)
                        ])

                    # send metrics to SDN controller if enabled
                    if reporter and (seq % report_every == 0):
                        payload = {
                            "client_id": cid,
                            "rtt_ms": rtt_ms,
                            "offset_ms": offset * 1000,
                            "error_ms": abs(err_after * 1000),
                            "timestamp": time.time()
                        }
                        reporter.report(payload)

                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n[Client {cid}] Interrupted by user.")
        finally:
            sock.close()


def run_clients(host, port, n, skews, rounds, interval, base_id=101,
                gain=0.4, drift_ppm=10.0, dscp=46, priority=1,
                controller=None, report_every=1, log_template="client_{cid}.csv"):
    # run N client threads concurrently in one process
    if not skews:
        skews = [0.35, -0.35, 0.80, -1.20]
    client_skews = [skews[i % len(skews)] for i in range(n)]

    stop_event = threading.Event()
    threads = []

    print(f"[Clients] Launching {n} client threads against {host}:{port}...")

    for i in range(n):
        cid = base_id + i
        skew = client_skews[i]
        log_file = log_template.format(cid=cid, id=cid) if log_template else f"client_{cid}.csv"
        t = threading.Thread(
            target=run_client,
            args=(host, port, cid, skew, rounds, interval, log_file),
            kwargs={
                "gain": gain,
                "drift_ppm": drift_ppm,
                "dscp": dscp,
                "priority": priority,
                "controller": controller,
                "report_every": report_every,
                "stop_event": stop_event
            },
            daemon=True
        )
        threads.append(t)
        t.start()

    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=0.2)
        print(f"[Clients] All {n} clients completed successfully.")
    except KeyboardInterrupt:
        print("\n[Clients] KeyboardInterrupt received. Signalling all client threads to exit...")
        stop_event.set()
        for t in threads:
            t.join(timeout=1.0)
        print("[Clients] Shutdown complete.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="UDP Network Time Synchronization Protocol (NTP-Style 4 Timestamps)"
    )
    p.add_argument("mode", choices=["server", "client", "clients"],
                   help="Operating mode: 'server', 'client', or multi-client 'clients'")
    p.add_argument("--host", default="127.0.0.1",
                   help="Server hostname or IP address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=9999,
                   help="UDP port number (default: 9999)")
    p.add_argument("--id", type=int, default=1,
                   help="Client ID for single client, or base ID for multi-client (default: 1)")
    p.add_argument("--skew", type=float, default=0.25,
                   help="Initial clock skew in seconds for single client (default: 0.25)")
    p.add_argument("--rounds", type=int, default=50,
                   help="Number of synchronization rounds (default: 50)")
    p.add_argument("--interval", type=float, default=0.1,
                   help="Interval between synchronization rounds in seconds (default: 0.1)")
    p.add_argument("--log", default=None,
                   help="Path to CSV log file (default: None, or 'client_{cid}.csv' in clients mode)")
    p.add_argument("--dscp", type=int, default=46,
                   help="DSCP QoS marking (0-63). Default: 46 (Expedited Forwarding EF / TOS 0xB8). 0 = no marking.")
    p.add_argument("--gain", type=float, default=0.4,
                   help="Proportional clock correction gain (default: 0.4)")
    p.add_argument("--drift-ppm", type=float, default=10.0,
                   help="Simulated hardware clock frequency drift in parts-per-million (default: 10.0 PPM)")
    p.add_argument("--priority", type=int, default=1,
                   help="Request packet priority field (0-255, default: 1)")
    p.add_argument("--controller", default=None,
                   help="SDN Controller URL to POST JSON telemetry reports (e.g. http://127.0.0.1:8080/sync/report)")
    p.add_argument("--report-every", type=int, default=1,
                   help="Send telemetry to controller every N rounds (default: 1)")
    p.add_argument("--n", type=int, default=4,
                   help="Number of concurrent client threads for 'clients' mode (default: 4)")
    p.add_argument("--skews", default="0.35,-0.35,0.80,-1.20",
                   help="Comma-separated initial skews in seconds for 'clients' mode (default: '0.35,-0.35,0.80,-1.20')")
    p.add_argument("--priority-queue", dest="priority_queue", action="store_true", default=False,
                   help="Enable server priority queue (default: False)")
    p.add_argument("--delay-ms", type=float, default=0.0,
                   help="Artificial server-side delay in milliseconds inserted AFTER T3 (default: 0.0)")
    p.add_argument("--jitter-ms", type=float, default=0.0,
                   help="Artificial server-side jitter in milliseconds inserted AFTER T3 (default: 0.0)")

    a = p.parse_args()

    if a.mode == "server":
        run_server(
            port=a.port,
            dscp=a.dscp,
            use_prio_queue=a.priority_queue,
            delay_ms=a.delay_ms,
            jitter_ms=a.jitter_ms
        )
    elif a.mode == "client":
        run_client(
            host=a.host,
            port=a.port,
            cid=a.id,
            skew=a.skew,
            rounds=a.rounds,
            interval=a.interval,
            log=a.log,
            gain=a.gain,
            drift_ppm=a.drift_ppm,
            dscp=a.dscp,
            priority=a.priority,
            controller=a.controller,
            report_every=a.report_every
        )
    elif a.mode == "clients":
        parsed_skews = [float(s.strip()) for s in a.skews.split(",") if s.strip()]
        log_pattern = a.log if a.log else "client_{cid}.csv"
        run_clients(
            host=a.host,
            port=a.port,
            n=a.n,
            skews=parsed_skews,
            rounds=a.rounds,
            interval=a.interval,
            base_id=a.id if a.id != 1 else 101,
            gain=a.gain,
            drift_ppm=a.drift_ppm,
            dscp=a.dscp,
            priority=a.priority,
            controller=a.controller,
            report_every=a.report_every,
            log_template=log_pattern
        )
