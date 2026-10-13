#!/usr/bin/env python3
"""
UDP Network Time Synchronization (NTP-Style 4-Timestamp Algorithm)
==================================================================
# CHANGED: Replaced self-referencing and outdated docstring with comprehensive module documentation.
This module implements a student-readable, UDP-based clock synchronization
protocol inspired by NTP (Network Time Protocol, RFC 5905).

Packet Format (32 bytes, Big-Endian):
    !BHIBddd
    - Type      (uint8,   1 byte):  1 = REQ (Request), 2 = RESP (Response)
    - Client ID (uint16,  2 bytes): Unique client identifier
    - Sequence  (uint32,  4 bytes): Monotonically increasing round number
    - Priority  (uint8,   1 byte):  QoS priority field (0-255, higher = higher priority)
    - T1        (double,  8 bytes): Client transmit timestamp
    - T2        (double,  8 bytes): Server receive timestamp
    - T3        (double,  8 bytes): Server transmit timestamp

NTP Math:
    Round-Trip Time (RTT): D = (T4 - T1) - (T3 - T2)
    Clock Offset (theta):  θ = ((T2 - T1) + (T3 - T4)) / 2
    Clock Correction:      skew = skew + gain * θ

Supported Modes:
    - server:  Listens for time sync requests and sends timestamped responses.
               Supports priority queueing and artificial return-path delay/jitter.
    - client:  Runs a single simulated client adjusting its skewed clock.
    - clients: Multi-threaded mode running N clients concurrently in one process.
"""

import argparse
import csv
import json              # CHANGED: Required for controller JSON payload serialization
import os
import queue             # CHANGED: Required for server priority queue and background controller reporter
import random            # CHANGED: Required for server-side artificial jitter generation
import socket
import struct
import sys
import threading         # CHANGED: Required for multi-client mode, server worker, and async reporter
import time
import urllib.error      # CHANGED: Required for catching controller HTTP/network exceptions
import urllib.request    # CHANGED: Required for reporting metrics to SDN controller via HTTP POST
from contextlib import nullcontext

# Packet format: Big-endian, uint8 (type), uint16 (cid), uint32 (seq), uint8 (prio), 3 doubles (t1, t2, t3)
FMT = "!BHIBddd"          # type, client id, seq, priority, t1, t2, t3  (32 bytes)
SIZE = struct.calcsize(FMT)
REQ, RESP = 1, 2


# ---------------------------------------------------------------- QoS / DSCP Helper
def setup_dscp(sock, dscp, role="Socket"):
    """
    # CHANGED: Configurable and verifiable DSCP socket QoS helper.
    Configures the IP_TOS option (DSCP shifted left by 2 bits) and reads it back
    with getsockopt to verify setting. Gracefully handles and reports OS limitations
    (such as Windows QoS policies requiring admin rights or GPO configuration).
    """
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


# ---------------------------------------------------------------- Controller Reporter
class ControllerReporter:
    """
    # CHANGED: Non-blocking background reporter for Socket-SDN controller integration.
    Queues telemetry reports and posts them asynchronously using urllib.request.
    Guarantees that network delays or controller outages will NEVER delay or crash
    the client synchronization loop. Issues a single warning on failure.
    """
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
            pass  # CHANGED: Drop report if backlog exceeds capacity to prevent memory leaks

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
                    # CHANGED: Swallow errors with one-time user-facing warning
                    print(f"[Controller] Warning: Telemetry POST to {self.url} failed: {err} "
                          f"(further reporting errors will be suppressed)")
                    self.warned = True
            finally:
                self.queue.task_done()


# ---------------------------------------------------------------- Server
def run_server(port, dscp=46, use_prio_queue=False, delay_ms=0.0, jitter_ms=0.0):
    """
    Run NTP-style time synchronization UDP server.
    # CHANGED: Added DSCP socket marking for return path treatment.
    # CHANGED: Added request counter tracking per-client statistics printed upon exit.
    # CHANGED: Added optional PriorityQueue worker architecture with clear queueing explanation.
    # CHANGED: Added artificial delay and jitter applied strictly AFTER T3 is stamped.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # CHANGED: Configure DSCP on server socket for symmetric return path QoS
    setup_dscp(sock, dscp, role="Server")

    sock.bind(("0.0.0.0", port))
    print(f"[Server] listening on UDP port {port} (DSCP={dscp}, PriorityQueue={use_prio_queue}, "
          f"Delay={delay_ms}ms, Jitter={jitter_ms}ms)")

    client_stats = {}
    stats_lock = threading.Lock()
    stop_event = threading.Event()

    def apply_artificial_delay():
        """
        # CHANGED: Artificial server-side delay inserted AFTER T3 timestamp.
        Delay between T2 and T3 is cancelled out by NTP's RTT calculation D = (T4-T1)-(T3-T2).
        Therefore, delay must be added after T3 to simulate return path network delay.
        """
        if delay_ms > 0.0 or jitter_ms > 0.0:
            jitter = random.uniform(-jitter_ms, jitter_ms) if jitter_ms > 0.0 else 0.0
            total_delay = max(0.0, (delay_ms + jitter) / 1000.0)
            if total_delay > 0.0:
                time.sleep(total_delay)

    if use_prio_queue:
        # CHANGED: Priority queue architecture.
        # Receiver thread timestamps T2 immediately upon packet arrival, then pushes to PriorityQueue.
        # Worker thread pops highest-priority requests first (negated priority key), stamps T3, and replies.
        # Note: Under load, lower-priority requests experience longer queueing delay between T2 and T3.
        # NTP subtracts (T3 - T2), but excessive queue waiting increases total turnaround time T4 - T1.
        prio_q = queue.PriorityQueue()
        seq_counter = 0

        def worker():
            while not stop_event.is_set():
                try:
                    # Item format: (-priority, arrival_counter, addr, cid, seq, prio, t1, t2)
                    item = prio_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                _, _, addr, cid, seq, prio, t1, t2 = item
                t3 = time.time()  # Stamp T3 when worker starts transmission processing
                reply = struct.pack(FMT, RESP, cid, seq, prio, t1, t2, t3)
                apply_artificial_delay()  # Delay after T3
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

            t2 = time.time()  # Server arrival timestamp T2
            if len(data) != SIZE:
                continue
            typ, cid, seq, prio, t1, _, _ = struct.unpack(FMT, data)
            if typ != REQ:
                continue

            if use_prio_queue:
                # CHANGED: Push to PriorityQueue with negated priority (higher priority number = popped first)
                seq_counter += 1
                prio_q.put((-prio, seq_counter, addr, cid, seq, prio, t1, t2))
            else:
                # Immediate processing mode
                t3 = time.time()
                reply = struct.pack(FMT, RESP, cid, seq, prio, t1, t2, t3)
                apply_artificial_delay()
                sock.sendto(reply, addr)
                with stats_lock:
                    client_stats[cid] = client_stats.get(cid, 0) + 1

    except KeyboardInterrupt:
        # CHANGED: Clean shutdown on KeyboardInterrupt with statistics display
        print("\n[Server] Shutting down...")
    finally:
        stop_event.set()
        sock.close()
        # CHANGED: Print per-client request statistics table upon exit
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


# ---------------------------------------------------------------- Client Clock & Exchange
class Clock:
    """Fake hardware clock: real time + skew + constant drift."""
    def __init__(self, skew, drift_ppm=10.0):
        # CHANGED: Added configurable drift_ppm parameter (default 10.0 ppm)
        self.skew = skew
        self.drift = drift_ppm * 1e-6
        self.start = time.time()

    def now(self):
        t = time.time()
        return t + self.skew + self.drift * (t - self.start)

    def error(self):
        """Returns difference between simulated clock and true system time in seconds."""
        return self.now() - time.time()


def exchange(sock, addr, clock, cid, seq, timeout=1.0, priority=1):
    """
    Send one request, wait for the MATCHING reply. Returns (t1,t2,t3,t4) or None.
    # CHANGED: Added optional priority parameter (default 1) to populate packet priority field.
    # CHANGED: Resolves destination hostname to IP address to prevent source mismatch on loopback/DNS.
    """
    # CHANGED: Pre-resolve target IP address to ensure reliable comparison with recvfrom src
    target_ip = socket.gethostbyname(addr[0])
    target_addr = (target_ip, addr[1])

    t1 = clock.now()
    # CHANGED: Populate priority field in request packet
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

        # CHANGED: Compare resolved IP and port to avoid hostname vs 127.0.0.1 mismatch
        if src[0] != target_addr[0] or src[1] != target_addr[1] or len(data) != SIZE:
            continue
        typ, rcid, rseq, _, _, t2, t3 = struct.unpack(FMT, data)
        if typ == RESP and rcid == cid and rseq == seq:
            return t1, t2, t3, t4


def run_client(host, port, cid, skew, rounds, interval, log,
               gain=0.4, drift_ppm=10.0, dscp=46, priority=1,
               controller=None, report_every=1, stop_event=None):
    """
    Run single NTP time sync client.
    # CHANGED: Added configurable gain and drift_ppm parameters.
    # CHANGED: Added DSCP setting and startup verification.
    # CHANGED: Added priority parameter passed to exchange().
    # CHANGED: Added asynchronous reporting to SDN controller.
    # CHANGED: Added comprehensive error logging (before, after, full correction).
    # CHANGED: Added stop_event check for graceful thread cancellation.
    """
    addr = (host, port)
    clock = Clock(skew, drift_ppm=drift_ppm)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # CHANGED: Configurable DSCP marking with verification
    setup_dscp(sock, dscp, role=f"Client {cid}")

    # CHANGED: Initialize background controller reporter if controller URL is provided
    reporter = ControllerReporter(controller) if controller else None

    # CSV output preparation
    log_context = open(log, "w", newline="") if log else nullcontext()
    with log_context as f:
        writer = csv.writer(f) if f else None
        if writer:
            # CHANGED: Added true_error_before_ms, true_error_after_ms, full_correction_error_ms
            # Retained true_error_ms at position 5 for full backward compatibility with existing scripts
            writer.writerow([
                "timestamp",
                "client_id",
                "seq",
                "rtt_ms",
                "offset_ms",
                "true_error_ms",
                "true_error_before_ms",
                "true_error_after_ms",
                "full_correction_error_ms"
            ])

        try:
            for seq in range(rounds):
                if stop_event and stop_event.is_set():
                    # CHANGED: Honor stop_event for clean shutdown
                    break

                result = exchange(sock, addr, clock, cid, seq, priority=priority)
                if result is None:
                    print(f"[Client {cid}] seq {seq:02d}: timeout")
                else:
                    t1, t2, t3, t4 = result
                    rtt = (t4 - t1) - (t3 - t2)
                    offset = ((t2 - t1) + (t3 - t4)) / 2

                    # CHANGED: Record error before correction, apply correction, and record error after
                    err_before = clock.error()
                    full_corr_err = err_before + offset  # Error if 100% of offset were applied
                    clock.skew += gain * offset          # Proportional clock correction
                    err_after = clock.error()            # Error immediately after applying gain * offset

                    print(f"[Client {cid}] seq {seq:02d} | rtt {rtt*1000:7.2f} ms | "
                          f"offset {offset*1000:8.2f} ms | before {err_before*1000:8.2f} ms | "
                          f"after {err_after*1000:8.2f} ms")

                    if writer:
                        writer.writerow([
                            time.time(),
                            cid,
                            seq,
                            rtt * 1000,
                            offset * 1000,
                            err_before * 1000,        # true_error_ms (backward compatible)
                            err_before * 1000,        # true_error_before_ms
                            err_after * 1000,         # true_error_after_ms
                            full_corr_err * 1000      # full_correction_error_ms
                        ])

                    # CHANGED: Report metrics to SDN controller in background thread
                    if reporter and (seq % report_every == 0):
                        payload = {
                            "client_id": cid,
                            "rtt_ms": rtt * 1000,
                            "offset_ms": offset * 1000,
                            "error_ms": err_before * 1000,
                            "timestamp": time.time()
                        }
                        reporter.report(payload)

                time.sleep(interval)
        except KeyboardInterrupt:
            # CHANGED: Handle KeyboardInterrupt cleanly in client mode
            print(f"\n[Client {cid}] Interrupted by user.")
        finally:
            sock.close()


# ---------------------------------------------------------------- Multi-Client Mode
def run_clients(host, port, n, skews, rounds, interval, base_id=101,
                gain=0.4, drift_ppm=10.0, dscp=46, priority=1,
                controller=None, report_every=1, log_template="client_{cid}.csv"):
    """
    # CHANGED: Added multi-client in one process mode ('clients' mode).
    Spawns N concurrent client threads, each with its own socket, ID, skew, and CSV log file.
    Handles KeyboardInterrupt cleanly and waits for all threads to terminate.
    """
    # Ensure sufficient skews for n clients by cycling if necessary
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
        # Join threads with timeout loop to allow clean KeyboardInterrupt handling on Windows/Linux
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


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="UDP Network Time Synchronization Protocol (NTP-Style 4 Timestamps)"
    )
    # CHANGED: Added 'clients' option to mode choices
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

    # CHANGED: Added DSCP marking argument for both client and server
    p.add_argument("--dscp", type=int, default=46,
                   help="DSCP QoS marking (0-63). Default: 46 (Expedited Forwarding EF / TOS 0xB8). 0 = no marking.")

    # CHANGED: Added configurable gain and drift PPM arguments with trade-off documentation
    p.add_argument("--gain", type=float, default=0.4,
                   help="Proportional clock correction gain (default: 0.4). Trade-off: Higher gain converges faster "
                        "but amplifies network jitter; lower gain filters noise better but converges more slowly.")
    p.add_argument("--drift-ppm", type=float, default=10.0,
                   help="Simulated hardware clock frequency drift in parts-per-million (default: 10.0 PPM = 10 us/s). "
                        "Simulates quartz crystal oscillator physical drift.")

    # CHANGED: Added priority argument to set packet priority field
    p.add_argument("--priority", type=int, default=1,
                   help="Request packet priority field (0-255, default: 1). Higher values represent higher QoS priority.")

    # CHANGED: Added controller telemetry reporting arguments
    p.add_argument("--controller", default=None,
                   help="SDN Controller URL to POST JSON telemetry reports (e.g. http://127.0.0.1:8080/sync/report)")
    p.add_argument("--report-every", type=int, default=1,
                   help="Send telemetry to controller every N rounds (default: 1)")

    # CHANGED: Added multi-client arguments
    p.add_argument("--n", type=int, default=4,
                   help="Number of concurrent client threads for 'clients' mode (default: 4)")
    p.add_argument("--skews", default="0.35,-0.35,0.80,-1.20",
                   help="Comma-separated initial skews in seconds for 'clients' mode (default: '0.35,-0.35,0.80,-1.20')")

    # CHANGED: Added server priority queue and artificial delay/jitter arguments
    p.add_argument("--priority-queue", dest="priority_queue", action="store_true", default=False,
                   help="Enable server priority queue (default: False). Arriving requests are timestamped T2, "
                        "queued by priority, and timestamped T3 when serviced by worker thread.")
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
        # Parse comma-separated skews string
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
