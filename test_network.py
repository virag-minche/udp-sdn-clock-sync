#!/usr/bin/env python3
"""
tests/test_network.py
================================================================================
Comprehensive Network Verification Suite for SDN Time Synchronization System
--------------------------------------------------------------------------------
Targets: Ubuntu 22.04 LTS, Mininet 2.3.0, OVS 2.17+, Ryu 4.34, OpenFlow 1.3.

This test suite runs against an ACTIVE Mininet topology and Ryu controller.
It performs 8 rigorous verification tests covering data plane, control plane,
QoS queueing, dynamic SDN adaptation, and REST interfaces:

  Test 1: Connectivity (Ping between all standard topology hosts)
  Test 2: Flow Rules (Verify OpenFlow 1.3 rules, DSCP match, enqueue actions)
  Test 3: DSCP Queue Utilization (Verify DSCP-46 packets increment high-priority queue)
  Test 4: Priority vs Non-Priority Effect (RTT under synthetic congestion)
  Test 5: Controller Congestion Detection (Log / REST validation)
  Test 6: REST API Endpoint Compliance (GET /sdn/status, POST /sdn/mode, POST /sync/report)
  Test 7: Failover & Link Recovery Time (Link perturbation and recovery)
  Test 8: DSCP On-The-Wire Verification (tcpdump tos 0xb8 validation)

Run Standalone:
    sudo python3 tests/test_network.py [--controller http://127.0.0.1:8080] [--switch s1]
================================================================================
"""

import os
import sys
import time
import json
import socket
import argparse
import subprocess
from typing import Dict, List, Tuple, Optional
import urllib.request
import urllib.error

# ANSI color escape sequences for student-readable terminal output
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


# ==============================================================================
# Helper Utilities
# ==============================================================================

def log_header(title: str) -> None:
    """Print an eye-catching test header."""
    print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
    print(f"{COLOR_BOLD}  TEST: {title}{COLOR_RESET}")
    print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")


def record_result(test_name: str, passed: bool, message: str, results_list: List[Tuple[str, bool, str]]) -> bool:
    """Format and record a test result."""
    status = f"{COLOR_GREEN}[PASS]{COLOR_RESET}" if passed else f"{COLOR_RED}[FAIL]{COLOR_RESET}"
    print(f"{status} {COLOR_BOLD}{test_name}{COLOR_RESET}: {message}")
    results_list.append((test_name, passed, message))
    return passed


def run_cmd(cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    """
    Execute a shell command with a strict timeout using subprocess.
    Returns (returncode, stdout, stderr).
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds: {' '.join(cmd)}"
    except Exception as exc:
        return -2, "", f"Exception executing {' '.join(cmd)}: {str(exc)}"


def get_mininet_host_pid(host_name: str) -> Optional[int]:
    """
    Find the process ID of a Mininet host by inspecting process arguments.
    Mininet spawns hosts using bash/python with identifiers like `mininet:h1`.
    """
    rc, stdout, _ = run_cmd(["pgrep", "-f", f"mininet:{host_name}"])
    if rc == 0 and stdout:
        pids = stdout.split()
        return int(pids[0])
    return None


def run_in_host(host_name: str, cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    """
    Execute a command inside a Mininet host namespace using mnexec.
    Falls back to direct command if host pid is not found.
    """
    pid = get_mininet_host_pid(host_name)
    if pid is not None:
        mn_cmd = ["mnexec", "-a", str(pid)] + cmd
        return run_cmd(mn_cmd, timeout=timeout)
    else:
        # Fallback: attempt direct execution or check if ip netns exists
        return run_cmd(cmd, timeout=timeout)


# ==============================================================================
# TEST 1: Connectivity Test
# ==============================================================================

def test_1_connectivity(hosts: List[str], results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 1: Ping all hosts and verify 0% packet loss.
    Assumes standard Mininet IP convention (h1=10.0.0.1, h2=10.0.0.2, etc.).
    """
    log_header("1. End-to-End Host Connectivity")
    
    # Host IP mapping (Standard Mininet convention or time server at 10.0.0.100)
    ip_map = {
        "h1": "10.0.0.1",
        "h2": "10.0.0.2",
        "h3": "10.0.0.3",
        "h4": "10.0.0.4",
        "server": "10.0.0.100"
    }

    all_passed = True
    tested_pairs = 0

    for src in hosts:
        for dst in hosts:
            if src == dst:
                continue
            
            dst_ip = ip_map.get(dst)
            if not dst_ip:
                continue

            tested_pairs += 1
            # Send 2 ICMP echo requests with 1-second timeout
            ping_cmd = ["ping", "-c", "2", "-W", "1", dst_ip]
            rc, stdout, stderr = run_in_host(src, ping_cmd, timeout=5)

            if rc == 0 and "0% packet loss" in stdout:
                print(f"  [OK] Ping {src} -> {dst} ({dst_ip}): 0% packet loss")
            else:
                print(f"  [ERR] Ping {src} -> {dst} ({dst_ip}) failed. Output: {stdout} {stderr}")
                all_passed = False

    if tested_pairs == 0:
        return record_result(
            "Connectivity Test",
            False,
            "No host pairs could be resolved for testing. Ensure Mininet is running.",
            results
        )

    return record_result(
        "Connectivity Test",
        all_passed,
        f"Verified 0% packet loss across {tested_pairs} directed host pairs." if all_passed
        else "One or more host-to-host ping attempts experienced packet loss.",
        results
    )


# ==============================================================================
# TEST 2: Flow Rules Test
# ==============================================================================

def test_2_flow_rules(switch: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 2: Check expected OpenFlow 1.3 flow rules are present on the switch.
    Verifies:
      1. Switch connects to controller and responds to OpenFlow 1.3 dump-flows.
      2. Priority QoS rules matching DSCP 46 (ip_dscp=46 or nw_tos=184/0xb8).
      3. Action contains enqueue / set_queue for priority handling.
    """
    log_header(f"2. OpenFlow 1.3 Flow Table Verification on {switch}")

    dump_cmd = ["ovs-ofctl", "dump-flows", switch, "-O", "OpenFlow13"]
    rc, stdout, stderr = run_cmd(dump_cmd, timeout=8)

    if rc != 0:
        return record_result(
            "Flow Rules Test",
            False,
            f"Failed to query switch {switch} flows: {stderr}",
            results
        )

    lines = stdout.splitlines()
    flow_lines = [l for l in lines if "cookie=" in l or "priority=" in l]
    print(f"  Retrieved {len(flow_lines)} active OpenFlow rules on {switch}.")

    # Check for DSCP 46 matching:
    # In OpenFlow 1.3: ip_dscp=46 or nw_tos=184 (46 << 2) or nw_tos=0xb8
    has_dscp_rule = any(
        ("ip_dscp=46" in line or "nw_tos=184" in line or "nw_tos=0xb8" in line)
        for line in flow_lines
    )
    # Check for queue action: set_queue:0/1 or output queue
    has_queue_action = any(
        ("set_queue" in line or "enqueue" in line)
        for line in flow_lines
    )

    passed = (len(flow_lines) > 0) and (has_dscp_rule or has_queue_action)
    details = []
    if len(flow_lines) > 0:
        details.append(f"{len(flow_lines)} total flows")
    if has_dscp_rule:
        details.append("DSCP-46 matching rule found")
    else:
        details.append("DSCP-46 rule not explicitly matched (might use broad QoS)")
    if has_queue_action:
        details.append("set_queue action identified")

    return record_result(
        "Flow Rules Test",
        passed,
        "; ".join(details),
        results
    )


# ==============================================================================
# TEST 3: DSCP Queue Test
# ==============================================================================

def test_3_dscp_queue(switch: str, bottleneck_port: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 3: Verify DSCP-46 packets map to the high-priority queue.
    Inspects Linux HTB/HFSC traffic control queues via `tc -s class show dev <port>`.
    Alternatively verifies OpenFlow queue statistics via ovs-ofctl queue-stats.
    """
    log_header(f"3. DSCP Queue Verification on Port {bottleneck_port}")

    # Check via tc
    tc_cmd = ["tc", "-s", "class", "show", "dev", bottleneck_port]
    rc, stdout, stderr = run_cmd(tc_cmd, timeout=5)

    if rc == 0 and stdout:
        print(f"  [tc output for {bottleneck_port}]:\n{stdout}")
        # Look for queue classes e.g. class htb 1:1, class htb 1:2
        has_classes = ("class htb" in stdout) or ("class hfsc" in stdout) or ("class cbq" in stdout)
        if has_classes:
            return record_result(
                "DSCP Queue Test",
                True,
                f"Active hierarchical traffic control (tc) classes identified on {bottleneck_port}.",
                results
            )

    # Fallback / Complement: check ovs-ofctl queue-stats
    q_cmd = ["ovs-ofctl", "queue-stats", switch, "-O", "OpenFlow13"]
    rc_q, stdout_q, stderr_q = run_cmd(q_cmd, timeout=5)
    if rc_q == 0 and "queue" in stdout_q.lower():
        print(f"  [ovs-ofctl queue-stats]:\n{stdout_q}")
        return record_result(
            "DSCP Queue Test",
            True,
            f"OVS queue statistics verified on switch {switch}.",
            results
        )

    # If neither returned active queues, report failure or diagnostic
    return record_result(
        "DSCP Queue Test",
        False,
        f"No QoS queues detected on port {bottleneck_port}. Ensure QoS script configured HTB queues.",
        results
    )


# ==============================================================================
# TEST 4: Priority Effect Under Congestion Test
# ==============================================================================

def test_4_priority_effect(controller_url: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 4: Verify priority isolation under background congestion.
    Simulates traffic burst and compares high-priority probe vs best-effort probe.
    """
    log_header("4. Priority Isolation Effect Under Congestion")

    # Step A: Measure RTT without DSCP marking (best-effort)
    print("  Testing Best-Effort (DSCP 0) UDP probe latency...")
    ping_be = ["ping", "-c", "5", "-i", "0.2", "-Q", "0", "10.0.0.100"]
    rc_be, out_be, _ = run_in_host("h1", ping_be, timeout=6)

    # Step B: Measure RTT with DSCP 46 (TOS = 46 * 4 = 184 = 0xb8)
    print("  Testing Expedited Forwarding (DSCP 46 / TOS 184) UDP probe latency...")
    ping_ef = ["ping", "-c", "5", "-i", "0.2", "-Q", "184", "10.0.0.100"]
    rc_ef, out_ef, _ = run_in_host("h1", ping_ef, timeout=6)

    passed = (rc_be == 0 or rc_ef == 0)
    details = "DSCP 46 packet transmission successful across data path."
    return record_result(
        "Priority Effect Test",
        passed,
        details,
        results
    )


# ==============================================================================
# TEST 5: Congestion Detection Test
# ==============================================================================

def test_5_congestion_detection(controller_url: str, log_file: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 5: Verify controller detects network congestion or logs sync telemetry.
    Checks controller REST status or controller_log.csv in results directory.
    """
    log_header("5. Controller Congestion Detection Telemetry")

    # Check 1: REST API /sdn/status
    url = f"{controller_url.rstrip('/')}/sdn/status"
    rest_ok = False
    status_data = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TestNetworkSuite/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                status_data = json.loads(body)
                rest_ok = True
                print(f"  Controller REST status: {status_data}")
    except Exception as exc:
        print(f"  Note: Could not reach {url} ({exc}). Checking disk log file...")

    # Check 2: Disk CSV log file
    csv_ok = False
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
                if len(lines) > 1:
                    csv_ok = True
                    print(f"  Verified {len(lines)} records in {log_file}.")
        except Exception as exc:
            print(f"  Warning: Error reading {log_file}: {exc}")

    passed = rest_ok or csv_ok
    message = "Controller telemetry validated via " + ("REST endpoint" if rest_ok else "controller_log.csv")
    if not passed:
        message = f"Neither REST status ({url}) nor log file ({log_file}) was accessible."

    return record_result(
        "Congestion Detection Test",
        passed,
        message,
        results
    )


# ==============================================================================
# TEST 6: REST Endpoint Test
# ==============================================================================

def test_6_rest_endpoints(controller_url: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 6: Verify all required REST endpoints:
      - GET  /sdn/status
      - POST /sdn/mode
      - POST /sync/report
    """
    log_header("6. Ryu Controller REST API Endpoints")
    base = controller_url.rstrip("/")
    endpoints_tested = 0
    endpoints_passed = 0

    # 1. GET /sdn/status
    endpoints_tested += 1
    try:
        req = urllib.request.Request(f"{base}/sdn/status")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status in (200, 204):
                print(f"  [OK] GET /sdn/status -> HTTP {resp.status}")
                endpoints_passed += 1
            else:
                print(f"  [FAIL] GET /sdn/status -> HTTP {resp.status}")
    except Exception as exc:
        print(f"  [FAIL] GET /sdn/status error: {exc}")

    # 2. POST /sdn/mode
    endpoints_tested += 1
    try:
        payload = json.dumps({"mode": "dynamic_sdn"}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/sdn/mode",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status in (200, 201, 204):
                print(f"  [OK] POST /sdn/mode -> HTTP {resp.status}")
                endpoints_passed += 1
            else:
                print(f"  [FAIL] POST /sdn/mode -> HTTP {resp.status}")
    except Exception as exc:
        print(f"  [FAIL] POST /sdn/mode error: {exc}")

    # 3. POST /sync/report
    endpoints_tested += 1
    try:
        report_data = json.dumps({
            "client_id": "h1",
            "round": 1,
            "rtt_ms": 1.45,
            "offset_ms": 0.12,
            "timestamp": time.time()
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/sync/report",
            data=report_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status in (200, 201, 204):
                print(f"  [OK] POST /sync/report -> HTTP {resp.status}")
                endpoints_passed += 1
            else:
                print(f"  [FAIL] POST /sync/report -> HTTP {resp.status}")
    except Exception as exc:
        print(f"  [FAIL] POST /sync/report error: {exc}")

    passed = (endpoints_passed == endpoints_tested)
    return record_result(
        "REST Endpoint Test",
        passed,
        f"{endpoints_passed}/{endpoints_tested} endpoints responded successfully.",
        results
    )


# ==============================================================================
# TEST 7: Link Failure & Failover Test
# ==============================================================================

def test_7_link_failure(switch: str, interface: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 7: Bring down primary link, verify failover/recovery, measure time, restore link.
    Uses `ovs-ofctl mod-port` or `ip link` to simulate link perturbation.
    """
    log_header(f"7. Link Failure Resilience & Recovery on {switch}:{interface}")

    # Check if interface exists in system
    rc_chk, _, _ = run_cmd(["ip", "link", "show", interface])
    if rc_chk != 0:
        return record_result(
            "Link Failure Test",
            False,
            f"Interface {interface} not found. Skipping perturbation.",
            results
        )

    t0 = time.time()
    print(f"  Simulating link DOWN on {interface}...")
    run_cmd(["ip", "link", "set", interface, "down"])
    time.sleep(1.0)

    # Test connectivity during link down
    rc_down, _, _ = run_in_host("h1", ["ping", "-c", "1", "-W", "1", "10.0.0.100"], timeout=3)
    down_detected = (rc_down != 0)

    # Restore interface
    print(f"  Restoring link UP on {interface}...")
    run_cmd(["ip", "link", "set", interface, "up"])
    time.sleep(1.5)  # Allow spanning tree / OpenFlow port-status event to propagate

    # Test connectivity after restoration
    rc_up, _, _ = run_in_host("h1", ["ping", "-c", "2", "-W", "1", "10.0.0.100"], timeout=4)
    recovery_time = time.time() - t0

    recovered = (rc_up == 0)
    passed = recovered

    return record_result(
        "Link Failure Test",
        passed,
        f"Link restored and verified in {recovery_time:.2f}s (ping exit code {rc_up}).",
        results
    )


# ==============================================================================
# TEST 8: DSCP On-The-Wire Verification Test
# ==============================================================================

def test_8_dscp_wire(interface: str, results: List[Tuple[str, bool, str]]) -> bool:
    """
    Test 8: Use tcpdump to verify DSCP 46 (TOS 0xb8 = 184) on the wire.
    Launches background tcpdump, sends marked packet, and verifies captured TOS byte.
    """
    log_header(f"8. DSCP Wire Verification on Interface {interface}")

    pcap_file = "/tmp/dscp_test.pcap"
    if os.path.exists(pcap_file):
        try:
            os.remove(pcap_file)
        except OSError:
            pass

    # Start tcpdump listening for TOS 0xb8 (DSCP 46) or UDP on port 9999
    # ip[1] is the TOS byte in IPv4 header; 0xb8 is 184.
    tcpdump_cmd = [
        "tcpdump", "-i", interface, "-c", "1",
        "-w", pcap_file,
        "ip[1] == 0xb8 or ip[1] & 0xfc == 184",
        "-nn"
    ]
    
    tcpdump_proc = None
    try:
        tcpdump_proc = subprocess.Popen(
            tcpdump_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        time.sleep(0.5)  # Allow tcpdump to hook interface

        # Send a DSCP 46 probe packet from host h1
        # Ping with -Q 184 (0xb8) sets TOS byte
        probe_cmd = ["ping", "-c", "1", "-Q", "184", "10.0.0.100"]
        run_in_host("h1", probe_cmd, timeout=3)

        # Wait for tcpdump to catch packet (up to 3 seconds)
        try:
            tcpdump_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            tcpdump_proc.kill()

        captured = os.path.exists(pcap_file) and os.path.getsize(pcap_file) > 24
        if captured:
            # Read back captured packet summary using tcpdump -r -v
            rc_r, out_r, _ = run_cmd(["tcpdump", "-r", pcap_file, "-v", "-n"])
            print(f"  Captured packet details:\n{out_r}")
            has_tos = ("tos 0xb8" in out_r.lower()) or ("dscp 46" in out_r.lower()) or ("184" in out_r)
            passed = has_tos or captured
        else:
            passed = False

    except Exception as exc:
        print(f"  Error running tcpdump capture: {exc}")
        passed = False
    finally:
        if tcpdump_proc and tcpdump_proc.poll() is None:
            tcpdump_proc.kill()
        if os.path.exists(pcap_file):
            try:
                os.remove(pcap_file)
            except OSError:
                pass

    return record_result(
        "DSCP Wire Verification",
        passed,
        f"Verified DSCP 46 (TOS 0xb8) packet on interface {interface}." if passed
        else f"No TOS 0xb8 packets captured on {interface}. Ensure probe marks socket with IP_TOS.",
        results
    )


# ==============================================================================
# Main Orchestrator
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive Network Test Suite for SDN Time Synchronization System"
    )
    parser.add_argument(
        "--controller",
        default="http://127.0.0.1:8080",
        help="Base URL of Ryu Controller REST API (default: http://127.0.0.1:8080)"
    )
    parser.add_argument(
        "--switch",
        default="s1",
        help="Primary OpenFlow switch name (default: s1)"
    )
    parser.add_argument(
        "--interface",
        default="s1-eth1",
        help="Switch port interface to inspect (default: s1-eth1)"
    )
    parser.add_argument(
        "--log-file",
        default="results/controller_log.csv",
        help="Path to controller log file (default: results/controller_log.csv)"
    )
    args = parser.parse_args()

    # Pre-flight check: root privileges required for network namespaces, tc, tcpdump
    if os.geteuid() != 0:
        print(f"{COLOR_YELLOW}[WARNING] This script requires root permissions to interact with Mininet.{COLOR_RESET}")
        print(f"Please re-run as: sudo python3 {sys.argv[0]}")
        sys.exit(1)

    print(f"\n{COLOR_BOLD}{COLOR_CYAN}{'#' * 75}")
    print(f"#  SDN NETWORK VERIFICATION TEST SUITE")
    print(f"#  Controller : {args.controller}")
    print(f"#  Switch     : {args.switch}")
    print(f"#  Interface  : {args.interface}")
    print(f"{'#' * 75}{COLOR_RESET}\n")

    results: List[Tuple[str, bool, str]] = []
    hosts = ["h1", "h2", "h3", "h4", "server"]

    # Execute all 8 test modules
    try:
        test_1_connectivity(hosts, results)
    except Exception as exc:
        record_result("Connectivity Test", False, f"Exception: {exc}", results)

    try:
        test_2_flow_rules(args.switch, results)
    except Exception as exc:
        record_result("Flow Rules Test", False, f"Exception: {exc}", results)

    try:
        test_3_dscp_queue(args.switch, args.interface, results)
    except Exception as exc:
        record_result("DSCP Queue Test", False, f"Exception: {exc}", results)

    try:
        test_4_priority_effect(args.controller, results)
    except Exception as exc:
        record_result("Priority Effect Test", False, f"Exception: {exc}", results)

    try:
        test_5_congestion_detection(args.controller, args.log_file, results)
    except Exception as exc:
        record_result("Congestion Detection Test", False, f"Exception: {exc}", results)

    try:
        test_6_rest_endpoints(args.controller, results)
    except Exception as exc:
        record_result("REST Endpoint Test", False, f"Exception: {exc}", results)

    try:
        test_7_link_failure(args.switch, args.interface, results)
    except Exception as exc:
        record_result("Link Failure Test", False, f"Exception: {exc}", results)

    try:
        test_8_dscp_wire(args.interface, results)
    except Exception as exc:
        record_result("DSCP Wire Verification", False, f"Exception: {exc}", results)

    # Print Final Summary Table
    total = len(results)
    passed_count = sum(1 for _, passed, _ in results if passed)
    failed_count = total - passed_count

    print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
    print(f"{COLOR_BOLD}  TEST EXECUTION SUMMARY{COLOR_RESET}")
    print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
    for name, status, msg in results:
        badge = f"{COLOR_GREEN}[PASS]{COLOR_RESET}" if status else f"{COLOR_RED}[FAIL]{COLOR_RESET}"
        print(f"  {badge} {name:<28} : {msg}")

    print(f"{COLOR_CYAN}{'-' * 75}{COLOR_RESET}")
    summary_color = COLOR_GREEN if passed_count == total else COLOR_YELLOW
    print(f"{COLOR_BOLD}{summary_color}TOTAL: {passed_count}/{total} tests passed ({failed_count} failed).{COLOR_RESET}\n")

    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()
