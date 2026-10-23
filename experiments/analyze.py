#!/usr/bin/env python3
"""
experiments/analyze.py
================================================================================
Unified Post-Experiment Analysis & Visualization
SDN-Assisted Network Time Synchronization System (Computer Networks Topic 23)
================================================================================
Reads synchronization client CSV logs, computes summary statistics (RTT mean,
std, jitter, clock sync error, and convergence time), and produces:
  1. results/experiment_summary.csv   - Full statistical summary table
  2. results/comparison_chart.png      - Dual-axis bar chart (RTT vs Clock Error)
  3. results/convergence_plot.png      - Convergence curves with 1.0 ms threshold

Usage:
  # Batch analysis of all CSVs in results/ (default):
  python3 experiments/analyze.py --results-dir results/

  # Single CSV analysis:
  python3 experiments/analyze.py --csv results/client_101.csv
"""

import argparse
import csv
import glob
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional

try:
    import matplotlib
    matplotlib.use("Agg")  # Headless backend safe for Linux servers, WSL2, Windows
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ==============================================================================
# Statistical Helper Functions
# ==============================================================================

def compute_mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_std(values: List[float], mean_val: Optional[float] = None) -> float:
    if len(values) < 2:
        return 0.0
    m = mean_val if mean_val is not None else compute_mean(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)


def compute_jitter(rtts: List[float]) -> float:
    if len(rtts) < 2:
        return 0.0
    diffs = [abs(rtts[i + 1] - rtts[i]) for i in range(len(rtts) - 1)]
    return compute_mean(diffs)


def find_convergence_round(errors: List[float], threshold_ms: float = 1.0) -> Optional[int]:
    for idx, err in enumerate(errors):
        if abs(err) < threshold_ms:
            return idx + 1
    return None


# ==============================================================================
# Data Extraction & Normalization
# ==============================================================================

def normalize_field(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def parse_client_csv(filepath: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(filepath):
        return None

    rounds: List[int] = []
    rtts: List[float] = []
    errors: List[float] = []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if not headers:
            return None

        col_map = {normalize_field(h): i for i, h in enumerate(headers)}

        rtt_idx = (
            col_map.get("rtt_ms") or
            col_map.get("rtt") or
            col_map.get("round_trip_time_ms")
        )
        err_idx = (
            col_map.get("sync_error_ms") or
            col_map.get("true_error_after_ms") or
            col_map.get("true_error_ms") or
            col_map.get("true_error_before_ms") or
            col_map.get("offset_ms")
        )
        round_idx = (
            col_map.get("seq") or
            col_map.get("round") or
            col_map.get("step")
        )

        for row_num, row in enumerate(reader, start=1):
            if not row:
                continue
            try:
                r_val = int(float(row[round_idx])) if round_idx is not None and round_idx < len(row) else row_num
                rtt_val = float(row[rtt_idx]) if rtt_idx is not None and rtt_idx < len(row) else None
                err_val = float(row[err_idx]) if err_idx is not None and err_idx < len(row) else 0.0

                if rtt_val is not None:
                    rounds.append(r_val)
                    rtts.append(rtt_val)
                    errors.append(abs(err_val))
            except (ValueError, IndexError):
                continue

    if not rtts:
        return None

    label = os.path.splitext(os.path.basename(filepath))[0]
    parent = os.path.basename(os.path.dirname(os.path.abspath(filepath)))
    if parent not in ("results", "src", ".", ""):
        label = f"{parent}/{label}"

    mean_rtt = compute_mean(rtts)
    std_rtt = compute_std(rtts, mean_rtt)
    mean_err = compute_mean(errors)
    std_err = compute_std(errors, mean_err)
    jitter = compute_jitter(rtts)
    conv_round = find_convergence_round(errors, threshold_ms=1.0)
    tail_err = compute_mean(errors[-10:]) if len(errors) >= 10 else compute_mean(errors)

    return {
        "file": label,
        "path": filepath,
        "count": len(rtts),
        "rounds": rounds,
        "rtts": rtts,
        "errors": errors,
        "rtt_mean": mean_rtt,
        "rtt_std": std_rtt,
        "rtt_min": min(rtts),
        "rtt_max": max(rtts),
        "jitter": jitter,
        "err_mean": mean_err,
        "err_std": std_err,
        "err_final": tail_err,
        "conv_round": conv_round,
    }


# ==============================================================================
# Visualization Generators
# ==============================================================================

def generate_comparison_chart(datasets: List[Dict[str, Any]], out_path: str):
    if not HAS_MATPLOTLIB or not datasets:
        return

    labels = [d["file"] for d in datasets]
    rtt_means = [d["rtt_mean"] for d in datasets]
    rtt_stds = [d["rtt_std"] for d in datasets]
    err_means = [d["err_mean"] for d in datasets]
    err_stds = [d["err_std"] for d in datasets]

    x = range(len(labels))
    width = 0.35

    fig, ax1 = plt.subplots(figsize=(10, 5.5))
    ax2 = ax1.twinx()

    bar1 = ax1.bar(
        [i - width / 2 for i in x],
        rtt_means,
        width,
        yerr=rtt_stds,
        capsize=4,
        label="RTT Mean (ms)",
        color="#345995",
        edgecolor="#1D2A44",
        alpha=0.9
    )

    bar2 = ax2.bar(
        [i + width / 2 for i in x],
        err_means,
        width,
        yerr=err_stds,
        capsize=4,
        label="Sync Error |θ| Mean (ms)",
        color="#E06D53",
        edgecolor="#7A2E1D",
        alpha=0.9
    )

    ax1.set_xlabel("Experiment Condition / Client", fontsize=11, fontweight="bold")
    ax1.set_ylabel("Round-Trip Time (ms)", color="#345995", fontsize=11, fontweight="bold")
    ax2.set_ylabel("Clock Sync Error |θ| (ms)", color="#E06D53", fontsize=11, fontweight="bold")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(labels, rotation=15 if len(labels) > 4 else 0, ha="right" if len(labels) > 4 else "center")

    ax1.grid(True, linestyle=":", alpha=0.5, axis="y")
    plt.title("Time Synchronization Performance: RTT & Clock Error", fontsize=12, fontweight="bold")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")

    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()
    print(f"[SAVED] Performance comparison chart: {out_path}")


def generate_convergence_plot(datasets: List[Dict[str, Any]], out_path: str):
    if not HAS_MATPLOTLIB or not datasets:
        return

    plt.figure(figsize=(9, 5))
    for d in datasets:
        errs = d["errors"]
        rounds = d["rounds"]
        plt.semilogy(rounds, errs, marker="o", markersize=2.5, linewidth=1.5, label=d["file"])

    plt.axhline(
        y=1.0,
        color="#D90429",
        linestyle="--",
        linewidth=1.3,
        label="Pass Threshold (1.0 ms)"
    )

    plt.xlabel("Synchronization Round (Sequence)", fontsize=11, fontweight="bold")
    plt.ylabel("Absolute Clock Error |θ| (ms, Log Scale)", fontsize=11, fontweight="bold")
    plt.title("Clock Synchronization Error Convergence Over Rounds", fontsize=12, fontweight="bold")
    plt.grid(True, which="both", linestyle=":", alpha=0.5)
    plt.legend(loc="upper right", fontsize=8.5)
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()
    print(f"[SAVED] Convergence plot: {out_path}")


# ==============================================================================
# Main Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Analyze Time Sync Experiment Results")
    parser.add_argument("--csv", default=None, help="Path to single client CSV file")
    parser.add_argument("--results-dir", default="results", help="Directory containing client_*.csv files")
    args = parser.parse_args()

    out_dir = args.results_dir
    os.makedirs(out_dir, exist_ok=True)

    if args.csv:
        csv_files = [args.csv]
    else:
        pattern1 = glob.glob(os.path.join(args.results_dir, "**", "client_*.csv"), recursive=True)
        pattern2 = glob.glob(os.path.join(args.results_dir, "client_*.csv"))
        pattern3 = glob.glob("client_*.csv")
        csv_files = sorted(set(pattern1 + pattern2 + pattern3))

    if not csv_files:
        print(f"No client CSV files found in '{args.results_dir}'.")
        return

    datasets: List[Dict[str, Any]] = []
    for cp in csv_files:
        parsed = parse_client_csv(cp)
        if parsed:
            datasets.append(parsed)

    if not datasets:
        print("No valid synchronization telemetry could be extracted.")
        return

    print("\n" + "=" * 98)
    print("                    TIME SYNCHRONIZATION EXPERIMENT SUMMARY TABLE")
    print("=" * 98)
    print(f"{'Condition / Client':<22} | {'RTT Mean':>10} | {'RTT Std':>8} | {'Jitter':>8} | {'Error Mean':>11} | {'Final Err':>9} | {'Conv Round':>10}")
    print("-" * 98)
    for d in datasets:
        conv_str = str(d["conv_round"]) if d["conv_round"] is not None else "N/A"
        print(
            f"{d['file']:<22} | "
            f"{d['rtt_mean']:>8.3f} ms | "
            f"{d['rtt_std']:>8.3f} | "
            f"{d['jitter']:>6.3f} ms | "
            f"{d['err_mean']:>9.3f} ms | "
            f"{d['err_final']:>7.3f} ms | "
            f"{conv_str:>10}"
        )
    print("=" * 98 + "\n")

    # Save summary CSV
    summary_path = os.path.join(out_dir, "experiment_summary.csv")
    fieldnames = [
        "file", "count", "rtt_mean", "rtt_std", "rtt_min", "rtt_max",
        "jitter", "err_mean", "err_std", "err_final", "conv_round"
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(datasets)
    print(f"[SAVED] Statistical summary CSV: {summary_path}")

    # Generate charts
    chart_path = os.path.join(out_dir, "comparison_chart.png")
    generate_comparison_chart(datasets, chart_path)

    conv_path = os.path.join(out_dir, "convergence_plot.png")
    generate_convergence_plot(datasets, conv_path)


if __name__ == "__main__":
    main()
