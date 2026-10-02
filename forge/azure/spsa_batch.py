"""Play one batch of SPSA game pairs on a fleet node and write the results.

    python3 spsa_batch.py SPSA.json HOST OUT.json
    python3 spsa_batch.py SPSA.json HOST --check     # exit 0 only if this node takes part

SPSA.json is control/spsa.json: the parameters' current values (theta) and
perturbation sizes (c), the settings both sides share, the network and the
time control. Each game pair draws its own random direction: every parameter
moves by +c or -c, rounded stochastically to an integer, and the "plus" engine
plays the "minus" engine twice from one opening with colours swapped. The
coordinator (forge/spsa.py) turns the results into parameter updates.

Exits 1 when the run is not for this node (finished, or outside its share).
"""

import concurrent.futures
import hashlib
import json
import math
import os
import random
import subprocess
import sys


def stochastic_round(x, rng):
    return math.floor(x + rng.random())


def main():
    spec_path, host, out_path = sys.argv[1:4]
    spec = json.load(open(spec_path))
    if spec.get("status") != "running":
        sys.exit(1)
    # Take part only within the run's share of the fleet; the rest keeps generating training data.
    if int(hashlib.sha1(host.encode()).hexdigest(), 16) % 1000 >= spec.get("share", 1.0) * 1000:
        sys.exit(1)
    if out_path == "--check":
        sys.exit(0)
    params = spec["params"]
    workers = max(1, (os.cpu_count() or 2) - 1)
    pairs = spec.get("pairs_per_worker", 3) * workers
    net = os.path.basename(spec["net"])
    fixed = [f"opt.{k}={v}" for k, v in (spec.get("fixed") or {}).items()]
    base_seed = int.from_bytes(os.urandom(4), "little")

    def one_pair(i):
        rng = random.Random(base_seed + i)
        flips = [rng.choice((-1, 1)) for _ in params]
        plus, minus = [], []
        for p, f in zip(params, flips):
            for side, sign in ((plus, 1), (minus, -1)):
                v = stochastic_round(p["value"] + sign * f * p["c"], rng)
                side.append(f"opt.{p['name']}={min(p['max'], max(p['min'], v))}")
        out = f"spsa-pair-{base_seed}-{i}.json"
        cmd = ["src/target/release/arena",
               "--engine", "name=plus", "cmd=./arhanpassant", f"opt.EvalFile=nets/{net}", *fixed, *plus,
               "--engine", "name=minus", "cmd=./arhanpassant", f"opt.EvalFile=nets/{net}", *fixed, *minus,
               "--tc", spec["tc"], "--book", "book.epd", "--concurrency", "1", "--games", "2",
               "--seed", str(base_seed + i), "--quiet", "--out", out]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = json.load(open(out))
        os.remove(out)
        return {"flips": flips, "w": r["wins"], "l": r["losses"], "d": r["draws"]}

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one_pair, range(pairs)))
    json.dump({"id": spec["id"], "host": host, "params": [p["name"] for p in params], "pairs": results},
              open(out_path, "w"))


if __name__ == "__main__":
    main()
