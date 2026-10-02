"""Tune the engine's search parameters with SPSA on the fleet.

    python forge/spsa.py run --pairs 30000 --share 0.6     # starts a run, or resumes the active one
    python forge/spsa.py show                              # current values of the active run
    python forge/spsa.py stop

Fleet nodes that are not playing a gate play game pairs between two engines
whose parameters are nudged in opposite random directions (forge/azure/
spsa_batch.py); this process turns each pair's result into a step along those
directions. Step sizes follow the schedule fishtest and OpenBench use
(alpha 0.602, gamma 0.101, A = N/10), with each parameter's final
perturbation c_end and learning rate r_end from the engine's `tunables` list.
Only a share of the fleet takes part, so self-play data keeps coming.

The tuned values are only a candidate: they ship only after passing an
ordinary gate against the current settings (forge/test_queue.py).
"""

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet  # noqa: E402

DATA = os.path.expanduser("~/arhanpassant-data")
RUNS = os.path.join(DATA, "forge", "spsa")
ALPHA, GAMMA = 0.602, 0.101


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def tunables(engine):
    out = subprocess.run([engine], input="tunables\nquit\n", capture_output=True, text=True, check=True).stdout
    rows = {}
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 7 and parts[1] == "int":
            name, _, default, lo, hi, c_end, r_end = parts
            rows[name] = {"default": int(default), "min": int(lo), "max": int(hi), "c_end": float(c_end), "r_end": float(r_end)}
    return rows


def new_run(args):
    state = json.load(open(os.path.join(DATA, "forge", "state.json")))
    accepted = json.load(open(os.path.join(DATA, "forge", "search.json")))["accepted"]
    rows = tunables(args.engine)
    params = []
    for name, t in rows.items():
        value = int(accepted.get(name, t["default"]))
        # On/off switches are not tuned, nor the strength of a feature that is off.
        if t["max"] - t["min"] <= 1 or (t["min"] == 0 and value == 0):
            continue
        if args.params and name not in args.params.split(","):
            continue
        params.append({"name": name, "start": value, "theta": float(value), "min": t["min"], "max": t["max"],
                       "c_end": t["c_end"], "r_end": t["r_end"]})
    n = args.pairs
    big_a = 0.1 * n
    for p in params:
        p["c"] = p["c_end"] * n ** GAMMA
        p["a"] = p["r_end"] * p["c_end"] ** 2 * (big_a + n) ** ALPHA
    fixed = {k: v for k, v in accepted.items() if k not in {p["name"] for p in params}}
    run = {"id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S"), "pairs": n, "A": big_a, "k": 0,
           "net": state["champion_net"], "tc": args.tc, "share": args.share, "fixed": fixed, "params": params,
           "seen": [], "results": {"w": 0, "l": 0, "d": 0}, "history": []}
    fleet.put(f"nets/{os.path.basename(run['net'])}", open(run["net"], "rb").read())
    return run


def publish(run, status="running"):
    k = max(run["k"], 1)
    spec = {
        "id": run["id"], "status": status, "net": f"nets/{os.path.basename(run['net'])}", "tc": run["tc"],
        "share": run["share"], "fixed": run["fixed"], "pairs_per_worker": 3, "k": run["k"],
        "heartbeat": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S"),
        "params": [{"name": p["name"], "value": round(p["theta"], 3), "c": round(p["c"] / k ** GAMMA, 4),
                    "min": p["min"], "max": p["max"]} for p in run["params"]],
    }
    fleet.put("control/spsa.json", json.dumps(spec).encode())


def save(run):
    os.makedirs(RUNS, exist_ok=True)
    path = os.path.join(RUNS, f"{run['id']}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(run, f, indent=1)
    os.replace(path + ".tmp", path)
    with open(os.path.join(RUNS, "active"), "w") as f:
        f.write(run["id"] + "\n")


def load_active():
    try:
        rid = open(os.path.join(RUNS, "active")).read().strip()
        run = json.load(open(os.path.join(RUNS, f"{rid}.json")))
        return run if run["k"] < run["pairs"] and not run.get("stopped") else None
    except FileNotFoundError:
        return None


def apply(run, batch):
    """Each game pair is one SPSA step: theta += a_k / c_k * (wins - losses of the plus side) * direction."""
    names = [p["name"] for p in run["params"]]
    if batch.get("params") != names:
        return 0
    for pair in batch["pairs"]:
        run["k"] += 1
        k = run["k"]
        result = pair["w"] - pair["l"]
        for p, flip in zip(run["params"], pair["flips"]):
            c_k = p["c"] / k ** GAMMA
            a_k = p["a"] / (run["A"] + k) ** ALPHA
            p["theta"] = min(p["max"], max(p["min"], p["theta"] + a_k / c_k * result * flip))
        for x in ("w", "l", "d"):
            run["results"][x] += pair[x]
    return len(batch["pairs"])


def show(run):
    log(f"SPSA {run['id']}: {run['k']:,} of {run['pairs']:,} pairs; plus side W-L-D "
        f"{run['results']['w']}-{run['results']['l']}-{run['results']['d']}")
    for p in run["params"]:
        moved = p["theta"] - p["start"]
        log(f"  {p['name']:>16} {p['start']:>6} -> {p['theta']:8.2f} ({moved:+.2f})")


def run_loop(args):
    run = load_active() or new_run(args)
    save(run)
    log(f"SPSA {run['id']}: {len(run['params'])} parameters, {run['pairs']:,} pairs, share {run['share']}, "
        f"{os.path.basename(run['net'])}, {run['tc']}; fixed {run['fixed']}")
    seen = set(run["seen"])
    last_show = 0.0
    try:
        while run["k"] < run["pairs"]:
            publish(run)
            for name in sorted(fleet.list_names(f"spsa/{run['id']}/")):
                if name in seen:
                    continue
                seen.add(name)
                run["seen"].append(name)
                apply(run, json.loads(fleet.get(name)))
            if run["k"] // 1000 > len(run["history"]):
                run["history"].append({"k": run["k"], "theta": {p["name"]: round(p["theta"], 2) for p in run["params"]}})
            save(run)
            if time.time() - last_show > 1800:
                show(run)
                last_show = time.time()
            time.sleep(60)
    finally:
        publish(run, status="done" if run["k"] >= run["pairs"] else "paused")
        save(run)
    show(run)
    tuned = {p["name"]: str(round(p["theta"])) for p in run["params"] if round(p["theta"]) != p["start"]}
    log(f"SPSA {run['id']} finished; values that moved: {tuned}")


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run", "show", "stop"])
    ap.add_argument("--pairs", type=int, default=30000)
    ap.add_argument("--share", type=float, default=0.6, help="fraction of the fleet that takes part")
    ap.add_argument("--tc", default="8+0.08")
    ap.add_argument("--params", help="comma-separated subset to tune (default: all that are on)")
    ap.add_argument("--engine", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                     "target", "release", "arhanpassant"))
    args = ap.parse_args()
    if args.command == "run":
        run_loop(args)
    elif args.command == "show":
        run = load_active()
        show(run) if run else print("no active run")
    else:
        run = load_active()
        if run:
            run["stopped"] = True
            save(run)
            publish(run, status="done")
            log(f"stopped SPSA {run['id']} at {run['k']:,} pairs")


if __name__ == "__main__":
    main()
