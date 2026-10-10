
import socket
import time
import argparse


def generate_udp_traffic(host, port, rate, duration, packet_size):
    """
    Generate background UDP traffic for network experiments.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    message = b"X" * packet_size
    interval = 1.0 / rate
    end_time = time.monotonic() + duration
    packets_sent = 0

    print(f"Sending UDP traffic to {host}:{port}")
    print(f"Rate: {rate} packets/second")
    print(f"Duration: {duration} seconds")

    try:
        while time.monotonic() < end_time:
            start = time.monotonic()

            sock.sendto(message, (host, port))
            packets_sent += 1

            elapsed = time.monotonic() - start
            time.sleep(max(0, interval - elapsed))

    except KeyboardInterrupt:
        print("\nTraffic generation stopped by user.")

    finally:
        sock.close()

    print(f"Total packets sent: {packets_sent}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Background UDP traffic generator"
    )

    parser.add_argument("--host", required=True,
                        help="Destination host IP address")
    parser.add_argument("--port", type=int, required=True,
                        help="Destination UDP port")
    parser.add_argument("--rate", type=int, default=100,
                        help="Packets per second")
    parser.add_argument("--duration", type=int, default=10,
                        help="Duration in seconds")
    parser.add_argument("--size", type=int, default=512,
                        help="Packet size in bytes")

    args = parser.parse_args()

    if args.rate <= 0 or args.duration <= 0:
        parser.error("Rate and duration must be positive")

    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")

    if not 1 <= args.size <= 65507:
        parser.error("Packet size must be between 1 and 65507")

    generate_udp_traffic(
        args.host,
        args.port,
        args.rate,
        args.duration,
        args.size
    )
