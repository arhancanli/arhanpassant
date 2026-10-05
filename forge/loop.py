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
import contextlib
import datetime as dt
import fcntl
import glob
import json
import os
import shutil
import subprocess
import signal
import re
import sys
import time

import local_gate
import milestones
import diagnostics
import publish_data

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


def size(f):
    """Bytes in a data file, or 0 once it is gone (the pull loop deletes the oldest data to free disk)."""
    try:
        return os.path.getsize(f)
    except FileNotFoundError:
        return 0


def positions(files):
    return sum(size(f) // REC for f in files)


def grown_since(files, sizes):
    """Positions added since `sizes` ({path: bytes}) was recorded; new files count in full."""
    return sum(max(0, size(f) - sizes.get(f, 0)) // REC for f in files)


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
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def run(cmd, logfile):
    return local_gate.run(cmd, logfile)


@contextlib.contextmanager
def prevent_idle_sleep(enabled):
    """The macOS assertion belongs to this controller and expires if it dies."""
    if not enabled:
        yield
        return
    guard = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield
    finally:
        guard.terminate()
        try:
            guard.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guard.kill()
            guard.wait()


def stop_selfplay(args, state):
    """Only stop a PID that still belongs to this data directory's generator."""
    pid = state.pop("selfplay_pid", None)
    state["selfplay_threads"] = 0
    if not alive(pid):
        return
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True).stdout
    if " datagen " not in command or f"--out {args.data}/selfplay/" not in command:
        log(f"ignoring stale self-play pid {pid}; it is no longer our generator")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not alive(pid):
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)


def storage_ready(args, state):
    """Bound disposable self-play data, retaining all untrained positions.

    Stop writers before trimming. Only complete generated files recorded in
    the last successful training snapshot may be removed; networks, games,
    match evidence and untrained chunks are always retained.
    """
    files = data_files(args.data)
    used = sum(size(f) for f in files)
    free = shutil.disk_usage(args.data).free
    budget, floor = int(args.max_data_gb * 2**30), int(args.min_free_gb * 2**30)
    if used <= budget and free >= floor:
        return True
    stop_selfplay(args, state)
    known = state.get("sizes", {})
    eligible = []
    root = os.path.realpath(os.path.join(args.data, "selfplay")) + os.sep
    for path in files:
        directory = os.path.basename(os.path.dirname(path))
        match = re.fullmatch(r"(?:fleet-)?gen(\d+)", directory)
        n = size(path)
        if match and n and known.get(path) == n and os.path.realpath(path).startswith(root):
            eligible.append((int(match[1]), os.path.getmtime(path), path, n))
    removed = 0
    for _, _, path, n in sorted(eligible):
        if used <= budget - (256 << 20) and free >= floor + (512 << 20):
            break
        os.remove(path)
        used -= n
        removed += 1
        free = shutil.disk_usage(args.data).free
    if removed:
        log(f"retired {removed} already-trained self-play chunks; {used / 2**30:.2f} GiB data, {free / 2**30:.2f} GiB free")
    ready = used <= budget and free >= floor
    if not ready:
        log(f"self-play paused for disk space: {used / 2**30:.2f} GiB data, {free / 2**30:.2f} GiB free; untrained data retained")
    return ready


def start_selfplay(args, state, threads=None):
    """(Re)start local self-play with the champion network."""
    stop_selfplay(args, state)
    threads = args.selfplay_threads if threads is None else threads
    if threads == 0:
        return
    gen = state["generation"]
    out = os.path.join(args.data, "selfplay", f"gen{gen}")
    os.makedirs(out, exist_ok=True)
    cmd = ["nice", "-n", "19", args.engine, "datagen", "--threads", str(threads),
           "--nodes", str(args.selfplay_nodes), "--seed", str(int.from_bytes(os.urandom(8), "little")), "--out", out]
    cmd += ["--positions-per-file", str(args.chunk_positions)]
    opts = search_settings(args)
    for name, value in opts.items():
        cmd += ["--set", f"{name}={value}"]
    if state.get("champion_net"):
        cmd += ["--net", state["champion_net"]]
    logf = open(os.path.join(args.data, "forge", f"selfplay-gen{gen}.log"), "a")
    try:
        p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        logf.close()
    state["selfplay_pid"] = p.pid
    state["selfplay_threads"] = threads
    state["selfplay_options"] = opts
    log(f"self-play generation {gen}: {threads} threads (pid {p.pid}) with {state.get('champion_net') or 'hand-written evaluation'}")


def ensure_selfplay(args, state, threads):
    if threads == 0:
        stop_selfplay(args, state)
        return
    if storage_ready(args, state):
        if (not alive(state.get("selfplay_pid")) or state.get("selfplay_threads") != threads
                or state.get("selfplay_options") != search_settings(args)):
            start_selfplay(args, state, threads)


def search_settings(args):
    """Search settings accepted by the fleet's test queue (forge/test_queue.py); both sides of a gate use them."""
    return load_json(os.path.join(args.data, "forge", "search.json"), {"accepted": {}})["accepted"]


def search_threads_for(args, item):
    """Both sides receive the same thread count; it is a test condition, not a tuned setting."""
    if any(k.lower() == "threads" for k in item["opts"]):
        raise ValueError("put Threads in common_options so both engines have the same CPU budget")
    common = item.get("common_options", {})
    thread_values = [v for k, v in common.items() if k.lower() == "threads"]
    threads = int(thread_values[0]) if thread_values else 1
    if len(thread_values) > 1 or not 1 <= threads <= args.cpu_budget:
        raise ValueError(f"invalid search test thread count: {thread_values}")
    return threads


def selfplay_workers(args, item):
    reserved = max(args.cpu_budget // 2, search_threads_for(args, item))
    return min(args.selfplay_threads, args.cpu_budget - reserved)


def gate_workers(args, state, threads=1):
    playing = state.get("selfplay_threads", 0) if alive(state.get("selfplay_pid")) else 0
    workers = min(args.concurrency, (args.cpu_budget - playing) // threads)
    if workers < 1:
        raise ValueError("search test has no cores available; reduce self-play first")
    return workers


def search_evidence(args, state, item):
    engine = os.path.expanduser(item.get("candidate_engine", args.engine))
    baseline = os.path.expanduser(item.get("baseline_engine", engine))
    identity = local_gate.build_identity(engine, state["champion_net"], baseline)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", item["name"])
    out = os.path.join(args.data, "forge", "tests", f"local-{name}-{identity}.json")
    return engine, baseline, identity, out


def accepted_checkpoint(previous, item, search, out):
    if not previous or previous["decision"] != "H1":
        return False
    config = previous.get("config", {})
    if not isinstance(config.get("champion_options"), dict) or not isinstance(config.get("candidate_options"), dict):
        return False
    return (any(e.get("evidence") == os.path.basename(out) for e in search["history"])
            and config["candidate_options"] == {**config["champion_options"], **item["opts"]}
            and all(config["champion_options"].get(k) == v for k, v in item.get("common_options", {}).items()))


def search_has_progress(args, state, item, ready):
    """Finish an existing gate before training changes its network. A queued
    test with no completed pairs does not delay a ready training round."""
    engine, baseline, _, out = search_evidence(args, state, item)
    previous = load_json(out, None)
    if not previous or previous.get("games", 0) == 0:
        return False
    search = load_json(os.path.join(args.data, "forge", "search.json"), {"accepted": {}, "history": []})
    if accepted_checkpoint(previous, item, search, out):
        return True  # Recover acceptance before dequeuing, before a new network changes the filename.
    threads = search_threads_for(args, item)
    # At startup the old generator has been stopped; plan the allocation that
    # ensure_selfplay will establish, rather than mistaking the gap for a new profile.
    playing = selfplay_workers(args, item) if ready else 0
    workers = min(args.concurrency, (args.cpu_budget - playing) // threads)
    base = {**search["accepted"], **item.get("common_options", {})}
    elo0, elo1 = item.get("bounds", [0.0, 5.0])
    expected = local_gate.configuration(engine=engine, baseline_engine=baseline, arena=args.arena,
                                         candidate=state["champion_net"], champion=state["champion_net"],
                                         book=args.book, tc=args.tc, concurrency=workers, max_games=args.max_games,
                                         elo0=elo0, elo1=elo1, cand_opts={**base, **item["opts"]}, champ_opts=base,
                                         batch_games=args.batch_games)
    return previous.get("config") == expected


def gate(args, candidate, state, logfile):
    out = os.path.join(args.data, "forge", f"sprt-{os.path.basename(candidate)}.json")
    opts = search_settings(args)
    if args.fleet and state.get("champion_net"):
        import fleet
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
    return local_gate.gate(engine=args.engine, arena=args.arena, candidate=candidate,
                           champion=state.get("champion_net"), book=args.book, tc=args.tc,
                           concurrency=args.concurrency, max_games=args.max_games,
                           elo0=args.elo0, elo1=args.elo1, cand_opts=opts, champ_opts=opts,
                           out=out, logfile=logfile, log=log, batch_games=args.batch_games)


def search_batch(args, state, logfile):
    """Advance the existing search queue locally while self-play uses the other cores."""
    queue_path = os.path.join(args.data, "forge", "queue.json")
    queue = load_json(queue_path, {"pending": [], "done": []})
    if not queue["pending"]:
        return
    item = queue["pending"][0]
    threads = search_threads_for(args, item)
    base = {**search_settings(args), **item.get("common_options", {})}
    elo0, elo1 = item.get("bounds", [0.0, 5.0])
    engine, baseline, identity, out = search_evidence(args, state, item)
    directory = os.path.join(args.data, "forge", "tests")
    os.makedirs(directory, exist_ok=True)
    workers = gate_workers(args, state, threads)
    previous = load_json(out, None)
    search = load_json(os.path.join(args.data, "forge", "search.json"), {"accepted": {}, "history": []})
    if accepted_checkpoint(previous, item, search, out):
        # Recover a crash after acceptance but before dequeuing with the
        # original baseline rather than testing the change against itself.
        result = previous
        base = previous["config"]["champion_options"]
    else:
        result = local_gate.gate(engine=engine, baseline_engine=baseline, arena=args.arena,
                                 candidate=state["champion_net"], champion=state["champion_net"],
                                 book=args.book, tc=args.tc, concurrency=max(1, workers),
                                 max_games=args.max_games, elo0=elo0, elo1=elo1,
                                 cand_opts={**base, **item["opts"]}, champ_opts=base,
                                 out=out, logfile=logfile, log=log, batch_games=args.batch_games, max_batches=1)
    if result["decision"] == "running":
        return
    entry = {"date": dt.date.today().strftime("%Y%m%d"), "name": item["name"], "change": item["change"],
             "candidate": {**base, **item["opts"]}, "baseline": base,
             "network": os.path.basename(state["champion_net"]), "build": identity,
             "tc": args.tc, "bounds": [elo0, elo1], "games": result["games"],
             "elo": round(result["elo"], 1), "eloLo": round(result["elo_lo"], 1),
             "eloHi": round(result["elo_hi"], 1), "llr": round(result["sprt"]["llr"], 2),
             "decision": result["decision"], "runtime": "local", "evidence": os.path.basename(out)}
    ledger_path = os.path.join(ROOT, "forge", "tests.json")
    ledger = load_json(ledger_path, {"tests": []})
    if not any(e.get("evidence") == entry["evidence"] for e in ledger["tests"]):
        ledger["tests"].append(entry)
        save_json(ledger_path, ledger)
    if result["decision"] == "H1":
        path = os.path.join(args.data, "forge", "search.json")
        search = load_json(path, {"accepted": {}, "history": []})
        if not any(e.get("evidence") == entry["evidence"] for e in search["history"]):
            search["accepted"].update(item["opts"])
            search["history"].append({"name": item["name"], "opts": item["opts"],
                                      "elo": entry["elo"], "evidence": entry["evidence"]})
            save_json(path, search)
            log(f"accepted search change {item['name']} after {result['games']} games")
    queue = load_json(queue_path, {"pending": [], "done": []})
    queue["pending"] = [p for p in queue["pending"] if p != item]
    if not any(e.get("evidence") == entry["evidence"] for e in queue["done"]):
        queue["done"].append({**item, "decision": entry["decision"], "elo": entry["elo"],
                              "games": entry["games"], "evidence": entry["evidence"]})
    save_json(queue_path, queue)
    if not args.no_publish:
        publish_data.publish_quietly(log)


def promote_if_passed(args, state, candidate, result, change):
    """Log a gate's result; when it passed, make the candidate the champion everywhere."""
    if not result:
        raise RuntimeError("gate failed to run; see loop.log")
    log(f"gate result {result['decision']}: {result['games']} games, elo {result['elo']:+.1f} [{result['elo_lo']:+.1f}, {result['elo_hi']:+.1f}], llr {result['sprt']['llr']:.2f}")
    if result["decision"] != "H1":
        return
    major, minor, _ = (int(x) for x in state["champion_version"].split("."))
    version = f"{major}.{minor + 1}.0"
    champion = os.path.join(args.data, "nets", f"champion-{version}.nnue")
    shutil.copy(candidate, champion)
    ledger = load_json(LEDGER, {"versions": []})
    ledger["versions"] = [v for v in ledger["versions"] if v["version"] != version]
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
    state.pop("pending", None)
    log(f"PROMOTED {version}")
    # Publishing reads state.json, so commit the new champion first.
    save_json(os.path.join(args.data, "forge", "state.json"), state)
    if not args.no_publish:
        publish_data.publish_quietly(log)
    if args.fleet:
        import fleet
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
    ap.add_argument("--cpu-budget", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--fleet", action="store_true", help="run gates on the cloud fleet and keep its self-play on the champion")
    ap.add_argument("--fleet-min-nodes", type=int, default=4)
    ap.add_argument("--fleet-nodes", type=int, default=8000, help="nodes per move for fleet self-play")
    ap.add_argument("--selfplay-threads", type=int, help="default: the CPU budget")
    ap.add_argument("--selfplay-nodes", type=int, default=5000)
    ap.add_argument("--min-new", type=int, default=5_000_000, help="new positions needed before the next attempt")
    ap.add_argument("--samples", type=int, default=1_200_000_000, help="training samples per round (sets the epoch count)")
    ap.add_argument("--input-buckets", type=int, default=8, help="king buckets for new networks (1 or 8)")
    ap.add_argument("--output-buckets", type=int, default=8, help="piece-count output heads for new networks")
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument("--gate-first", help="a network already trained: gate it before training anything")
    ap.add_argument("--gate-first-change", help="ledger description of --gate-first's network")
    ap.add_argument("--local-search", action="store_true", help="advance the search queue on local cores between network rounds")
    ap.add_argument("--batch-games", type=int, default=64, help="checkpoint local gates after this many games")
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument("--chunk-positions", type=int, default=1_000_000)
    ap.add_argument("--max-data-gb", type=float, default=14, help="maximum GiB of disposable self-play data")
    ap.add_argument("--min-free-gb", type=float, default=8, help="pause generation below this many free GiB")
    ap.add_argument("--train-workers", type=int, default=8)
    ap.add_argument("--train-threads", type=int, default=4)
    ap.add_argument("--train-hidden", type=int, help="explicit hidden width for a gated network-capacity experiment")
    ap.add_argument("--no-publish", action="store_true", help="keep promotion and search ledgers local")
    ap.add_argument("--keep-awake", action="store_true", help="prevent macOS idle sleep for this controller's lifetime")
    ap.add_argument("--milestone-catalog", help="verified local opponents; alternate milestone matches with search gates")
    ap.add_argument("--loss-analysis-sample", type=int, default=32,
                    help="review this many saved losses after opponent coverage and suite completion; 0 disables")
    args = ap.parse_args()
    if args.keep_awake and sys.platform != "darwin":
        ap.error("--keep-awake requires macOS caffeinate")
    args.data = os.path.abspath(os.path.expanduser(args.data))
    args.cpu_budget = max(1, min(args.cpu_budget, os.cpu_count() or 1))
    args.selfplay_threads = min(args.selfplay_threads or args.cpu_budget, args.cpu_budget)
    args.concurrency = min(args.concurrency, args.cpu_budget)
    if args.loss_analysis_sample < 0:
        ap.error("loss-analysis-sample must be nonnegative")
    if min(args.selfplay_threads, args.concurrency, args.poll_seconds, args.chunk_positions,
           args.train_workers, args.train_threads, args.max_data_gb) <= 0 or args.min_free_gb < 0:
        ap.error("CPU, training, polling and storage budgets must be positive")
    if args.batch_games < 2 or args.batch_games % 2 or args.max_games < 2 or args.max_games % 2:
        ap.error("gate budgets must contain complete game pairs")
    if args.train_hidden is not None and (not 8 <= args.train_hidden <= 8192 or args.train_hidden % 8):
        ap.error("hidden width must be a multiple of eight between 8 and 8192")
    if args.min_new * REC > args.max_data_gb * 2**30:
        ap.error("the fresh-data threshold cannot exceed the self-play storage budget")

    forge_dir = os.path.join(args.data, "forge")
    os.makedirs(forge_dir, exist_ok=True)
    with open(os.path.join(forge_dir, "loop.lock"), "a") as controller_lock:
        try:
            fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another improvement controller owns this data directory; exiting")
            return
        with prevent_idle_sleep(args.keep_awake):
            run_controller(args, forge_dir)


def run_controller(args, forge_dir):
    state_path = os.path.join(forge_dir, "state.json")
    state = load_json(state_path, {"generation": 0, "champion_net": None, "champion_version": "0.1.0", "trained_on": 0, "attempts": 0})
    logfile = os.path.join(forge_dir, "loop.log")

    state["cpu_budget"] = args.cpu_budget
    state["controller_pid"] = os.getpid()
    # Reconcile the running generator with this controller's build and budget.
    stop_selfplay(args, state)
    if args.gate_first and not state.get("pending"):
        state["attempts"] += 1
        state["pending"] = {"candidate": args.gate_first,
                            "change": args.gate_first_change or "NNUE network"}
    log(f"controller started: CPU budget {args.cpu_budget}, self-play up to {args.selfplay_threads}, "
        f"gate concurrency {args.concurrency}, local search {args.local_search}")
    if args.milestone_catalog and args.cpu_budget < 2:
        raise ValueError("milestone matches need a CPU budget of at least two")
    try:
        while True:
            state["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                state.pop("error", None)
                state.pop("training_deferred_for", None)
                state.pop("activity", None)
                if args.milestone_catalog and state.get("champion_net"):
                    milestones.enqueue(args, state, search_settings(args))
                if state.get("pending"):
                    stop_selfplay(args, state)
                    state["phase"] = "network gate"
                    save_json(state_path, state)
                    pending = state["pending"]
                    log(f"gate: {os.path.basename(pending['candidate'])} vs champion {state['champion_version']}")
                    result = gate(args, pending["candidate"], state, logfile)
                    promote_if_passed(args, state, pending["candidate"], result, pending["change"])
                    state.pop("pending", None)
                    save_json(state_path, state)
                    if args.once:
                        return
                    continue

                ready = storage_ready(args, state)
                files = data_files(args.data)
                # Freeze file sizes BEFORE training. Bytes generated or pulled
                # during training belong to the next round.
                snapshot = {f: size(f) for f in files if size(f)}
                n = sum(v // REC for v in snapshot.values())
                fresh = grown_since(files, state["sizes"]) if "sizes" in state else n - state["trained_on"]
                queue = load_json(os.path.join(forge_dir, "queue.json"), {"pending": []})
                testing = args.local_search and bool(queue["pending"]) and args.cpu_budget > 1
                external = bool(args.milestone_catalog and milestones.pending(args.data))
                review = bool(args.milestone_catalog and args.loss_analysis_sample
                              and diagnostics.choose(args.data, args.loss_analysis_sample))
                finish_search = (fresh >= args.min_new and testing
                                 and search_has_progress(args, state, queue["pending"][0], ready))
                if fresh < args.min_new or finish_search:
                    threads = (selfplay_workers(args, queue["pending"][0]) if testing else
                               min(args.selfplay_threads, args.cpu_budget - max(1, args.cpu_budget // 2))
                               if external or review else args.selfplay_threads)
                    ensure_selfplay(args, state, threads)
                    state["phase"] = ("search tests and self-play" if testing else "milestone matches and self-play"
                                      if external else "self-play" if ready else "disk pause")
                    state["positions"] = n
                    state["fresh_positions"] = fresh
                    if finish_search:
                        state["training_deferred_for"] = queue["pending"][0]["name"]
                    save_json(state_path, state)
                    log(f"{state['phase']}: {n:,} positions, {fresh:,} new of {args.min_new:,} needed")
                    if finish_search:
                        log(f"training ready; finishing existing search test {state['training_deferred_for']} first")
                    if testing:
                        search_batch(args, state, logfile)
                    analysed = (review and diagnostics.advance(
                        args, state, gate_workers(args, state), logfile, log))
                    if external and not analysed:
                        state["activity"] = "external milestone matches"
                        save_json(state_path, state)
                        milestones.advance(args, state, gate_workers(args, state), logfile, log)
                        state.pop("activity", None)
                        save_json(state_path, state)
                    if args.once:
                        return
                    if not testing and not external and not analysed:
                        time.sleep(args.poll_seconds)
                    continue

                stop_selfplay(args, state)
                state["phase"] = "training"
                state["attempts"] += 1
                save_json(state_path, state)
                hidden = args.train_hidden if args.train_hidden is not None else hidden_for(n)
                shape = f" ({args.input_buckets} king buckets, {args.output_buckets} output buckets)" if args.input_buckets * args.output_buckets > 1 else ""
                change = f"NNUE network{shape}, {hidden} hidden units, trained on {n / 1e6:.1f}M self-play positions"
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
                layout = f"-{args.input_buckets}x{args.output_buckets}" if args.input_buckets * args.output_buckets > 1 else ""
                candidate = os.path.join(args.data, "nets", f"cand-{stamp}-h{hidden}{layout}.nnue")
                os.makedirs(os.path.dirname(candidate), exist_ok=True)
                manifest = candidate + ".data.json"
                save_json(manifest, snapshot)
                log(f"round {state['attempts']}: training h{hidden} on {n:,} positions -> {candidate}")
                epochs = max(2, min(15, args.samples // max(n, 1)))
                rc = run([sys.executable, os.path.join(ROOT, "trainer", "train.py"),
                          "--data", os.path.join(args.data, "selfplay"), "--manifest", manifest,
                          "--hidden", str(hidden), "--epochs", str(epochs), "--input-buckets", str(args.input_buckets),
                          "--output-buckets", str(args.output_buckets), "--workers", str(args.train_workers),
                          "--threads", str(args.train_threads), "--seed", str(state["attempts"]), "--out", candidate], logfile)
                if rc != 0 or not os.path.exists(candidate):
                    raise RuntimeError("training failed; fresh-data progress retained for retry")
                state["trained_on"] = n
                state["sizes"] = snapshot
                state["pending"] = {"candidate": candidate, "change": change}
                save_json(state_path, state)
            except Exception as e:
                state["phase"] = "retry"
                state["error"] = str(e)
                save_json(state_path, state)
                log(f"round will retry: {e}")
                if args.once:
                    raise
                time.sleep(args.poll_seconds)
    finally:
        stop_selfplay(args, state)
        state["phase"] = "stopped"
        state.pop("controller_pid", None)
        state["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_json(state_path, state)


if __name__ == "__main__":
    main()
