"""Persistent external matches after each champion/build/accepted-settings change.

Fixed game budgets give opponent coverage rather than an SPRT promotion rule.
Snapshots survive later promotions. The single forge controller alternates
these batches with search gates and accounts for their cores with self-play.
"""

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import shutil

import local_gate
import opponents


PLAN = [("10+0.1", 200), ("60+0.6", 100)]
BATCH_GAMES = 32
MAX_BATCH_ATTEMPTS = 3


class RetryLimit(RuntimeError):
    """An external batch needs investigation before any more games are run."""


def archive_attempt(batch_out, games_out, journal_path, journal, error):
    """Keep rejected or interrupted outputs before their paths can be reused."""
    attempt = journal["attempts"][-1]
    archive = batch_out.parent / (batch_out.stem + "-attempts") / f"{len(journal['attempts']):04d}"
    archive.mkdir(parents=True, exist_ok=True)
    files = []
    for source in (batch_out, games_out, pathlib.Path(str(batch_out) + ".tmp")):
        if source.exists():
            digest = local_gate.digest(source)
            target = archive / source.name
            freeze(source, target, digest)
            files.append({"file": str(target), "sha256": digest, "bytes": source.stat().st_size})
    attempt.update(status="failed", error=str(error), files=files,
                   finished=dt.datetime.now(dt.timezone.utc).isoformat())
    local_gate.save(str(archive / "attempt.json"), attempt)
    local_gate.save(str(journal_path), journal)


def exhausted(directory, match):
    """Return the failed next batch's journal, without changing game counts."""
    out = directory / match["file"]
    result = read(out, {"batches": []})
    journal_path = directory / (out.stem + "-batches") / f"{len(result['batches']):04d}.attempts.json"
    journal = read(journal_path)
    identity = {"milestone_match": fingerprint({"milestone": directory.name, "match": match}),
                "seed": 20261005 + len(result["batches"]) + (1000000 if match["tc"] == "60+0.6" else 0),
                "games": min(BATCH_GAMES, match["target"] - result.get("games", 0)),
                "max_attempts": MAX_BATCH_ATTEMPTS}
    if journal and any(journal.get(k) != v for k, v in identity.items()):
        raise ValueError("milestone attempt journal does not match this batch")
    if journal and len(journal["attempts"]) >= MAX_BATCH_ATTEMPTS and journal["attempts"][-1]["status"] == "failed":
        return {"opponent": match["opponent"], "tc": match["tc"], "journal": str(journal_path),
                "attempts": len(journal["attempts"]), "error": journal["attempts"][-1]["error"]}
    return None


def read(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return default


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def freeze(source, target, expected):
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, temporary)
        if local_gate.digest(temporary) != expected:
            raise RuntimeError(f"milestone input changed during snapshot: {source}")
        os.replace(temporary, target)
    if local_gate.digest(target) != expected:
        raise RuntimeError(f"milestone snapshot changed: {target}")


def enqueue(args, state, accepted):
    """Capture each changed identity once, keeping unfinished earlier suites."""
    catalog = read(args.milestone_catalog)
    entries = catalog["opponents"]
    if sorted(e["name"] for e in entries) != sorted(opponents.NAMES):
        raise ValueError("milestone catalog must contain every benchmark opponent exactly once")
    if any(str(v) != "1" for k, v in accepted.items() if k.lower() == "threads"):
        raise ValueError("milestone matches require one thread on each side")
    config = {"engine_sha256": local_gate.digest(args.engine),
              "network_sha256": local_gate.digest(state["champion_net"]),
              "arena_sha256": local_gate.digest(args.arena), "book_sha256": local_gate.digest(args.book),
              "options": {**accepted, "Threads": "1", "Hash": "64"},
              "opponents": entries, "plan": PLAN, "batch_games": BATCH_GAMES,
              "execution_profile": os.environ.get("ARHANPASSANT_EXECUTION_PROFILE", "manual")}
    identity = fingerprint(config)[:24]
    root = pathlib.Path(args.data) / "forge/milestones"
    directory = root / identity
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if not manifest.exists():
        paths = {}
        for key, source in (("engine", args.engine), ("network", state["champion_net"]),
                            ("arena", args.arena), ("book", args.book)):
            target = directory / {"engine": "arhanpassant", "network": "champion.nnue",
                                  "arena": "arena", "book": "openings.epd"}[key]
            freeze(source, target, config[key + "_sha256"])
            paths[key] = str(target)
        matches = [{"opponent": e["name"], "tc": tc, "target": games,
                    "file": f"{e['name']}-{tc}.json"} for tc, games in PLAN for e in entries]
        local_gate.save(str(manifest), {"identity": identity, "config": config, "paths": paths,
                                      "version": state["champion_version"], "matches": matches,
                                      "created": dt.datetime.now(dt.timezone.utc).isoformat()})
    index_path = root / "index.json"
    index = read(index_path, {"runs": []})
    if not any(r["identity"] == identity for r in index["runs"]):
        index["runs"].append({"identity": identity, "version": state["champion_version"],
                              "status": "queued", "cursor": 0, "games": 0, "target_games": sum(g for _, g in PLAN) * len(entries)})
        local_gate.save(str(index_path), index)
    return index


def pending(data):
    index = read(pathlib.Path(data) / "forge/milestones/index.json", {"runs": []})
    return next((r for r in index["runs"] if r["status"] not in ("complete", "needs_attention")), None)


def summary(result):
    n = result["games"]
    result["score"] = (result["wins"] + result["draws"] / 2) / n if n else None
    # An independent unit is a colour-reversed opening pair, whose score is
    # bounded by [0,1]. Hoeffding remains conservative at zero/all wins and
    # does not assume the two colours are independent Bernoulli games.
    radius = math.sqrt(math.log(40) / (2 * (n / 2))) if n else 1
    result["score_ci95"] = [max(0, result["score"] - radius), min(1, result["score"] + radius)] if n else [0, 1]
    result["interval_method"] = "Hoeffding bound on independent opening-pair scores"
    result["status"] = "complete" if n == result["target"] else "running"


def advance_match(manifest, match, directory, concurrency, logfile):
    config, paths = manifest["config"], manifest["paths"]
    opponent = next(e for e in config["opponents"] if e["name"] == match["opponent"])
    expected = {paths[k]: config[k + "_sha256"] for k in paths}
    expected[opponent["path"]] = opponent["sha256"]

    def unchanged():
        for path, digest in expected.items():
            if local_gate.digest(path) != digest:
                raise RuntimeError(f"milestone input changed: {path}; saved games retained")

    unchanged()
    out = directory / match["file"]
    result = read(out)
    match_id = fingerprint({"milestone": manifest["identity"], "match": match})
    if result is None:
        result = {"identity": match_id, "opponent": match["opponent"], "tc": match["tc"],
                  "target": match["target"], "games": 0, "wins": 0, "losses": 0, "draws": 0,
                  "seconds": 0, "penta": [0] * 5, "reasons": {}, "batches": []}
    if result["identity"] != match_id:
        raise ValueError("milestone checkpoint does not match its frozen manifest")
    if result["games"] >= result["target"]:
        return result
    count = min(BATCH_GAMES, result["target"] - result["games"])
    if count < 2 or count % 2 or concurrency < 1:
        raise ValueError("milestone batch needs complete pairs and an available CPU core")
    number = len(result["batches"])
    # Same openings and seeds across opponents, repeatable across milestones.
    seed = 20261005 + number + (1000000 if match["tc"] == "60+0.6" else 0)
    batches = directory / (out.stem + "-batches")
    batches.mkdir(exist_ok=True)
    batch_out, games_out = batches / f"{number:04d}.json", batches / f"{number:04d}.games.jsonl"
    try:
        batch = read(batch_out)
    except json.JSONDecodeError:
        batch = None
    recoverable = (batch is not None and batch.get("milestone_match") == match_id and batch.get("seed") == seed)
    if not recoverable:
        cmd = [paths["arena"], "--engine", "name=ArhanPassant", f"cmd={paths['engine']}",
               f"opt.EvalFile={paths['network']}", *[f"opt.{k}={v}" for k, v in config["options"].items()],
               "--engine", f"name={opponent['name']}", f"cmd={opponent['path']}",
               *[f"opt.{k}={v}" for k, v in opponent["options"].items()],
               "--tc", match["tc"], "--book", paths["book"], "--concurrency", str(concurrency),
               "--games", str(count), "--seed", str(seed), "--nice", "10", "--quiet",
               "--out", str(batch_out), "--games-out", str(games_out)]
        journal_path = batches / f"{number:04d}.attempts.json"
        identity = {"milestone_match": match_id, "seed": seed, "games": count,
                    "max_attempts": MAX_BATCH_ATTEMPTS}
        journal = read(journal_path, {**identity, "attempts": []})
        if any(journal.get(k) != v for k, v in identity.items()):
            raise ValueError("milestone attempt journal does not match this batch")
        if journal["attempts"] and journal["attempts"][-1]["status"] == "running":
            archive_attempt(batch_out, games_out, journal_path, journal, "interrupted unsealed arena attempt")
        elif not journal["attempts"] and any(p.exists() for p in (batch_out, games_out)):
            # Older controllers left no launch journal. Preserve their outputs
            # as one unknown attempt; do not silently attribute them to this run.
            journal["attempts"].append({"status": "running", "command": None, "legacy": True})
            archive_attempt(batch_out, games_out, journal_path, journal, "unsealed legacy arena outputs")
        if len(journal["attempts"]) >= MAX_BATCH_ATTEMPTS:
            raise RetryLimit(f"external batch exhausted {MAX_BATCH_ATTEMPTS} attempts; inspect {journal_path}")
        journal["attempts"].append({"status": "running", "command": cmd,
                                    "started": dt.datetime.now(dt.timezone.utc).isoformat()})
        local_gate.save(str(journal_path), journal)
        # The journal and any previous failure archive are durable first.
        for stale in (batch_out, games_out, pathlib.Path(str(batch_out) + ".tmp")):
            stale.unlink(missing_ok=True)
        try:
            if local_gate.run(cmd, logfile) != 0:
                raise RuntimeError("external arena failed; completed batches retained")
            batch = read(batch_out)
            unchanged()
            local_gate.validate_batch(batch, count)
            if batch.get("seed") != seed:
                raise ValueError("unexpected milestone batch seed")
            records = [json.loads(line) for line in games_out.read_text().splitlines()]
            if len(records) != count:
                raise ValueError("milestone game record count differs from completed games")
            batch.update(milestone_match=match_id, concurrency=concurrency,
                         records_sha256=local_gate.digest(games_out))
            local_gate.save(str(batch_out), batch)
        except Exception as error:
            archive_attempt(batch_out, games_out, journal_path, journal, error)
            raise
        journal["attempts"][-1].update(status="passed", finished=dt.datetime.now(dt.timezone.utc).isoformat())
        local_gate.save(str(journal_path), journal)
    unchanged()
    local_gate.validate_batch(batch, count)
    if local_gate.digest(games_out) != batch["records_sha256"]:
        raise ValueError("milestone game records changed; cannot recover this batch")
    for key in ("games", "wins", "losses", "draws", "seconds"):
        result[key] += batch[key]
    result["penta"] = [a + b for a, b in zip(result["penta"], batch["penta"])]
    for reason, n in batch["reasons"].items():
        result["reasons"][reason] = result["reasons"].get(reason, 0) + n
    result["batches"].append({"file": str(batch_out), "sha256": local_gate.digest(batch_out),
                              "seed": seed, "concurrency": batch["concurrency"]})
    summary(result)
    local_gate.save(str(out), result)
    return result


def advance(args, state, concurrency, logfile, log):
    root = pathlib.Path(args.data) / "forge/milestones"
    index_path = root / "index.json"
    index = read(index_path, {"runs": []})
    run = next((r for r in index["runs"] if r["status"] not in ("complete", "needs_attention")), None)
    if run is None:
        return
    directory = root / run["identity"]
    manifest = read(directory / "manifest.json")
    # Round-robin coverage across the entire suite at the quick time control
    # first, then repeat at the longer time control.
    available = [i for i, m in enumerate(manifest["matches"])
                 if read(directory / m["file"], {}).get("games", 0) < m["target"]]
    blocked = [failure for i in available if (failure := exhausted(directory, manifest["matches"][i]))]
    available = [i for i in available if not exhausted(directory, manifest["matches"][i])]
    if available:
        first_stage = [i for i in available if manifest["matches"][i]["tc"] == PLAN[0][0]]
        choices = first_stage or available
        cursor = run.get("cursor", 0)
        chosen = next((i for i in choices if i >= cursor), choices[0])
        match = manifest["matches"][chosen]
        result = advance_match(manifest, match, directory, concurrency, logfile)
        run["cursor"] = chosen + 1
        log(f"milestone {run['version']} vs {match['opponent']} ({match['tc']}): "
            f"{result['games']}/{result['target']} games, score {result['score']:.1%}")
    results = [read(directory / m["file"]) for m in manifest["matches"]]
    run["games"] = sum(r["games"] for r in results if r)
    run["blocked_matches"] = blocked
    active_remaining = any(i in available and (not r or r["games"] < m["target"])
                           for i, (m, r) in enumerate(zip(manifest["matches"], results)))
    run["status"] = ("complete" if all(r and r["status"] == "complete" for r in results) else
                     "running" if active_remaining else "needs_attention")
    if run["status"] == "needs_attention":
        log(f"milestone {run['version']} needs investigation: {len(blocked)} batches exhausted their retry budget")
    run["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    local_gate.save(str(index_path), index)
    report = {"identity": run["identity"], "version": run["version"], "status": run["status"],
              "games": run["games"], "target_games": run["target_games"], "updated": run["updated"],
              "blocked_matches": blocked,
              "note": "Full-strength local matches. No absolute CCRL rating or promotion is inferred from this suite.",
              "matches": [{**m, **(r or {"games": 0, "status": "queued"})} for m, r in zip(manifest["matches"], results)]}
    local_gate.save(str(directory / "report.json"), report)
    state["external_milestone"] = {k: run[k] for k in ("identity", "version", "games", "target_games", "status")}
    state["external_milestone"]["blocked_matches"] = blocked
