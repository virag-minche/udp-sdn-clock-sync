
import csv
import os
import statistics
import matplotlib.pyplot as plt

def main():
    filename = input("Enter CSV file path: ")

    with open(filename, "r", newline="") as file:
        rows = list(csv.DictReader(file))

    if not rows:
        print("No experiment data found.")
        return

    metrics = ["rtt_ms", "jitter_ms", "sync_error_ms"]
    os.makedirs("results", exist_ok=True)

    for metric in metrics:
        values = []

        for row in rows:
            try:
                values.append(float(row[metric]))
            except (KeyError, ValueError, TypeError):
                continue

        if not values:
            print(f"Missing data for {metric}")
            continue

        print(f"\n{metric}")
        print(f"Average: {statistics.mean(values):.4f}")
        print(f"Minimum: {min(values):.4f}")
        print(f"Maximum: {max(values):.4f}")
        print(f"Std deviation: {statistics.pstdev(values):.4f}")

        plt.figure()
        plt.plot(values, marker="o")
        plt.title(metric)
        plt.xlabel("Measurement")
        plt.ylabel("Milliseconds")
        plt.grid(True)
        plt.savefig(f"results/{metric}.png")
        plt.close()

    print("\nAnalysis completed.")

if __name__ == "__main__":
    main()
      
