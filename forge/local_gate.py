"""Checkpointed local game-pair gates, using the same SPRT as the fleet.

Each batch is durable before the next starts. A restarted controller reuses
completed batches only when the builds, networks, options, book and bounds
still match. Both sides play on the same machine with colours swapped.
"""

import hashlib
import json
import os
import signal
import subprocess
import time

import sprt


def save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, logfile):
    """Own the entire child process group, including arena's engine children."""
    with open(logfile, "a") as f:
        f.write(f"$ {' '.join(cmd)}\n")
        f.flush()
        child = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return child.wait()
        finally:
            # This also runs when the controller receives SIGTERM.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if child.poll() is None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()


def validate_batch(batch, expected_games):
    penta = batch.get("penta", [])
    if len(penta) != 5 or any(not isinstance(n, int) or n < 0 for n in penta):
        raise ValueError("invalid pentanomial game counts")
    if batch.get("games") != expected_games or sum(penta) * 2 != expected_games:
        raise ValueError("incomplete arena batch; refusing to rate or promote it")
    if sum(batch.get(k, 0) for k in ("wins", "losses", "draws")) != expected_games:
        raise ValueError("arena outcome counts do not match its game count")
    if sum(i * n for i, n in enumerate(penta)) != 2 * batch["wins"] + batch["draws"]:
        raise ValueError("arena outcomes do not match its game-pair counts")
    for reason, count in batch.get("reasons", {}).items():
        if count and any(word in reason.lower() for word in ("engine", "illegal", "timeout", "time forfeit", "loses on time")):
            raise ValueError(f"unreliable arena batch: {count} games ended with {reason}")


def summarize(result):
    config = result["config"]
    elo0, elo1 = config["bounds"]
    result["elo"], result["elo_lo"], result["elo_hi"] = sprt.elo_estimate(result["penta"])
    lo, hi = sprt.bounds(0.05, 0.05)
    llr = sprt.llr(result["penta"], elo0, elo1)
    result["sprt"] = {"elo0": elo0, "elo1": elo1, "alpha": 0.05, "beta": 0.05,
                      "llr": llr, "lower": lo, "upper": hi}
    result["decision"] = ("H1" if llr >= hi else "H0" if llr <= lo else
                          "inconclusive" if result["games"] >= config["max_games"] else "running")


def gate(*, engine, arena, candidate, champion, book, tc, concurrency, max_games,
         elo0, elo1, cand_opts, champ_opts, out, logfile, log, batch_games=64,
         max_batches=None, baseline_engine=None):
    baseline_engine = baseline_engine or engine
    config = {
        "engine": os.path.realpath(engine), "engine_sha256": digest(engine),
        "baseline_engine": os.path.realpath(baseline_engine), "baseline_sha256": digest(baseline_engine),
        "arena_sha256": digest(arena), "candidate": candidate, "candidate_sha256": digest(candidate) if candidate else None,
        "champion": champion, "champion_sha256": digest(champion) if champion else None,
        "book_sha256": digest(book), "tc": tc, "concurrency": concurrency,
        "max_games": max_games, "bounds": [elo0, elo1], "candidate_options": cand_opts,
        "champion_options": champ_opts, "batch_games": batch_games,
        "execution_profile": os.environ.get("ARHANPASSANT_EXECUTION_PROFILE", "manual"),
    }
    try:
        with open(out) as f:
            result = json.load(f)
        if result.get("config") != config:
            os.replace(out, out + f".superseded-{time.time_ns()}")
            result = None
    except FileNotFoundError:
        result = None
    if result is None:
        result = {"config": config, "seed": int.from_bytes(os.urandom(4), "little"), "batches": 0,
                  "engine": "candidate", "baseline": "champion", "tc": tc,
                  "games": 0, "wins": 0, "losses": 0, "draws": 0, "penta": [0] * 5,
                  "seconds": 0, "reasons": {}, "decision": "running"}
        save(out, result)
    if result["decision"] != "running":
        return result
    completed = 0
    while result["games"] < max_games and (max_batches is None or completed < max_batches):
        count = min(batch_games, max_games - result["games"])
        if count < 2 or count % 2:
            raise ValueError("gate budgets must contain complete game pairs")
        batch_out = out + ".batch.json"
        seed = result["seed"] + result["batches"]
        cmd = [arena, "--engine", "name=candidate", f"cmd={engine}",
               f"opt.EvalFile={candidate or '<none>'}", *[f"opt.{k}={v}" for k, v in cand_opts.items()],
               "--engine", "name=champion", f"cmd={baseline_engine}",
               f"opt.EvalFile={champion or '<none>'}", *[f"opt.{k}={v}" for k, v in champ_opts.items()],
               "--tc", tc, "--book", book, "--concurrency", str(concurrency), "--games", str(count),
               "--seed", str(seed), "--nice", "10", "--quiet", "--out", batch_out]
        # A complete batch left by an interrupted controller is recoverable.
        # Validate its seed before using it to prevent double counting.
        batch = None
        if os.path.exists(batch_out):
            try:
                with open(batch_out) as f:
                    old = json.load(f)
                if old.get("gate_config") == config and old.get("seed") == seed:
                    batch = old
            except json.JSONDecodeError:
                pass
        if batch is None:
            if os.path.exists(batch_out):
                os.remove(batch_out)
            if run(cmd, logfile) != 0:
                raise RuntimeError("local arena failed; completed batches are saved")
            with open(batch_out) as f:
                batch = json.load(f)
            validate_batch(batch, count)
            batch["gate_config"] = config
            save(batch_out, batch)
        validate_batch(batch, count)
        for k in ("games", "wins", "losses", "draws", "seconds"):
            result[k] += batch[k]
        result["penta"] = [a + b for a, b in zip(result["penta"], batch["penta"])]
        for reason, n in batch["reasons"].items():
            result["reasons"][reason] = result["reasons"].get(reason, 0) + n
        result["batches"] += 1
        summarize(result)
        save(out, result)
        os.remove(batch_out)
        completed += 1
        log(f"local gate: {result['games']} games, elo {result['elo']:+.1f} "
            f"[{result['elo_lo']:+.1f}, {result['elo_hi']:+.1f}], "
            f"llr {result['sprt']['llr']:.2f}, {result['decision']}")
        if result["decision"] != "running":
            break
    return result
