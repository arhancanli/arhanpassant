"""The ArhanPassant improvement loop: generate, train, gate, promote, repeat.

Each round waits for enough new self-play data, trains a candidate network,
and runs it through the SPRT gate against the current champion. A candidate
that passes becomes the champion, is logged in forge/ledger.json, and local
self-play switches to it so the next round learns from stronger games.

State lives in files (DATA/forge/state.json), never in this process, so the
loop can be stopped and restarted at any time.

    .venv/bin/python forge/loop.py --data ~/arhanpassant-data
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import signal
import sys
import time

import fleet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "forge", "ledger.json")
REC = 32


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def data_files(data):
    return sorted(glob.glob(os.path.join(data, "selfplay", "**", "*.bin"), recursive=True))


def positions(files):
    return sum(os.path.getsize(f) // REC for f in files)


def grown_since(files, sizes):
    """Positions added since `sizes` ({path: bytes}) was recorded; new files count in full."""
    return sum(max(0, os.path.getsize(f) - sizes.get(f, 0)) // REC for f in files)


def hidden_for(n):
    """Grow the network as the data grows."""
    if n < 20_000_000:
        return 128
    if n < 100_000_000:
        return 256
    if n < 500_000_000:
        return 512
    return 1024


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def run(cmd, logfile):
    with open(logfile, "a") as f:
        f.write(f"$ {' '.join(cmd)}\n")
        f.flush()
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def start_selfplay(args, state):
    """(Re)start local self-play with the champion network."""
    pid = state.get("selfplay_pid")
    if alive(pid):
        os.kill(pid, 15)
    gen = state["generation"]
    out = os.path.join(args.data, "selfplay", f"gen{gen}")
    os.makedirs(out, exist_ok=True)
    cmd = ["nice", "-n", "19", args.engine, "datagen", "--threads", str(args.selfplay_threads),
           "--nodes", str(args.selfplay_nodes), "--seed", str(int(time.time())), "--out", out]
    if state.get("champion_net"):
        cmd += ["--net", state["champion_net"]]
    logf = open(os.path.join(args.data, "forge", f"selfplay-gen{gen}.log"), "a")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    state["selfplay_pid"] = p.pid
    log(f"self-play generation {gen} running (pid {p.pid}) with {state.get('champion_net') or 'hand-written evaluation'}")


def search_settings(args):
    """Search settings accepted by the fleet's test queue (forge/test_queue.py); both sides of a gate use them."""
    return load_json(os.path.join(args.data, "forge", "search.json"), {"accepted": {}})["accepted"]


def gate(args, candidate, state, logfile):
    out = os.path.join(args.data, "forge", f"sprt-{os.path.basename(candidate)}.json")
    opts = search_settings(args)
    if args.fleet and state.get("champion_net"):
        try:
            alive = fleet.nodes_alive()
            if alive >= args.fleet_min_nodes:
                log(f"gate on the fleet ({alive} nodes reporting)")
                r = fleet.remote_gate(candidate, state["champion_net"], tc=args.tc, elo0=args.elo0, elo1=args.elo1,
                                      max_games=args.max_games, log=log, cand_opts=opts, champ_opts=opts,
                                      src=fleet.current_src(), name=f"network {os.path.basename(candidate)}")
                if r:
                    save_json(out, r)
                    return r
            log(f"fleet unavailable ({alive} nodes reporting); gating locally")
        except Exception as e:  # network trouble: fall back to the local gate
            log(f"fleet gate error ({e}); gating locally")
    settings = [f"opt.{k}={v}" for k, v in opts.items()]
    champ = ["--engine", "name=champion", f"cmd={args.engine}", *settings]
    if state.get("champion_net"):
        champ.append(f"opt.EvalFile={state['champion_net']}")
    else:
        champ.append("opt.EvalFile=<none>")
    cmd = [args.arena, "--engine", "name=candidate", f"cmd={args.engine}", f"opt.EvalFile={candidate}", *settings, *champ,
           "--tc", args.tc, "--book", args.book, "--concurrency", str(args.concurrency), "--games", str(args.max_games),
           "--sprt", f"{args.elo0},{args.elo1}", "--nice", "10", "--seed", str(int(time.time())), "--quiet", "--out", out]
    if run(cmd, logfile) != 0:
        return None
    return load_json(out, None)


def promote_if_passed(args, state, candidate, result, change):
    """Log a gate's result; when it passed, make the candidate the champion everywhere."""
    if not result:
        log("gate failed to run; see loop.log")
        time.sleep(600)
        return
    log(f"gate result {result['decision']}: {result['games']} games, elo {result['elo']:+.1f} [{result['elo_lo']:+.1f}, {result['elo_hi']:+.1f}], llr {result['sprt']['llr']:.2f}")
    if result["decision"] != "H1":
        return
    major, minor, _ = (int(x) for x in state["champion_version"].split("."))
    version = f"{major}.{minor + 1}.0"
    champion = os.path.join(args.data, "nets", f"champion-{version}.nnue")
    shutil.copy(candidate, champion)
    ledger = load_json(LEDGER, {"versions": []})
    ledger["versions"].append({
        "version": version,
        "date": dt.date.today().isoformat(),
        "change": change,
        "status": "promoted",
        "games": result["games"],
        "elo": round(result["elo"], 1),
        "eloLo": round(result["elo_lo"], 1),
        "eloHi": round(result["elo_hi"], 1),
        "llr": round(result["sprt"]["llr"], 2),
        "net": os.path.basename(champion),
    })
    save_json(LEDGER, ledger)
    state.update(champion_net=champion, champion_version=version, generation=state["generation"] + 1)
    log(f"PROMOTED {version}")
    start_selfplay(args, state)
    if args.fleet:
        try:
            fleet.set_net(champion, f"gen{state['generation']}", nodes=args.fleet_nodes)
            log(f"fleet self-play switched to {os.path.basename(champion)}")
        except Exception as e:
            log(f"could not switch the fleet ({e})")


def main():
    # A plain kill (SIGTERM) unwinds normally, so a running fleet gate is closed, not left holding the fleet.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/arhanpassant-data"))
    ap.add_argument("--engine", default=os.path.expanduser("~/arhanpassant-data/bin/ap-0.1.0"))
    ap.add_argument("--arena", default=os.path.expanduser("~/arhanpassant-data/bin/arena"))
    ap.add_argument("--book", default=os.path.join(ROOT, "tools", "books", "UHO_4060_v4.epd"))
    ap.add_argument("--tc", default="8+0.08")
    ap.add_argument("--elo0", type=float, default=0.0)
    ap.add_argument("--elo1", type=float, default=5.0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-games", type=int, default=12000)
    ap.add_argument("--fleet", action="store_true", help="run gates on the cloud fleet and keep its self-play on the champion")
    ap.add_argument("--fleet-min-nodes", type=int, default=4)
    ap.add_argument("--fleet-nodes", type=int, default=8000, help="nodes per move for fleet self-play")
    ap.add_argument("--selfplay-threads", type=int, default=4)
    ap.add_argument("--selfplay-nodes", type=int, default=5000)
    ap.add_argument("--min-new", type=int, default=5_000_000, help="new positions needed before the next attempt")
    ap.add_argument("--samples", type=int, default=1_200_000_000, help="training samples per round (sets the epoch count)")
    ap.add_argument("--input-buckets", type=int, default=8, help="king buckets for new networks (1 or 8)")
    ap.add_argument("--output-buckets", type=int, default=8, help="piece-count output heads for new networks")
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument("--gate-first", help="a network already trained: gate it before training anything")
    ap.add_argument("--gate-first-change", help="ledger description of --gate-first's network")
    args = ap.parse_args()

    forge_dir = os.path.join(args.data, "forge")
    os.makedirs(forge_dir, exist_ok=True)
    state_path = os.path.join(forge_dir, "state.json")
    state = load_json(state_path, {"generation": 0, "champion_net": None, "champion_version": "0.1.0", "trained_on": 0, "attempts": 0})
    logfile = os.path.join(forge_dir, "loop.log")

    if not alive(state.get("selfplay_pid")):
        start_selfplay(args, state)
        save_json(state_path, state)

    first = args.gate_first
    while True:
        files = data_files(args.data)
        n = positions(files)
        if first:
            candidate, change, first = first, args.gate_first_change or "NNUE network", None
            state["attempts"] += 1
            log(f"round {state['attempts']}: gating {os.path.basename(candidate)}, trained before the loop started")
            result = gate(args, candidate, state, logfile)
            promote_if_passed(args, state, candidate, result, change)
            save_json(state_path, state)
            continue
        # New data = growth since the last training round, so deleting old
        # generations never looks like lost data and growing files count once.
        fresh = grown_since(files, state["sizes"]) if "sizes" in state else n - state["trained_on"]
        if fresh < args.min_new:
            log(f"waiting for data: {n:,} positions, {fresh:,} new of {args.min_new:,} needed")
            if args.once:
                return
            time.sleep(600)
            continue

        state["attempts"] += 1
        hidden = hidden_for(n)
        shape = f" ({args.input_buckets} king buckets, {args.output_buckets} output buckets)" if args.input_buckets * args.output_buckets > 1 else ""
        change = f"NNUE network{shape}, {hidden} hidden units, trained on {n / 1e6:.1f}M self-play positions"
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        layout = f"-{args.input_buckets}x{args.output_buckets}" if args.input_buckets * args.output_buckets > 1 else ""
        candidate = os.path.join(args.data, "nets", f"cand-{stamp}-h{hidden}{layout}.nnue")
        os.makedirs(os.path.dirname(candidate), exist_ok=True)
        log(f"round {state['attempts']}: training h{hidden} on {n:,} positions -> {candidate}")
        # A fixed sample budget keeps each round's training time steady as data grows.
        epochs = max(2, min(15, args.samples // max(n, 1)))
        rc = run([sys.executable, os.path.join(ROOT, "trainer", "train.py"), "--data", os.path.join(args.data, "selfplay"),
                  "--hidden", str(hidden), "--epochs", str(epochs), "--input-buckets", str(args.input_buckets),
                  "--output-buckets", str(args.output_buckets), "--out", candidate], logfile)
        state["trained_on"] = n
        state["sizes"] = {f: os.path.getsize(f) for f in files}
        save_json(state_path, state)
        if rc != 0 or not os.path.exists(candidate):
            log("training failed; see loop.log")
            time.sleep(600)
            continue

        log(f"gate: {os.path.basename(candidate)} vs champion {state['champion_version']}")
        result = gate(args, candidate, state, logfile)
        promote_if_passed(args, state, candidate, result, change)
        save_json(state_path, state)
        if args.once:
            return


if __name__ == "__main__":
    main()
