#!/usr/bin/env python3
# Simple UDP traffic generator and sink.
# Used to generate background cross-traffic in Mininet to create link congestion.

import argparse
import socket
import time


def configure_dscp(sock, dscp=0):
    # Set IP_TOS based on 6-bit DSCP value
    tos = (dscp & 0x3F) << 2
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
    except OSError as err:
        print(f"[LoadGen] Warning: Could not set IP_TOS=0x{tos:02X}: {err}")


def run_udp_sink(port=5201, duration=0):
    # Listens on UDP port to absorb background traffic cleanly
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)

    print(f"[LoadSink] Listening for background UDP traffic on 0.0.0.0:{port}")
    start_time = time.monotonic()
    total_bytes = 0
    total_pkts = 0

    try:
        while True:
            if duration > 0 and (time.monotonic() - start_time) >= duration:
                break
            try:
                data, _ = sock.recvfrom(65535)
                total_bytes += len(data)
                total_pkts += 1
            except socket.timeout:
                continue
    except KeyboardInterrupt:
        print("\n[LoadSink] Stopped by user.")
    finally:
        sock.close()

    elapsed = max(0.001, time.monotonic() - start_time)
    mbps = (total_bytes * 8.0) / (elapsed * 1_000_000.0)
    print(f"[LoadSink] Received {total_pkts} packets ({total_bytes} bytes, avg {mbps:.2f} Mbps)")


def generate_udp_traffic(host, port, rate, duration, packet_size, dscp=0):
    # Sends UDP packets at a target rate and packet size
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    configure_dscp(sock, dscp=dscp)

    message = b"X" * packet_size
    interval = 1.0 / rate
    start_wall = time.monotonic()
    end_time = start_wall + duration
    packets_sent = 0
    bytes_sent = 0

    target_mbps = (rate * packet_size * 8.0) / 1_000_000.0
    print(f"[LoadGen] Sending UDP traffic to {host}:{port} (DSCP={dscp})")
    print(f"[LoadGen] Rate: {rate} pkt/s | Packet size: {packet_size} B | Target: {target_mbps:.2f} Mbps | Duration: {duration}s")

    try:
        next_send = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= end_time:
                break

            try:
                sock.sendto(message, (host, port))
                packets_sent += 1
                bytes_sent += packet_size
            except OSError:
                pass

            next_send += interval
            sleep_dur = next_send - time.monotonic()
            if sleep_dur > 0.0005:
                time.sleep(sleep_dur)
            elif sleep_dur < -0.1:
                # Catch up if lagging behind
                next_send = time.monotonic()

    except KeyboardInterrupt:
        print("\n[LoadGen] Traffic generation stopped by user.")

    finally:
        sock.close()

    actual_dur = max(0.001, time.monotonic() - start_wall)
    actual_mbps = (bytes_sent * 8.0) / (actual_dur * 1_000_000.0)
    print(f"[LoadGen] Total packets sent: {packets_sent} ({bytes_sent} bytes, {actual_mbps:.2f} Mbps over {actual_dur:.2f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Background UDP traffic generator and sink"
    )

    parser.add_argument("--host", default="10.0.0.201",
                        help="Destination host IP address (default: 10.0.0.201)")
    parser.add_argument("--port", type=int, default=5201,
                        help="Destination UDP port (default: 5201)")
    parser.add_argument("--rate", type=int, default=100,
                        help="Packets per second (default: 100)")
    parser.add_argument("--duration", type=int, default=10,
                        help="Duration in seconds (default: 10)")
    parser.add_argument("--size", type=int, default=1024,
                        help="Packet size in bytes (default: 1024)")
    parser.add_argument("--mbps", type=float, default=None,
                        help="Target bandwidth in Mbit/s (e.g. 9.0)")
    parser.add_argument("--dscp", type=int, default=0,
                        help="DSCP value for packets (default: 0 = Best Effort)")
    parser.add_argument("--sink", action="store_true",
                        help="Run as UDP receiver/sink on --port instead of sender")

    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")

    if args.sink:
        run_udp_sink(port=args.port, duration=args.duration if args.duration != 10 else 0)
    else:
        if not 1 <= args.size <= 65507:
            parser.error("Packet size must be between 1 and 65507")

        rate = args.rate
        if args.mbps is not None:
            if args.mbps <= 0:
                parser.error("--mbps must be positive")
            # Calculate packets/sec needed for target Mbps with args.size bytes
            rate = max(1, int((args.mbps * 1_000_000.0) / (args.size * 8.0)))

        if rate <= 0 or args.duration <= 0:
            parser.error("Rate and duration must be positive")

        generate_udp_traffic(
            args.host,
            args.port,
            rate,
            args.duration,
            args.size,
            dscp=args.dscp
        )
