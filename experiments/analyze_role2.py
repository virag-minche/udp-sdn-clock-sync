#!/usr/bin/env python3
"""
experiments/analyze_role2.py
================================================================================
Post-Experiment Analysis & Visualization for SDN Time Synchronization System
--------------------------------------------------------------------------------
Targets: Ubuntu 22.04 LTS (Python 3.8-3.10), matplotlib

Theoretical Background & Network Engineering Principles:
--------------------------------------------------------------------------------
1. Why Asymmetric Congestion Corrupts Clock Offset Estimation:
   In two-way time transfer protocols (NTP, PTP IEEE 1588):
       Clock Offset Theta = [(T2 - T1) - (T4 - T3)] / 2
       Round Trip Time (RTT) = (T4 - T1) - (T3 - T2)
   This mathematical formulation relies on the fundamental assumption that the
   forward transit delay (d_f = T2 - T1) equals the backward transit delay (d_b = T4 - T3).
   When asymmetric background traffic (e.g., cross-traffic burst in forward direction)
   induces buffer queueing delay (Q_f >> Q_b):
       Actual Offset Error = (Q_f - Q_b) / 2
   Thus, unmitigated bufferbloat translates directly into clock synchronization error.

2. Why DSCP Priority Queueing Reduces RTT Variance (Not Baseline RTT):
   Without congestion, packet propagation delay and transmission delay are identical
   regardless of queue assignment. Therefore, priority marking does NOT reduce the
   minimum baseline RTT in an idle network.
   However, during cross-traffic bursts, priority queueing allows DSCP-46 packets
   to bypass congested Best-Effort FIFO queues (Queue 0), avoiding queueing delays
   (Q -> 0). This drastically shrinks RTT variance, eliminates jitter, and protects
   the symmetry assumption of the synchronization algorithm.

3. What to Look For in the Results:
   - Condition E3 (No Priority / Best Effort): High RTT spikes, severe jitter,
     divergent or lagging clock offset error under cross-traffic.
   - Condition E4 (Static Priority): Stable low RTT, near-zero jitter, rapid error
     convergence, but statically reserves bandwidth.
   - Condition E5 (Dynamic SDN): Controller monitors congestion telemetry; dynamically
     installs QoS flow rules only when congestion is detected, providing high
     accuracy while minimizing control-plane overhead.

Usage:
    python3 experiments/analyze_role2.py --results-dir results/
================================================================================
"""

import os
import sys
import csv
import math
import glob
import argparse
from typing import Dict, List, Any, Optional, Tuple

# Gracefully import matplotlib
try:
    import matplotlib
    # Use non-interactive Agg backend suitable for headless Linux servers / WSL
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ==============================================================================
# Statistical Helper Functions
# ==============================================================================

def compute_mean(values: List[float]) -> float:
    """Compute arithmetic mean."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def compute_std(values: List[float], mean_val: Optional[float] = None) -> float:
    """Compute population or sample standard deviation."""
    if len(values) < 2:
        return 0.0
    m = mean_val if mean_val is not None else compute_mean(values)
    variance = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def compute_jitter(rtts: List[float]) -> float:
    """
    Compute delay jitter as the sample standard deviation of differences
    between consecutive round-trip times: std(RTT_{i+1} - RTT_i).
    """
    if len(rtts) < 2:
        return 0.0
    diffs = [rtts[i + 1] - rtts[i] for i in range(len(rtts) - 1)]
    return compute_std(diffs)


def find_convergence_round(errors: List[float], threshold_ms: float = 1.0) -> Optional[int]:
    """
    Find the earliest round (1-indexed) where synchronization error
    permanently or first drops below threshold_ms (default: 1.0 ms).
    """
    for idx, err in enumerate(errors):
        if abs(err) < threshold_ms:
            return idx + 1
    return None


# ==============================================================================
# CSV Loading & Data Extraction
# ==============================================================================

def normalize_field_name(name: str) -> str:
    """Normalize CSV column names (lowercase, stripped, underscored)."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def parse_client_csv(filepath: str) -> Dict[str, Any]:
    """
    Parses a synchronization client CSV file.
    Extracts rounds, RTTs, offsets, and true_error_after_ms.
    Robust against minor variations in column header naming.
    """
    rounds: List[int] = []
    rtts: List[float] = []
    true_errors: List[float] = []
    timestamps: List[float] = []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if not headers:
            return {}

        col_map = {normalize_field_name(h): i for i, h in enumerate(headers)}

        # Identify key column indices
        rtt_idx = (
            col_map.get("rtt_ms") or
            col_map.get("rtt") or
            col_map.get("round_trip_time_ms")
        )
        err_idx = (
            col_map.get("true_error_after_ms") or
            col_map.get("true_error_ms") or
            col_map.get("error_after_ms") or
            col_map.get("sync_error_ms") or
            col_map.get("offset_error_ms") or
            col_map.get("error_ms")
        )
        round_idx = (
            col_map.get("round") or
            col_map.get("iteration") or
            col_map.get("seq") or
            col_map.get("step")
        )
        ts_idx = (
            col_map.get("timestamp") or
            col_map.get("t_send") or
            col_map.get("t1")
        )

        for row_num, row in enumerate(reader, start=1):
            if not row:
                continue
            try:
                # Extract round
                r_val = int(float(row[round_idx])) if round_idx is not None and round_idx < len(row) else row_num
                # Extract RTT
                rtt_val = float(row[rtt_idx]) if rtt_idx is not None and rtt_idx < len(row) else None
                # Extract sync error
                err_val = float(row[err_idx]) if err_idx is not None and err_idx < len(row) else None
                # Extract timestamp
                ts_val = float(row[ts_idx]) if ts_idx is not None and ts_idx < len(row) else 0.0

                if rtt_val is not None:
                    rounds.append(r_val)
                    rtts.append(rtt_val)
                    timestamps.append(ts_val)
                    # If error is missing, fall back to 0.0
                    true_errors.append(err_val if err_val is not None else 0.0)

            except (ValueError, IndexError):
                continue

    if not rtts:
        return {}

    mean_rtt = compute_mean(rtts)
    std_rtt = compute_std(rtts, mean_rtt)
    mean_err = compute_mean(true_errors)
    std_err = compute_std(true_errors, mean_err)
    jitter = compute_jitter(rtts)
    conv_round = find_convergence_round(true_errors, threshold_ms=1.0)

    return {
        "file": filepath,
        "count": len(rtts),
        "rounds": rounds,
        "rtts": rtts,
        "true_errors": true_errors,
        "timestamps": timestamps,
        "rtt_min": min(rtts),
        "rtt_max": max(rtts),
        "rtt_mean": mean_rtt,
        "rtt_std": std_rtt,
        "jitter": jitter,
        "err_min": min(true_errors) if true_errors else 0.0,
        "err_max": max(true_errors) if true_errors else 0.0,
        "err_mean": mean_err,
        "err_std": std_err,
        "conv_round": conv_round
    }


def parse_controller_log(log_path: str) -> List[Dict[str, Any]]:
    """Reads controller_log.csv if present."""
    records = []
    if not os.path.exists(log_path):
        return records

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
    except Exception as exc:
        print(f"[WARN] Could not parse controller log {log_path}: {exc}")
    return records


# ==============================================================================
# Experiment Grouping
# ==============================================================================

def identify_condition(name_or_path: str) -> str:
    """
    Classify a file or directory into standard benchmark conditions:
      E3: No Priority (Best Effort)
      E4: Static Priority (DSCP-46 static flow/queue)
      E5: Dynamic SDN (Controller-managed dynamic queue/reroute)
    """
    lower = name_or_path.lower()
    if "e3" in lower or "no_priority" in lower or "nopriority" in lower or "best_effort" in lower:
        return "E3-no-priority"
    elif "e4" in lower or "static_priority" in lower or "static" in lower:
        return "E4-static-priority"
    elif "e5" in lower or "dynamic_sdn" in lower or "dynamic" in lower or "sdn" in lower:
        return "E5-dynamic-SDN"
    return os.path.basename(name_or_path).replace(".csv", "")


def discover_and_aggregate_results(results_dir: str) -> Dict[str, Dict[str, Any]]:
    """
    Scan the results directory for client CSV files and categorize them by condition.
    Aggregates statistics for each condition.
    """
    aggregated: Dict[str, Dict[str, Any]] = {}
    csv_files = []

    # Search top-level and subdirectories of results_dir
    for root, _, files in os.walk(results_dir):
        for f in files:
            if f.endswith(".csv") and f != "controller_log.csv":
                csv_files.append(os.path.join(root, f))

    if not csv_files:
        print(f"[INFO] No client CSV files found in {results_dir}.")
        return {}

    for path in sorted(csv_files):
        parsed = parse_client_csv(path)
        if not parsed:
            continue

        cond = identify_condition(path)
        # Store the best or most detailed run for each condition
        if cond not in aggregated:
            aggregated[cond] = parsed
        else:
            # If multiple files exist for condition, pick the one with most samples
            if parsed["count"] > aggregated[cond]["count"]:
                aggregated[cond] = parsed

    return aggregated


# ==============================================================================
# Visualization Routines
# ==============================================================================

def generate_comparison_chart(data: Dict[str, Dict[str, Any]], output_path: str) -> None:
    """
    Generates comparison bar chart:
      X-axis: Experiment conditions (E3-no-priority, E4-static-priority, E5-dynamic-SDN)
      Y-axis (Left): Mean RTT (ms) with error bars (std dev)
      Y-axis (Right): Mean Sync Error (ms) with error bars (std dev)
    """
    if not HAS_MATPLOTLIB:
        print("[WARN] matplotlib is not installed. Skipping plot generation.")
        return

    conditions = ["E3-no-priority", "E4-static-priority", "E5-dynamic-SDN"]
    # Filter to conditions present in dataset, or keep ordered list
    present_conditions = [c for c in conditions if c in data]
    if not present_conditions:
        present_conditions = list(data.keys())

    if not present_conditions:
        return

    fig, ax1 = plt.subplots(figsize=(10, 6))

    x_indices = range(len(present_conditions))
    width = 0.35

    # Extract metrics
    rtt_means = [data[c]["rtt_mean"] for c in present_conditions]
    rtt_stds = [data[c]["rtt_std"] for c in present_conditions]
    err_means = [abs(data[c]["err_mean"]) for c in present_conditions]
    err_stds = [data[c]["err_std"] for c in present_conditions]

    # Bar 1: RTT Mean & Std
    rects1 = ax1.bar(
        [x - width / 2 for x in x_indices],
        rtt_means,
        width,
        yerr=rtt_stds,
        capsize=5,
        label="Mean RTT (ms)",
        color="#2b5c8f",
        edgecolor="black",
        alpha=0.85
    )
    ax1.set_xlabel("Experiment Condition", fontweight="bold", fontsize=12)
    ax1.set_ylabel("Round-Trip Time (ms)", color="#2b5c8f", fontweight="bold", fontsize=12)
    ax1.tick_params(axis="y", labelcolor="#2b5c8f")
    ax1.set_xticks(x_indices)
    ax1.set_xticklabels(present_conditions, fontsize=11, fontweight="bold")
    ax1.grid(axis="y", linestyle="--", alpha=0.5)

    # Bar 2: Sync Error Mean & Std on secondary Y-axis
    ax2 = ax1.twinx()
    rects2 = ax2.bar(
        [x + width / 2 for x in x_indices],
        err_means,
        width,
        yerr=err_stds,
        capsize=5,
        label="Mean Sync Error (ms)",
        color="#d95f02",
        edgecolor="black",
        alpha=0.85
    )
    ax2.set_ylabel("Time Sync Error |Error| (ms)", color="#d95f02", fontweight="bold", fontsize=12)
    ax2.tick_params(axis="y", labelcolor="#d95f02")

    # Titles & Annotations
    plt.title("Network Time Synchronization Performance Under Asymmetric Congestion", fontsize=13, fontweight="bold", pad=15)

    # Value labels on bars
    for rect in rects1:
        height = rect.get_height()
        ax1.annotate(f"{height:.2f}ms",
                     xy=(rect.get_x() + rect.get_width() / 2, height),
                     xytext=(0, 3), textcoords="offset points",
                     ha="center", va="bottom", fontsize=9, fontweight="bold", color="#2b5c8f")

    for rect in rects2:
        height = rect.get_height()
        ax2.annotate(f"{height:.2f}ms",
                     xy=(rect.get_x() + rect.get_width() / 2, height),
                     xytext=(0, 3), textcoords="offset points",
                     ha="center", va="bottom", fontsize=9, fontweight="bold", color="#d95f02")

    plt.tight_layout()
    plt.savefig(output_path, dpi=300)
    plt.close()
    print(f"  [SAVED] Comparison chart written to: {output_path}")


def generate_convergence_plots(data: Dict[str, Dict[str, Any]], results_dir: str) -> None:
    """
    Generates convergence plots for each experiment and a combined comparison plot:
      Plots True Sync Error (ms) vs Synchronization Round.
    """
    if not HAS_MATPLOTLIB or not data:
        return

    # Combined convergence plot
    plt.figure(figsize=(10, 5))
    colors = {
        "E3-no-priority": "#d95f02",
        "E4-static-priority": "#7570b3",
        "E5-dynamic-SDN": "#1b9e77"
    }

    for cond, d in data.items():
        rounds = d["rounds"]
        errors = [abs(e) for e in d["true_errors"]]
        col = colors.get(cond, "#333333")
        plt.plot(rounds, errors, marker="o", markersize=4, label=cond, color=col, linewidth=1.8)

    # Add 1.0 ms threshold reference line
    plt.axhline(y=1.0, color="red", linestyle="--", alpha=0.7, label="1.0 ms Target Threshold")

    plt.xlabel("Synchronization Round", fontweight="bold", fontsize=11)
    plt.ylabel("|Clock Offset Error| (ms)", fontweight="bold", fontsize=11)
    plt.title("Synchronization Error Convergence Over Rounds", fontweight="bold", fontsize=13)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend(loc="upper right", frameon=True)
    plt.tight_layout()

    comb_path = os.path.join(results_dir, "convergence_comparison.png")
    plt.savefig(comb_path, dpi=300)
    plt.close()
    print(f"  [SAVED] Combined convergence plot written to: {comb_path}")


# ==============================================================================
# Summary Table Reporting
# ==============================================================================

def print_summary_table(data: Dict[str, Dict[str, Any]]) -> None:
    """Print student-readable formatted markdown/text summary table."""
    print("\n" + "=" * 96)
    print(f"{'EXPERIMENT PERFORMANCE SUMMARY TABLE':^96}")
    print("=" * 96)
    header = f"{'Condition':<20} | {'RTT Mean (ms)':<14} | {'RTT Std':<10} | {'Jitter (ms)':<12} | {'Err Mean (ms)':<14} | {'Conv. Round':<12}"
    print(header)
    print("-" * 96)

    for cond, metrics in data.items():
        conv_str = str(metrics["conv_round"]) if metrics["conv_round"] is not None else "N/A (>max)"
        row = (
            f"{cond:<20} | "
            f"{metrics['rtt_mean']:>10.3f} ms | "
            f"{metrics['rtt_std']:>7.3f} ms | "
            f"{metrics['jitter']:>9.3f} ms | "
            f"{metrics['err_mean']:>10.3f} ms | "
            f"{conv_str:>11}"
        )
        print(row)
    print("=" * 96 + "\n")


# ==============================================================================
# Main Execution Flow
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="SDN Network Time Synchronization Experiment Analysis and Plotting"
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Path to results directory containing CSV files (default: results)"
    )
    args = parser.parse_args()

    results_dir = os.path.abspath(args.results_dir)
    os.makedirs(results_dir, exist_ok=True)

    print(f"\n{'*' * 75}")
    print(f"  ANALYZING TIME SYNCHRONIZATION DATA (Role 2)")
    print(f"  Directory: {results_dir}")
    print(f"{'*' * 75}\n")

    # Read controller logs if present
    ctrl_log_path = os.path.join(results_dir, "controller_log.csv")
    ctrl_records = parse_controller_log(ctrl_log_path)
    if ctrl_records:
        print(f"  [LOADED] Read {len(ctrl_records)} records from controller_log.csv.")

    # Aggregate client CSV files
    data = discover_and_aggregate_results(results_dir)

    if not data:
        print("[WARN] No experimental client CSV files found.")
        print("Expected filenames like 'results/E3_client.csv', 'results/E4_client.csv', etc.")
        print("Exiting analysis.")
        sys.exit(0)

    # Print Formatted Table
    print_summary_table(data)

    # Generate Plots
    chart_path = os.path.join(results_dir, "comparison_chart.png")
    generate_comparison_chart(data, chart_path)
    generate_convergence_plots(data, results_dir)

    print("Analysis complete.\n")


if __name__ == "__main__":
    main()
