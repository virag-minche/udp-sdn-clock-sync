#!/usr/bin/env python3
# Standalone UDP echo probe client and server for DSCP testing.
# Used to check latency, jitter, and packet marking (DSCP/TOS).

import sys
import time
import socket
import struct
import argparse
import math

DEFAULT_PORT = 9999
DEFAULT_COUNT = 10
DEFAULT_DSCP = 46  # Expedited Forwarding (EF)
DEFAULT_TIMEOUT = 2.0

# Probe packet format: magic (13 bytes), seq (uint32), dscp (uint8), timestamp (double)
PROBE_MAGIC = b"PROBE_SYNC_V1"
HEADER_FORMAT = "!13sIQd"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def dscp_to_tos(dscp):
    # Convert 6-bit DSCP to 8-bit IP TOS (shift left 2 bits)
    return (dscp & 0x3F) << 2


def tos_to_dscp(tos):
    # Extract 6-bit DSCP from 8-bit IP TOS
    return (tos >> 2) & 0x3F


def configure_socket_tos(sock, dscp):
    # Set IP_TOS on socket to mark outgoing packets
    tos_val = dscp_to_tos(dscp)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos_val)
    except OSError as err:
        print(f"[WARN] Failed to set IP_TOS to {tos_val} (DSCP {dscp}): {err}", file=sys.stderr)


def enable_recv_tos(sock):
    # Enable IP_RECVTOS to inspect incoming packet TOS
    IP_RECVTOS = getattr(socket, "IP_RECVTOS", 13)
    try:
        sock.setsockopt(socket.IPPROTO_IP, IP_RECVTOS, 1)
        return True
    except OSError:
        return False


def run_server(bind_ip, port):
    # UDP echo server: receives probe packets and bounces them back
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    has_recvtos = enable_recv_tos(sock)

    try:
        sock.bind((bind_ip, port))
    except OSError as err:
        print(f"[ERROR] Failed to bind to {bind_ip}:{port}: {err}", file=sys.stderr)
        sys.exit(1)

    print("============================================================")
    print(f"  DSCP Probe Server listening on {bind_ip}:{port}")
    print(f"  IP_RECVTOS enabled: {has_recvtos}")
    print("  Press Ctrl+C to terminate.")
    print("============================================================")

    pkt_count = 0
    try:
        while True:
            if has_recvtos:
                try:
                    data, ancdata, flags, addr = sock.recvmsg(4096, 512)
                except (AttributeError, OSError):
                    data, addr = sock.recvfrom(4096)
                    ancdata = []
            else:
                data, addr = sock.recvfrom(4096)
                ancdata = []

            recv_time = time.time()
            pkt_count += 1

            # Extract TOS byte from ancillary data if available
            extracted_tos = None
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.IPPROTO_IP:
                    if len(cmsg_data) >= 1:
                        extracted_tos = cmsg_data[0]

            # Parse probe packet header
            seq = -1
            req_dscp = -1
            t_send = 0.0
            if len(data) >= HEADER_SIZE:
                try:
                    magic, seq, req_dscp, t_send = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
                    if magic == PROBE_MAGIC:
                        one_way_ms = (recv_time - t_send) * 1000.0
                        tos_str = f"TOS=0x{extracted_tos:02x} (DSCP {tos_to_dscp(extracted_tos)})" if extracted_tos is not None else f"DSCP={req_dscp}"
                        print(f"[{pkt_count:04d}] Echoed packet from {addr[0]}:{addr[1]} | seq={seq} | {tos_str} | 1-way~{one_way_ms:.3f}ms")
                except struct.error:
                    pass

            # Echo payload back with matching DSCP priority
            if req_dscp >= 0:
                configure_socket_tos(sock, req_dscp)
            sock.sendto(data, addr)

    except KeyboardInterrupt:
        print("\n[INFO] Server shutting down gracefully.")
    finally:
        sock.close()


def run_client(host, port, dscp, count, interval, timeout):
    # UDP echo client: sends probes, records RTT, and prints stats
    tos_val = dscp_to_tos(dscp)
    print("============================================================")
    print(f"  DSCP Probe Client -> {host}:{port}")
    print(f"  Target DSCP: {dscp} (TOS: 0x{tos_val:02x} / {tos_val})")
    print(f"  Packet Count: {count} | Interval: {interval}s | Timeout: {timeout}s")
    print("============================================================")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    configure_socket_tos(sock, dscp)

    rtts_ms = []
    lost_count = 0

    for seq in range(1, count + 1):
        t_send = time.time()
        payload = struct.pack(HEADER_FORMAT, PROBE_MAGIC, seq, dscp, t_send) + b"X" * 32

        try:
            sock.sendto(payload, (host, port))
            resp_data, _ = sock.recvfrom(4096)
            t_recv = time.time()

            rtt_ms = (t_recv - t_send) * 1000.0
            rtts_ms.append(rtt_ms)

            # Check echoed payload
            valid = False
            if len(resp_data) >= HEADER_SIZE:
                magic, echoed_seq, _, _ = struct.unpack(HEADER_FORMAT, resp_data[:HEADER_SIZE])
                if magic == PROBE_MAGIC and echoed_seq == seq:
                    valid = True

            valid_str = "VALID" if valid else "CORRUPTED"
            print(f"  [{seq:03d}/{count:03d}] RTT = {rtt_ms:6.3f} ms | Status: {valid_str}")

        except socket.timeout:
            lost_count += 1
            print(f"  [{seq:03d}/{count:03d}] TIMEOUT (packet lost or server down)")
        except Exception as exc:
            lost_count += 1
            print(f"  [{seq:03d}/{count:03d}] ERROR: {exc}")

        if seq < count:
            time.sleep(interval)

    sock.close()

    # Print summary stats
    print("\n------------------------------------------------------------")
    print("  PROBE RESULTS SUMMARY")
    print("------------------------------------------------------------")
    sent = count
    received = len(rtts_ms)
    loss_pct = (lost_count / sent) * 100.0

    print(f"  Packets Transmitted : {sent}")
    print(f"  Packets Received    : {received}")
    print(f"  Packet Loss         : {loss_pct:.1f}%")

    if rtts_ms:
        min_rtt = min(rtts_ms)
        max_rtt = max(rtts_ms)
        mean_rtt = sum(rtts_ms) / len(rtts_ms)
        variance = sum((x - mean_rtt) ** 2 for x in rtts_ms) / len(rtts_ms)
        std_rtt = math.sqrt(variance)

        # Jitter: mean consecutive RTT difference
        if len(rtts_ms) > 1:
            diffs = [abs(rtts_ms[i+1] - rtts_ms[i]) for i in range(len(rtts_ms) - 1)]
            jitter = sum(diffs) / len(diffs)
        else:
            jitter = 0.0

        print(f"  Min RTT             : {min_rtt:6.3f} ms")
        print(f"  Mean RTT            : {mean_rtt:6.3f} ms")
        print(f"  Max RTT             : {max_rtt:6.3f} ms")
        print(f"  Std Dev             : {std_rtt:6.3f} ms")
        print(f"  Jitter (mean diff)  : {jitter:6.3f} ms")
    else:
        print("  [FAIL] All probe packets were dropped.")

    print("------------------------------------------------------------\n")


def main():
    parser = argparse.ArgumentParser(
        description="Standalone DSCP-46 UDP Echo Probe Client/Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Start Server:
    python3 probe.py server --port 9999

  Run Probe Client with DSCP 46 (Expedited Forwarding):
    python3 probe.py client --host 10.0.0.100 --port 9999 --dscp 46 --count 10

  Run Probe Client with Best Effort (DSCP 0):
    python3 probe.py client --host 10.0.0.100 --port 9999 --dscp 0 --count 10
        """
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, help="Mode of operation")

    server_parser = subparsers.add_parser("server", help="Run in UDP echo server mode")
    server_parser.add_argument("--bind", default="0.0.0.0", help="IP address to bind server (default: 0.0.0.0)")
    server_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP listening port (default: 9999)")

    client_parser = subparsers.add_parser("client", help="Run in probe client mode")
    client_parser.add_argument("--host", default="10.0.0.100", help="Destination server IP address (default: 10.0.0.100)")
    client_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Destination UDP port (default: 9999)")
    client_parser.add_argument("--dscp", type=int, default=DEFAULT_DSCP, help="DSCP code point (0-63, default: 46 for EF)")
    client_parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Number of packets to transmit (default: 10)")
    client_parser.add_argument("--interval", type=float, default=0.2, help="Interval between probes in seconds (default: 0.2)")
    client_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-packet reply timeout in seconds (default: 2.0)")

    args = parser.parse_args()

    if args.mode == "server":
        run_server(args.bind, args.port)
    elif args.mode == "client":
        if not (0 <= args.dscp <= 63):
            print("[ERROR] DSCP value must be in range 0..63", file=sys.stderr)
            sys.exit(1)
        run_client(args.host, args.port, args.dscp, args.count, args.interval, args.timeout)


if __name__ == "__main__":
    main()
