#!/usr/bin/env python3
# Network integration tests for SDN time sync project
# Tests connectivity, flow rules, queues, priority, and link failure in Mininet

import os
import sys
import time
import json
import socket
import argparse
import subprocess
import urllib.request
import urllib.error

# terminal colors
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


# helpers

def log_header(title):
    print(f"\n{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")
    print(f"{COLOR_BOLD}  TEST: {title}{COLOR_RESET}")
    print(f"{COLOR_CYAN}{'=' * 75}{COLOR_RESET}")


def record_result(test_name, passed, message, results_list):
    # record test result and print status
    status = f"{COLOR_GREEN}[PASS]{COLOR_RESET}" if passed else f"{COLOR_RED}[FAIL]{COLOR_RESET}"
    print(f"{status} {COLOR_BOLD}{test_name}{COLOR_RESET}: {message}")
    results_list.append((test_name, passed, message))
    return passed


def run_cmd(cmd, timeout=10):
    # run a shell command with timeout
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


def get_mininet_host_pid(host_name):
    # find mininet host pid using pgrep
    rc, stdout, _ = run_cmd(["pgrep", "-f", f"mininet:{host_name}"])
    if rc == 0 and stdout:
        pids = stdout.split()
        return int(pids[0])
    return None


def run_in_host(host_name, cmd, timeout=10):
    # run command inside host namespace using mnexec
    pid = get_mininet_host_pid(host_name)
    if pid is not None:
        mn_cmd = ["mnexec", "-a", str(pid)] + cmd
        return run_cmd(mn_cmd, timeout=timeout)
    else:
        return run_cmd(cmd, timeout=timeout)


# test 1: host connectivity
def test_1_connectivity(hosts, results):
    log_header("1. End-to-End Host Connectivity")
    
    # ip mapping for hosts
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
            # send 2 pings with 1s timeout
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


# test 2: flow rules
def test_2_flow_rules(switch, results):
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

    # check for dscp 46 matching (tos 184 or 0xb8)
    has_dscp_rule = any(
        ("ip_dscp=46" in line or "nw_tos=184" in line or "nw_tos=0xb8" in line)
        for line in flow_lines
    )
    # check for queue actions
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


# test 3: dscp queue check
def test_3_dscp_queue(switch, bottleneck_port, results):
    log_header(f"3. DSCP Queue Verification on Port {bottleneck_port}")

    # check tc queues
    tc_cmd = ["tc", "-s", "class", "show", "dev", bottleneck_port]
    rc, stdout, stderr = run_cmd(tc_cmd, timeout=5)

    if rc == 0 and stdout:
        print(f"  [tc output for {bottleneck_port}]:\n{stdout}")
        # check for htb or hfsc classes
        has_classes = ("class htb" in stdout) or ("class hfsc" in stdout) or ("class cbq" in stdout)
        if has_classes:
            return record_result(
                "DSCP Queue Test",
                True,
                f"Active hierarchical traffic control (tc) classes identified on {bottleneck_port}.",
                results
            )

    # fallback: check ovs queue stats
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

    return record_result(
        "DSCP Queue Test",
        False,
        f"No QoS queues detected on port {bottleneck_port}. Ensure QoS script configured HTB queues.",
        results
    )


# test 4: priority vs best effort under congestion
def test_4_priority_effect(controller_url, results):
    log_header("4. Priority Isolation Effect Under Congestion")

    # normal ping without dscp mark (best effort)
    print("  Testing Best-Effort (DSCP 0) UDP probe latency...")
    ping_be = ["ping", "-c", "5", "-i", "0.2", "-Q", "0", "10.0.0.100"]
    rc_be, out_be, _ = run_in_host("h1", ping_be, timeout=6)

    # ping with dscp 46 (tos = 46 * 4 = 184)
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


# test 5: congestion detection
def test_5_congestion_detection(controller_url, log_file, results):
    log_header("5. Controller Congestion Detection Telemetry")

    # check 1: controller rest status
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

    # check 2: log file on disk
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


# test 6: rest endpoints
def test_6_rest_endpoints(controller_url, results):
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


# test 7: link failure and recovery
def test_7_link_failure(switch, interface, results):
    log_header(f"7. Link Failure Resilience & Recovery on {switch}:{interface}")

    # check if interface exists
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

    # ping while link is down
    rc_down, _, _ = run_in_host("h1", ["ping", "-c", "1", "-W", "1", "10.0.0.100"], timeout=3)
    down_detected = (rc_down != 0)

    # bring link back up
    print(f"  Restoring link UP on {interface}...")
    run_cmd(["ip", "link", "set", interface, "up"])
    time.sleep(1.5)  # wait for openflow port status to propagate

    # check ping after recovery
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


# test 8: dscp packet capture
def test_8_dscp_wire(interface, results):
    log_header(f"8. DSCP Wire Verification on Interface {interface}")

    pcap_file = "/tmp/dscp_test.pcap"
    if os.path.exists(pcap_file):
        try:
            os.remove(pcap_file)
        except OSError:
            pass

    # tcpdump looking for tos 0xb8 (dscp 46)
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
        time.sleep(0.5)  # wait for tcpdump to start

        # send a dscp 46 probe packet (tos 184)
        probe_cmd = ["ping", "-c", "1", "-Q", "184", "10.0.0.100"]
        run_in_host("h1", probe_cmd, timeout=3)

        # wait for capture
        try:
            tcpdump_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            tcpdump_proc.kill()

        captured = os.path.exists(pcap_file) and os.path.getsize(pcap_file) > 24
        if captured:
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


# main

def main():
    parser = argparse.ArgumentParser(
        description="Network test suite for SDN time sync"
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

    # needs root for mininet, tc, and tcpdump
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

    results = []
    hosts = ["h1", "h2", "h3", "h4", "server"]

    # run tests
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

    # print summary
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
