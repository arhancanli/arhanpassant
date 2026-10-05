"""Bounded Stockfish loss reviews in the controller's existing match slot."""

import datetime as dt
import pathlib
import sys
import time

import local_gate
import milestones


SCRIPT = pathlib.Path(__file__).with_name("blunders.py")


def choose(data, sample):
    root = pathlib.Path(data) / "forge/milestones"
    for run in milestones.read(root / "index.json", {"runs": []})["runs"]:
        directory = root / run["identity"]
        manifest = milestones.read(directory / "manifest.json")
        report = milestones.read(directory / "report.json")
        if not report:
            continue
        expected = {(m["opponent"], m["tc"], m["target"]) for m in manifest["matches"]}
        actual = {(m["opponent"], m["tc"], m["target"]) for m in report["matches"]}
        if actual != expected or len(report["matches"]) != len(expected):
            continue
        quick_tc = manifest["config"]["plan"][0][0]
        quick = [m for m in report["matches"] if m["tc"] == quick_tc]
        coverage = len(quick) == len(manifest["config"]["opponents"]) and all(
            m["games"] >= min(32, m["target"]) and m.get("batches") for m in quick)
        complete = report["status"] == "complete" and all(
            m["games"] == m["target"] and m["status"] == "complete" for m in report["matches"])
        for stage, matches, ready in (("coverage", quick, coverage),
                                      ("complete", report["matches"], complete)):
            if not ready:
                continue
            reference = next(e for e in manifest["config"]["opponents"] if e["name"] == "stockfish-19")
            batches = [b for m in matches for b in (m["batches"][:1] if stage == "coverage" else m["batches"])]
            config = {"milestone": run["identity"], "stage": stage, "sample": sample,
                      "nodes": 300_000, "seed": 20261005, "script_sha256": local_gate.digest(SCRIPT),
                      "reference_sha256": reference["sha256"], "batches": batches}
            target = directory / "diagnostics" / f"{stage}-{milestones.fingerprint(config)[:16]}"
            previous = milestones.read(target / "job.json", {})
            if previous.get("status") == "complete" or previous.get("attempts", 0) >= 3:
                continue
            if previous.get("retry_after", 0) > time.time():
                continue
            return target, config, reference, previous
    return None


def advance(args, state, concurrency, logfile, log):
    sample = getattr(args, "loss_analysis_sample", 0)
    if not sample:
        return False
    job = choose(args.data, sample)
    if not job:
        return False
    if concurrency < 1:
        raise ValueError("loss analysis needs an available CPU core")
    target, config, reference, previous = job
    target.mkdir(parents=True, exist_ok=True)
    record = {"config": config, "attempts": previous.get("attempts", 0) + 1,
              "status": "running", "threads": min(concurrency, sample),
              "started": dt.datetime.now(dt.timezone.utc).isoformat()}
    state["activity"] = "Stockfish loss diagnostics"
    state["loss_diagnostics"] = {"job": str(target / "job.json"), "status": "running", "threads": record["threads"]}
    local_gate.save(str(pathlib.Path(args.data) / "forge/state.json"), state)
    local_gate.save(str(target / "job.json"), record)
    try:
        script, engine, games, out = (target / n for n in ("blunders.py", "stockfish", "games.jsonl", "report.json"))
        milestones.freeze(SCRIPT, script, config["script_sha256"])
        milestones.freeze(reference["path"], engine, config["reference_sha256"])
        contents, inputs = [], {}
        for entry in config["batches"]:
            batch_path = pathlib.Path(entry["file"])
            if local_gate.digest(batch_path) != entry["sha256"]:
                raise ValueError("committed diagnostic batch changed")
            batch = milestones.read(batch_path)
            local_gate.validate_batch(batch, batch["games"])
            source = batch_path.with_suffix(".games.jsonl")
            content = source.read_bytes()
            if local_gate.digest(source) != batch["records_sha256"]:
                raise ValueError("diagnostic game records changed")
            if len(content.splitlines()) != batch["games"]:
                raise ValueError("diagnostic game records are incomplete")
            inputs[str(source)] = batch["records_sha256"]
            contents.append(content)
        combined = b"".join(c if c.endswith(b"\n") else c + b"\n" for c in contents)
        if games.exists() and games.read_bytes() != combined:
            raise ValueError("frozen diagnostic inputs changed")
        games.write_bytes(combined)
        games_hash = local_gate.digest(games)
        record["input_sha256"] = inputs
        local_gate.save(str(target / "job.json"), record)
        cmd = ["nice", "-n", "19", sys.executable, str(script), str(games), "--stockfish", str(engine),
               "--nodes", str(config["nodes"]), "--sample", str(sample), "--threads", str(record["threads"]),
               "--seed", str(config["seed"]), "--out", str(out)]
        log(f"loss diagnostics {config['stage']}: up to {sample} losses on {record['threads']} match cores")
        # A complete report may precede its job checkpoint during a crash.
        # Reuse it only after validating its frozen inputs below.
        result = milestones.read(out) if out.exists() else None
        if result is None and local_gate.run(cmd, logfile) != 0:
            raise RuntimeError("Stockfish loss analysis failed")
        result = result or milestones.read(out)
        if (result.get("schema_version") != 2 or result.get("stockfish_nodes") != config["nodes"]
                or result.get("stockfish_sha256") != config["reference_sha256"]
                or result.get("input_sha256") != {str(games): games_hash}
                or result.get("losses_analysed") != min(sample, result.get("losses_available", -1))):
            raise ValueError("loss diagnostic report does not match its frozen job")
        for path, expected in {str(script): config["script_sha256"], str(engine): config["reference_sha256"],
                               str(games): games_hash, **inputs}.items():
            if local_gate.digest(path) != expected:
                raise ValueError("loss diagnostic input changed during analysis")
        record.update(status="complete", report=str(out), report_sha256=local_gate.digest(out))
        log(f"loss diagnostics complete: {result['losses_analysed']} losses, {result['moves_analysed']} own moves")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        # A diagnostic failure never rates games, promotes a build, or stalls
        # the strength/training queue. Retries are bounded and durable.
        record.update(status="failed", error=str(error), retry_after=time.time() + 300)
        log(f"loss diagnostics failed; continuing strength work: {error}")
    finally:
        record["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        local_gate.save(str(target / "job.json"), record)
        state["loss_diagnostics"].update(status=record["status"], report=record.get("report"))
        state.pop("activity", None)
        local_gate.save(str(pathlib.Path(args.data) / "forge/state.json"), state)
    return True
