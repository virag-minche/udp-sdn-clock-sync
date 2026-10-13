#!/usr/bin/env python3
"""
Experiment Data Analysis & Visualization (Role 3 Integration)
=============================================================
Reads client synchronization CSV logs produced by timesync_core.py, computes
summary statistics (Average, Minimum, Maximum, Standard Deviation) for:
  - rtt_ms        (Round-Trip Time in ms)
  - jitter_ms     (Inter-packet RTT variation in ms)
  - sync_error_ms (Post-correction clock synchronization error |θ| in ms)

Supports both:
  1. Single CSV analysis (via --csv <path> or interactive prompt)
  2. Batch directory analysis (via --results-dir results/, default when CSVs exist)

Generates plots in results/:
  - results/rtt_ms.png
  - results/jitter_ms.png
  - results/sync_error_ms.png
  - results/experiment_summary.csv
"""

import argparse
import csv
import glob
import os
import statistics
import sys

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


METRICS = ["rtt_ms", "jitter_ms", "sync_error_ms"]

METRIC_LABELS = {
    "rtt_ms": "Round-Trip Time (RTT)",
    "jitter_ms": "Packet Delay Jitter",
    "sync_error_ms": "Clock Synchronization Error |θ|",
}


def extract_metric_values(rows, metric):
    """Extract float values for a metric column, with fallback computation if needed."""
    values = []
    prev_rtt = None

    for row in rows:
        try:
            if metric in row and row[metric] not in (None, ""):
                values.append(float(row[metric]))
            elif metric == "sync_error_ms":
                # Fallback to true_error_after_ms or true_error_ms
                for fallback in ("true_error_after_ms", "true_error_ms"):
                    if fallback in row and row[fallback] not in (None, ""):
                        values.append(abs(float(row[fallback])))
                        break
            elif metric == "jitter_ms" and "rtt_ms" in row:
                curr_rtt = float(row["rtt_ms"])
                if prev_rtt is not None:
                    values.append(abs(curr_rtt - prev_rtt))
                else:
                    values.append(0.0)
                prev_rtt = curr_rtt
        except (KeyError, ValueError, TypeError):
            continue

    return values


def analyze_single_csv(filename, out_dir="results"):
    """Analyze a single CSV file (Role 3 original workflow enhanced)."""
    if not os.path.exists(filename):
        print(f"Error: File not found: {filename}")
        return None

    with open(filename, "r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    if not rows:
        print(f"No experiment data found in {filename}.")
        return None

    os.makedirs(out_dir, exist_ok=True)
    summary = {"file": os.path.basename(filename), "samples": len(rows)}

    print(f"\n============================================================")
    print(f" Analyzing: {filename} ({len(rows)} samples)")
    print(f"============================================================")

    for metric in METRICS:
        values = extract_metric_values(rows, metric)

        if not values:
            print(f"Missing data for {metric}")
            continue

        avg_val = statistics.mean(values)
        min_val = min(values)
        max_val = max(values)
        std_val = statistics.pstdev(values) if len(values) > 1 else 0.0

        summary[f"{metric}_avg"] = avg_val
        summary[f"{metric}_min"] = min_val
        summary[f"{metric}_max"] = max_val
        summary[f"{metric}_std"] = std_val

        print(f"\n{metric} ({METRIC_LABELS.get(metric, metric)})")
        print(f"  Average       : {avg_val:.4f} ms")
        print(f"  Minimum       : {min_val:.4f} ms")
        print(f"  Maximum       : {max_val:.4f} ms")
        print(f"  Std deviation : {std_val:.4f} ms")

        if HAS_MATPLOTLIB:
            plt.figure(figsize=(8, 4.5))
            plt.plot(range(len(values)), values, marker="o", markersize=3.5, linewidth=1.5)
            plt.title(f"{METRIC_LABELS.get(metric, metric)} ({os.path.basename(filename)})")
            plt.xlabel("Synchronization Round")
            plt.ylabel("Milliseconds (ms)")
            plt.grid(True, linestyle=":", alpha=0.6)
            plt.tight_layout()
            out_path = os.path.join(out_dir, f"{metric}.png")
            plt.savefig(out_path, dpi=150)
            plt.close()

    return summary


def analyze_batch_csvs(csv_files, out_dir="results"):
    """Analyze multiple client CSV files and produce overlay plots + summary table."""
    os.makedirs(out_dir, exist_ok=True)
    all_summaries = []
    series_by_metric = {m: {} for m in METRICS}

    for path in sorted(csv_files):
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue

        label = os.path.splitext(os.path.basename(path))[0]
        parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
        if parent not in ("results", "src", ".", ""):
            label = f"{parent}/{label}"

        summary = {"file": label, "samples": len(rows)}
        for metric in METRICS:
            vals = extract_metric_values(rows, metric)
            if vals:
                series_by_metric[metric][label] = vals
                summary[f"{metric}_avg"] = statistics.mean(vals)
                summary[f"{metric}_min"] = min(vals)
                summary[f"{metric}_max"] = max(vals)
                summary[f"{metric}_std"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        all_summaries.append(summary)

    if not all_summaries:
        print("No valid client CSV data found.")
        return

    print("\n" + "=" * 92)
    print(f"{'Dataset / Client':<24} | {'RTT Avg (ms)':>12} | {'RTT Std':>9} | {'Jitter Avg':>10} | {'Sync Err Avg':>12} | {'Final Err':>10}")
    print("-" * 92)
    for s in all_summaries:
        label = s["file"]
        vals_err = series_by_metric["sync_error_ms"].get(label, [])
        tail_err = statistics.mean(vals_err[-10:]) if vals_err else 0.0
        print(
            f"{label:<24} | "
            f"{s.get('rtt_ms_avg', 0.0):>12.3f} | "
            f"{s.get('rtt_ms_std', 0.0):>9.3f} | "
            f"{s.get('jitter_ms_avg', 0.0):>10.3f} | "
            f"{s.get('sync_error_ms_avg', 0.0):>12.3f} | "
            f"{tail_err:>10.3f}"
        )
    print("=" * 92)

    # Save summary CSV
    summary_csv = os.path.join(out_dir, "experiment_summary.csv")
    fieldnames = [
        "file", "samples",
        "rtt_ms_avg", "rtt_ms_min", "rtt_ms_max", "rtt_ms_std",
        "jitter_ms_avg", "jitter_ms_min", "jitter_ms_max", "jitter_ms_std",
        "sync_error_ms_avg", "sync_error_ms_min", "sync_error_ms_max", "sync_error_ms_std"
    ]
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_summaries)
    print(f"[SAVED] Summary statistics written to: {summary_csv}")

    if HAS_MATPLOTLIB:
        for metric in METRICS:
            if not series_by_metric[metric]:
                continue
            plt.figure(figsize=(8.5, 4.8))
            for label, vals in series_by_metric[metric].items():
                plt.plot(range(len(vals)), vals, marker="o", markersize=3, linewidth=1.5, label=label)
            plt.title(f"{METRIC_LABELS.get(metric, metric)} Across Clients")
            plt.xlabel("Synchronization Round")
            plt.ylabel("Milliseconds (ms)")
            plt.grid(True, linestyle=":", alpha=0.6)
            plt.legend(loc="best", fontsize=8)
            plt.tight_layout()
            out_path = os.path.join(out_dir, f"{metric}.png")
            plt.savefig(out_path, dpi=150)
            plt.close()
            print(f"[SAVED] Plot written to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Analyze Time Sync CSV logs (Role 3)")
    parser.add_argument("--csv", default=None, help="Path to a single client CSV file")
    parser.add_argument("--results-dir", default="results", help="Directory containing client_*.csv files")
    args = parser.parse_args()

    if args.csv:
        analyze_single_csv(args.csv, out_dir=args.results_dir)
        print("\nAnalysis completed.")
        return

    # Discover client_*.csv files in results_dir or current directory
    pattern1 = glob.glob(os.path.join(args.results_dir, "**", "client_*.csv"), recursive=True)
    pattern2 = glob.glob(os.path.join(args.results_dir, "client_*.csv"))
    pattern3 = glob.glob("client_*.csv")
    discovered = sorted(set(pattern1 + pattern2 + pattern3))

    if discovered:
        analyze_batch_csvs(discovered, out_dir=args.results_dir)
        print("\nAnalysis completed.")
    else:
        # Fallback to interactive input prompt (Role 3 original behavior)
        try:
            filename = input("Enter CSV file path: ").strip()
        except EOFError:
            print("No CSV file specified and none found in results/.")
            return
        if filename:
            analyze_single_csv(filename, out_dir=args.results_dir)
            print("\nAnalysis completed.")


if __name__ == "__main__":
    main()
