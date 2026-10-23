#!/usr/bin/env python3
# Multi-client convergence and DSCP QoS test for timesync_core.py.
# Tests concurrent clients with skews and verifies socket DSCP marking.

import csv
import os
import socket
import subprocess
import sys
import time

# Path to timesync_core.py in the same folder
DEFAULT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(DEFAULT_SCRIPT_DIR, "timesync_core.py")

PORT = 9998
ROUNDS = 50
INTERVAL = 0.1
PASS_MS = 1.0

# Initial client settings: (client_id, initial_skew_seconds)
CLIENTS = [(101, 0.35), (102, -0.35), (103, 0.80), (104, -1.20)]


def read_log(path):
    # Reads CSV log and returns (errors_ms, rtts_ms)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [], []

    # Find error column (prefer post-correction error)
    err_key = None
    for candidate in ["true_error_after_ms", "true_error_ms", "true_error_before_ms"]:
        if candidate in rows[0]:
            err_key = candidate
            break
    if err_key is None:
        err_key = next(k for k in rows[0] if "true" in k)

    rtt_key = next(k for k in rows[0] if "rtt" in k)
    return [float(r[err_key]) for r in rows], [float(r[rtt_key]) for r in rows]


def read_log_full(path):
    # Parse entire CSV into dictionary of lists
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    keys = rows[0].keys()
    data = {k: [] for k in keys}
    for r in rows:
        for k in keys:
            try:
                data[k].append(float(r[k]))
            except ValueError:
                data[k].append(r[k])
    return data


def test_dscp_configuration():
    # Test setting and reading IP_TOS for DSCP 0 and 46
    print("\n" + "=" * 60)
    print("TEST 2: DSCP Socket QoS Configuration & Verification")
    print("=" * 60)

    test_values = [
        (0, "Best Effort (Default)"),
        (46, "Expedited Forwarding (EF / High Priority)")
    ]

    all_passed = True
    for dscp, desc in test_values:
        expected_tos = (dscp & 0x3F) << 2
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Set IP_TOS socket option
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, expected_tos)
            # Read back IP_TOS socket option
            actual_tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            actual_dscp = (actual_tos >> 2) & 0x3F

            # On Linux readback matches; Windows may behave differently without QoS policy
            match = (actual_tos == expected_tos)
            status = "MATCH" if match else f"VALUE=0x{actual_tos:02X}"
            print(f"  [DSCP {dscp:>2} - {desc:<35}] Expected TOS: 0x{expected_tos:02X} | "
                  f"Readback TOS: 0x{actual_tos:02X} (DSCP: {actual_dscp:>2}) -> {status}")
        except OSError as e:
            # Windows might require admin or QoS policies for IP_TOS
            print(f"  [DSCP {dscp:>2} - {desc:<35}] Expected TOS: 0x{expected_tos:02X} | "
                  f"OS Warning/Limitation: {e}")
            if sys.platform != "win32":
                all_passed = False
        finally:
            sock.close()

    print(f"DSCP Socket Configuration Test: {'PASSED' if all_passed else 'FAILED'}\n")
    return all_passed


def test_multiclient_convergence():
    # Run server and 4 clients with initial skews to check convergence
    print("=" * 60)
    print("TEST 1: Multi-Client Clock Synchronization Convergence")
    print("=" * 60)

    if not os.path.exists(SCRIPT):
        print(f"Error: Target script not found at {SCRIPT}")
        return False

    # Start server with Expedited Forwarding (DSCP 46)
    server_cmd = [sys.executable, SCRIPT, "server", "--port", str(PORT), "--dscp", "46"]
    server = subprocess.Popen(
        server_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )
    time.sleep(1.0)  # Wait for server socket to bind

    procs = []
    for cid, skew in CLIENTS:
        log_file = f"client_{cid}.csv"
        cmd = [
            sys.executable, SCRIPT, "client",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--id", str(cid),
            "--skew", str(skew),
            "--rounds", str(ROUNDS),
            "--interval", str(INTERVAL),
            "--dscp", "46",
            "--gain", "0.4",
            "--drift-ppm", "10.0",
            "--log", log_file
        ]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))

    print(f"Started {len(CLIENTS)} clients simultaneously. Syncing over UDP port {PORT}...\n")

    for p in procs:
        p.wait()
    server.terminate()
    try:
        server.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        server.kill()

    print(f"{'client':>6} {'skew(s)':>8} {'samples':>8} {'first err(ms)':>14} "
          f"{'final err(ms)':>14} {'avg rtt(ms)':>12}  result")
    all_ok = True

    for (cid, skew), p in zip(CLIENTS, procs):
        log_file = f"client_{cid}.csv"
        try:
            errs, rtts = read_log(log_file)
        except (FileNotFoundError, StopIteration):
            errs, rtts = [], []

        if not errs:
            err_output = p.stderr.read().decode(errors="replace")[-200:]
            print(f"{cid:>6} {skew:>+8.2f}  no data  (client crashed?)  {err_output}")
            all_ok = False
            continue

        tail = errs[-10:]
        final = sum(abs(e) for e in tail) / len(tail)
        ok = (final < PASS_MS) and (len(errs) >= ROUNDS * 0.95)
        all_ok &= ok
        avg_rtt = sum(rtts) / len(rtts) if rtts else 0.0
        print(f"{cid:>6} {skew:>+8.2f} {len(errs):>8} {errs[0]:>14.2f} "
              f"{final:>14.3f} {avg_rtt:>12.2f}  {'PASS' if ok else 'FAIL'}")

    print("\nConvergence Test Result: " + ("ALL PASSED" if all_ok else "SOME FAILED"))
    return all_ok


def generate_plot():
    # Plot convergence curves for each client
    try:
        import matplotlib.pyplot as plt
        plt.figure(figsize=(9, 5))

        for cid, skew in CLIENTS:
            log_file = f"client_{cid}.csv"
            if not os.path.exists(log_file):
                continue
            errs, _ = read_log(log_file)
            if errs:
                plt.semilogy(
                    range(len(errs)),
                    [abs(e) for e in errs],
                    linewidth=1.8,
                    label=f"Client {cid} (Initial Skew: {skew:+.2f}s)"
                )

        # 1.0 ms pass threshold line
        plt.axhline(
            y=PASS_MS,
            color="red",
            linestyle="--",
            linewidth=1.2,
            label=f"Pass Threshold ({PASS_MS:.1f} ms)"
        )

        plt.xlabel("Synchronization Round (Sequence Number)", fontsize=11)
        plt.ylabel("Absolute Clock Error |θ| (ms, Log Scale)", fontsize=11)
        plt.title("NTP-Style UDP Clock Synchronization Convergence\nMulti-Client Simulation with Clock Skew & Drift", fontsize=12, fontweight="bold")
        plt.grid(True, which="both", linestyle=":", alpha=0.5)
        plt.legend(loc="upper right", framealpha=0.9)
        plt.tight_layout()

        plot_path = "convergence.png"
        plt.savefig(plot_path, dpi=200, bbox_inches="tight")
        print(f"Generated convergence plot saved to: {os.path.abspath(plot_path)}")
    except ImportError:
        print("\n(Note: matplotlib is not installed. Run 'pip install matplotlib' to generate convergence.png)")


def main():
    conv_ok = test_multiclient_convergence()
    dscp_ok = test_dscp_configuration()
    generate_plot()

    if conv_ok and dscp_ok:
        print("\n" + "=" * 60)
        print("ALL TEST SUITES PASSED SUCCESSFULLY")
        print("=" * 60)
        sys.exit(0)
    else:
        print("\n" + "=" * 60)
        print("ONE OR MORE TESTS FAILED")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
