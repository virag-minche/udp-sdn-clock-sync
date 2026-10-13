#!/usr/bin/env python3
"""
Runs a server + several clients at the same time and checks every client converges.
Put this in the same folder as timesync_core.py, then:  python test_multiclient.py
"""
import csv
import subprocess
import sys
import time

SCRIPT = "timesync_core.py"      # change to timesync_simple.py if you want to test that one
PORT = 9998                      # not 9999, so it won't clash with a server you left running
ROUNDS = 50
INTERVAL = 0.1
PASS_MS = 1.0                    # final true error must be below this (ms)

# (client id, initial skew in seconds) -> mix of positive, negative and big skews
CLIENTS = [(101, 0.35), (102, -0.35), (103, 0.80), (104, -1.20)]


def read_log(path):
    """Returns (list of true-error ms, list of rtt ms). Finds columns by name so it works with either script."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [], []
    err_key = next(k for k in rows[0] if "true" in k)
    rtt_key = next(k for k in rows[0] if "rtt" in k)
    return [float(r[err_key]) for r in rows], [float(r[rtt_key]) for r in rows]


def main():
    server = subprocess.Popen([sys.executable, SCRIPT, "server", "--port", str(PORT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # let the server bind

    procs = []
    for cid, skew in CLIENTS:
        cmd = [sys.executable, SCRIPT, "client", "--port", str(PORT),
               "--id", str(cid), "--skew", str(skew),
               "--rounds", str(ROUNDS), "--interval", str(INTERVAL),
               "--log", f"client_{cid}.csv"]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    print(f"Started {len(CLIENTS)} clients at once, waiting for them to finish...\n")

    for p in procs:
        p.wait()
    server.terminate()

    print(f"{'client':>6} {'skew(s)':>8} {'samples':>8} {'first err(ms)':>14} "
          f"{'final err(ms)':>14} {'avg rtt(ms)':>12}  result")
    all_ok = True
    for (cid, skew), p in zip(CLIENTS, procs):
        try:
            errs, rtts = read_log(f"client_{cid}.csv")
        except (FileNotFoundError, StopIteration):
            errs, rtts = [], []
        if not errs:
            print(f"{cid:>6} {skew:>+8.2f}  no data  (client crashed?)  "
                  f"{p.stderr.read().decode()[-200:]}")
            all_ok = False
            continue
        tail = errs[-10:]
        final = sum(abs(e) for e in tail) / len(tail)       # mean |error| over last 10 rounds
        ok = final < PASS_MS and len(errs) >= ROUNDS * 0.95  # converged AND <5% packets lost
        all_ok &= ok #if final avg error <5% mark as pass
        print(f"{cid:>6} {skew:>+8.2f} {len(errs):>8} {errs[0]:>14.2f} "
              f"{final:>14.3f} {sum(rtts)/len(rtts):>12.2f}  {'PASS' if ok else 'FAIL'}")

    print("\nALL PASSED" if all_ok else "\nSOME FAILED")

    # optional plot, only if matplotlib is installed
    try:
        import matplotlib.pyplot as plt
        for cid, skew in CLIENTS:
            errs, _ = read_log(f"client_{cid}.csv")
            plt.semilogy(range(len(errs)), [abs(e) for e in errs], label=f"client {cid} ({skew:+.2f}s)")
        plt.xlabel("sync round")
        plt.ylabel("|true clock error| (ms)")
        plt.title("Convergence of multiple clients")
        plt.grid(True, which="both", alpha=0.3)
        plt.legend()
        plt.savefig("convergence.png", dpi=150, bbox_inches="tight")
        print("Saved convergence.png")
    except ImportError:
        print("(pip install matplotlib to also get a convergence.png graph)")


if __name__ == "__main__":
    main()