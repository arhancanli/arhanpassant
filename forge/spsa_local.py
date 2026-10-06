"""SPSA tuning of the engine's search settings on one machine.

Each step draws a random +-1 direction for every parameter, plays a batch of
game pairs between theta + c*delta and theta - c*delta (same binary, settings
passed as UCI options) with the arena, and moves theta along the measured
gradient. Schedules follow the usual SPSA form (as in OpenBench):

    c_k = c_end * (N / k) ** 0.101      a_k = a_end * ((A + N) / (A + k)) ** 0.602
    a_end = r_end * c_end ** 2          A = 0.1 * N

so the perturbation and step size shrink to c_end and r_end * c_end**2 by the
last pair. State is written after every batch; a restart resumes it.

    python forge/spsa_local.py run --state spsa.json --params params.json \
        --engine BIN --arena ARENA --book BOOK --pairs 15000 --batch 32 --concurrency 16
"""

import argparse
import glob
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time

ALPHA, GAMMA = 0.602, 0.101


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def schedule(p, k, total):
    """(c_k, a_k) for parameter p at pair k (1-based) of `total`."""
    big_a = 0.1 * total
    c_k = p["c_end"] * (total / k) ** GAMMA
    a_end = p["r_end"] * p["c_end"] ** 2
    a_k = a_end * ((big_a + total) / (big_a + k)) ** ALPHA
    return c_k, a_k


def clamp_int(x, lo, hi):
    return int(max(lo, min(hi, round(x))))


def play_batch(args, plus, minus, pairs, seed):
    """Play `pairs` game pairs plus-vs-minus; return (wins, losses, draws) for plus."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.json")
        cmd = [args.arena,
               "--engine", "name=plus", f"cmd={args.engine}", "opt.Hash=16", "opt.Threads=1",
               *[f"opt.{k}={v}" for k, v in plus.items()], *args.fixed,
               "--engine", "name=minus", f"cmd={args.engine}", "opt.Hash=16", "opt.Threads=1",
               *[f"opt.{k}={v}" for k, v in minus.items()], *args.fixed,
               "--tc", args.tc, "--book", args.book, "--concurrency", str(args.concurrency),
               "--games", str(2 * pairs), "--seed", str(seed), "--out", out, "--quiet"]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out) as f:
            r = json.load(f)
    return r["wins"], r["losses"], r["draws"]


def run(args):
    spec = load(args.params, None)
    if spec is None:
        sys.exit(f"no parameter file {args.params}")
    state = load(args.state, None)
    if state is None:
        state = {"pairs_done": 0, "total_pairs": args.pairs, "theta": {p["name"]: float(p["start"]) for p in spec},
                 "history": [], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save(args.state, state)
    total = state["total_pairs"]
    rng = random.Random(args.seed + state["pairs_done"])
    while state["pairs_done"] < total:
        # Lowest priority: wait while test jobs are queued or a test match is running.
        while args.yield_queue and (glob.glob(os.path.join(args.yield_queue, "*.sh")) or subprocess.run(
                ["pgrep", "-f", "^" + args.arena + " .*/elo/"], capture_output=True).returncode == 0):
            time.sleep(30)
        k = state["pairs_done"] + 1
        n = min(args.batch, total - state["pairs_done"])
        plus, minus, deltas, steps = {}, {}, {}, {}
        for p in spec:
            c_k, a_k = schedule(p, k, total)
            d = rng.choice((-1, 1))
            th = state["theta"][p["name"]]
            plus[p["name"]] = clamp_int(th + c_k * d, p["min"], p["max"])
            minus[p["name"]] = clamp_int(th - c_k * d, p["min"], p["max"])
            deltas[p["name"]], steps[p["name"]] = d, (c_k, a_k)
        w, l, dr = play_batch(args, plus, minus, n, rng.randrange(1 << 30))
        result = w - l
        for p in spec:
            c_k, a_k = steps[p["name"]]
            th = state["theta"][p["name"]] + a_k * result / (c_k * deltas[p["name"]])
            state["theta"][p["name"]] = max(p["min"], min(p["max"], th))
        state["pairs_done"] += n
        state["history"].append({"pairs": state["pairs_done"], "w": w, "l": l, "d": dr,
                                 "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        save(args.state, state)
        print(f"{state['pairs_done']}/{total} +{w} -{l} ={dr} " +
              " ".join(f"{p['name']}={state['theta'][p['name']]:.1f}" for p in spec), flush=True)
    print("done", flush=True)


def show(args):
    spec = load(args.params, [])
    state = load(args.state, None)
    if state is None:
        sys.exit("no state")
    print(f"{state['pairs_done']}/{state['total_pairs']} pairs")
    for p in spec:
        th = state["theta"][p["name"]]
        print(f"{p['name']:>16} start {p['start']:>7} now {th:9.2f} ({clamp_int(th, p['min'], p['max'])})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "show"])
    ap.add_argument("--state", required=True)
    ap.add_argument("--params", required=True, help="JSON list of {name,start,min,max,c_end,r_end}")
    ap.add_argument("--engine")
    ap.add_argument("--arena")
    ap.add_argument("--book")
    ap.add_argument("--tc", default="5+0.05")
    ap.add_argument("--pairs", type=int, default=15000)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fixed", nargs="*", default=[], help="extra opt.X=Y for both sides")
    ap.add_argument("--yield-queue", help="pause while this test queue has jobs or a test match runs")
    args = ap.parse_args()
    if args.cmd == "run":
        if not (args.engine and args.arena and args.book):
            ap.error("run needs --engine, --arena and --book")
        run(args)
    else:
        show(args)


if __name__ == "__main__":
    main()
