#!/usr/bin/env python3
"""Simple UDP time sync (NTP-style 4 timestamps). Same packet format as timesync_core.py."""
import argparse
import csv
import socket #sends n receives UDP packets
import struct
import time
from contextlib import nullcontext

FMT = "!BHIBddd"          # type, client id, seq, priority, t1, t2, t3  (32 bytes)
SIZE = struct.calcsize(FMT) #32bytes
REQ, RESP = 1, 2


# ---------------------------------------------------------------- server
def run_server(port): #server arch
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port)) #local network interface
    print(f"[Server] listening on UDP {port}")
    try:
        while True:                                   # one loop, no threads needed
            data, addr = sock.recvfrom(2048)  #when sock.recvfrom() unblocks t2 records when d packets arrive to server
            t2 = time.time()                          # arrival
            if len(data) != SIZE:
                continue
            typ, cid, seq, prio, t1, _, _ = struct.unpack(FMT, data) #after unpacking time.time() called for t3 b4 sending response to reduce serverside processing delay interference
            if typ != REQ:
                continue
            reply = struct.pack(FMT, RESP, cid, seq, prio, t1, t2, time.time())  # t3 = departure
            sock.sendto(reply, addr)
    except KeyboardInterrupt:
        print("\n[Server] bye")
    finally:
        sock.close()


# ---------------------------------------------------------------- client
class Clock:
    """Fake hardware clock: real time + skew + constant drift. No state mutated on read."""
    def __init__(self, skew, drift_ppm=10.0):
        self.skew = skew #fake initial time offset(how far ahead/behind client clock initially is)
        self.drift = drift_ppm * 1e-6 #hardware oscillator imperfections, as time passes client clock drifts further away from true time, n need to keep correcting it
        self.start = time.time()

    def now(self):
        t = time.time()
        return t + self.skew + self.drift * (t - self.start)

    def error(self):                                  # how wrong we are vs real time
        return self.now() - time.time()


def exchange(sock, addr, clock, cid, seq, timeout=1.0):
    """Send one request, wait for the MATCHING reply. Returns (t1,t2,t3,t4) or None."""
    t1 = clock.now()
    sock.sendto(struct.pack(FMT, REQ, cid, seq, 1, t1, 0.0, 0.0), addr)
    deadline = time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            data, src = sock.recvfrom(2048)
        except (socket.timeout, ConnectionResetError):   # 2nd one = Windows ICMP quirk
            return None
        t4 = clock.now()
        if src != addr or len(data) != SIZE:
            continue
        typ, rcid, rseq, _, _, t2, t3 = struct.unpack(FMT, data)
        if typ == RESP and rcid == cid and rseq == seq:  # ignore stale/foreign replies
            return t1, t2, t3, t4


def run_client(host, port, cid, skew, rounds, interval, log, gain=0.4):
    addr = (host, port)
    clock = Clock(skew)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)   # DSCP EF
    except OSError:
        pass

    with (open(log, "w", newline="") if log else nullcontext()) as f:
        writer = csv.writer(f) if f else None
        if writer:
            writer.writerow(["timestamp", "client_id", "seq", "rtt_ms", "offset_ms", "true_error_ms"])

        for seq in range(rounds):
            result = exchange(sock, addr, clock, cid, seq)
            if result is None:
                print(f"[Client {cid}] seq {seq:02d}: timeout")
            else:
                t1, t2, t3, t4 = result
                rtt = (t4 - t1) - (t3 - t2)
                offset = ((t2 - t1) + (t3 - t4)) / 2
                err = clock.error()                  # error BEFORE correction
                clock.skew += gain * offset          # simple proportional correction
                print(f"[Client {cid}] seq {seq:02d} | rtt {rtt*1000:7.2f} ms | "
                      f"offset {offset*1000:8.2f} ms | true error {err*1000:8.2f} ms")
                if writer:
                    writer.writerow([time.time(), cid, seq, rtt * 1000, offset * 1000, err * 1000])
            time.sleep(interval)
    sock.close()


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["server", "client"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--id", type=int, default=1)
    p.add_argument("--skew", type=float, default=0.25)
    p.add_argument("--rounds", type=int, default=50)
    p.add_argument("--interval", type=float, default=0.1)
    p.add_argument("--log", default=None)
    a = p.parse_args()

    if a.mode == "server":
        run_server(a.port)
    else:
        run_client(a.host, a.port, a.id, a.skew, a.rounds, a.interval, a.log)