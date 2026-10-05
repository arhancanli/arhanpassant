"""Build a frozen teacher corpus in restartable, bounded CPU slots.

Only completed chunks count. Interrupted chunks retain their evidence and are
retried without resampling. Run labeling inside the supervisor's reserved slot;
this command never stops other jobs, changes live data or promotes a network.
"""

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil

import chess
import paired
import teacher


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load(path):
    return json.loads(path.read_text())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def locked(out):
    with (out / "corpus.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prepare(data, state_path, engine, out, count=65536, nodes=100000,
            seed=20261101, chunk_size=512, floor_gib=8):
    data, state_path, engine, out = map(Path, [data, state_path, engine, out])
    engine = engine.resolve()
    paired.experimental_output(out)
    if min(count, nodes, chunk_size) < 1 or not 0 <= seed < 2**64:
        raise ValueError("invalid sampling or labeling budget")
    # Includes sampled provenance and both paired outputs, not only record bytes.
    paired.headroom(out.parent, count * 2048, floor_gib)
    state_bytes = state_path.read_bytes()
    items, sampling = teacher.fresh_sample(data, json.loads(state_bytes), count, seed)
    # Frozen copies can outlive a later training round's permitted retirement.
    # Re-read selected offsets before claiming their source bytes are captured.
    with contextlib.ExitStack() as stack:
        handles = {}
        for item in items:
            source = item["source"]
            if source not in handles:
                handles[source] = stack.enter_context(open(source, "rb"))
            handle = handles[source]
            handle.seek(item["record_index"] * teacher.REC)
            if handle.read(teacher.REC).hex() != item["raw_hex"]:
                raise ValueError("sampled source record changed before capture")
    paired.headroom(out.parent, count * 2048, floor_gib)
    out.mkdir(parents=True, exist_ok=False)
    (out / "chunks").mkdir()
    paired.save(out / "inputs.json", items)
    (out / "source-state.json").write_bytes(state_bytes)
    manifest = {"status": "prepared", "prepared": now(), "count": count,
                "nodes": nodes, "seed": seed, "chunk_size": chunk_size,
                "engine": str(engine), "engine_sha256": teacher.digest(engine),
                "reference_options": teacher.OPTIONS, "target_mode": "wdl",
                "labeler_sha256": teacher.digest(teacher.__file__),
                "pairing_sha256": teacher.digest(paired.__file__),
                "runner_sha256": teacher.digest(__file__), "python_chess": chess.__version__,
                "inputs_sha256": teacher.digest(out / "inputs.json"),
                "state_sha256": hashlib.sha256(state_bytes).hexdigest(), "sampling": sampling,
                "distinct_board_turn": len({paired.board_key(bytes.fromhex(r["raw_hex"])) for r in items}),
                "limitations": "Uniform eligible fresh records, distinct offsets; repeated board/turn groups may occur. Conditional on unambiguous irreversible moves. Earlier completed iterations can use fewer nodes than the requested ceiling. Teacher WDL is not measured ArhanPassant strength. No promotion."}
    paired.save(out / "manifest.json", manifest)
    return manifest


def checked_inputs(out):
    manifest = load(out / "manifest.json")
    expected = {out / "inputs.json": manifest["inputs_sha256"],
                out / "source-state.json": manifest["state_sha256"],
                Path(manifest["engine"]): manifest["engine_sha256"],
                Path(teacher.__file__): manifest["labeler_sha256"],
                Path(paired.__file__): manifest["pairing_sha256"],
                Path(__file__): manifest["runner_sha256"]}
    if manifest["reference_options"] != teacher.OPTIONS or manifest["python_chess"] != chess.__version__:
        raise ValueError("reference configuration changed")
    for path, digest in expected.items():
        if teacher.digest(path) != digest:
            raise ValueError(f"frozen corpus input changed: {path.name}")
    items = load(out / "inputs.json")
    if len(items) != manifest["count"] or [i["index"] for i in items] != list(range(len(items))):
        raise ValueError("invalid frozen input coverage")
    offsets = set()
    for item in items:
        raw = bytes.fromhex(item["raw_hex"])
        board, cp, result = teacher.decode(raw)
        if (hashlib.sha256(raw).hexdigest() != item["record_sha256"] or teacher.skip_reason(board)
                or board.fen() != item["fen"] or cp != item["original_white_cp"] or result != item["result"]):
            raise ValueError("invalid captured source record")
        offset = (str(Path(item["source"]).resolve()), item["record_index"])
        if offset in offsets:
            raise ValueError("duplicate sampled source offset")
        offsets.add(offset)
    return manifest, items


def checked_chunk(path, manifest, items):
    report = load(path / "report.json")
    if (report.get("status") != "complete" or report["manifest_identity"] != fingerprint(manifest)
            or report["input_identity"] != fingerprint(items)):
        raise ValueError("completed chunk identity changed")
    for name in ["baseline.bin", "teacher.bin"]:
        if teacher.digest(path / name) != report["artifacts_sha256"][name]:
            raise ValueError("completed chunk artifact changed")
    summary = report["summary"]
    indexed = {item["index"]: item for item in items}
    rows, excluded = summary["rows"], summary["excluded"]
    indices = [r["index"] for r in rows + excluded]
    if len(indices) != len(items) or set(indices) != set(indexed):
        raise ValueError("completed chunk coverage differs")
    old = list(paired.records(path / "baseline.bin"))
    new = list(paired.records(path / "teacher.bin"))
    if len(old) != len(new) or len(old) != len(rows) or summary["labelled"] != len(rows):
        raise ValueError("completed chunk paired counts differ")
    for original, labelled, row in zip(old, new, rows):
        item = indexed[row["index"]]
        if any(row.get(k) != v for k, v in item.items()):
            raise ValueError("completed chunk source provenance changed")
        if row["lowerbound"] or row["upperbound"]:
            raise ValueError("bounded score entered paired corpus")
        wdl = row["teacher_wdl_white"]
        if any(type(n) is not int or n < 0 for n in wdl) or len(wdl) != 3 or sum(wdl) != 1000:
            raise ValueError("invalid completed WDL counts")
        expected = (wdl[0] + 0.5 * wdl[1]) / 1000
        if expected != row["teacher_expected_score_white"]:
            raise ValueError("completed WDL orientation changed")
        if original.hex() != item["raw_hex"] or labelled != teacher.replace_score(original, teacher.expected_score_cp(expected)):
            raise ValueError("completed paired bytes changed")
        board = chess.Board(item["fen"])
        if not row["pv"] or row["move"] != row["pv"][0]:
            raise ValueError("completed chunk has no best-move PV")
        for uci in row["pv"]:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("illegal completed reference PV")
            board.push(move)
    for row in excluded:
        if not row["lowerbound"] and not row["upperbound"]:
            raise ValueError("unbounded completed row was excluded")
    return report


def label(path, manifest, items, workers):
    if path.exists():
        if path.is_symlink():
            raise ValueError("partial chunk must not be a symlink")
        path.rename(path.with_name(path.name + ".interrupted-" + str(dt.datetime.now().timestamp())))
    path.mkdir()
    rows = []
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        jobs = [pool.submit(teacher.label_chunk, Path(manifest["engine"]), items[i::workers], manifest["nodes"])
                for i in range(min(workers, len(items)))]
        for job in concurrent.futures.as_completed(jobs):
            rows.extend(job.result())
    # Never let engine-result metadata overwrite captured source fields.
    source_keys = set(items[0]) - {"index"}
    if any(source_keys & set(row) for row in rows):
        raise ValueError("reference result overwrote source provenance")
    summary = teacher.write_pairs(path, items, rows, "wdl")
    report = {"status": "complete", "finished": now(), "manifest_identity": fingerprint(manifest),
              "input_identity": fingerprint(items), "workers": workers, "summary": summary,
              "artifacts_sha256": {n: teacher.digest(path / n) for n in ["baseline.bin", "teacher.bin"]}}
    paired.save(path / "report.json", report)
    checked_chunk(path, manifest, items)


def finalize(out, manifest, chunks, floor_gib):
    target = out / "corpus"
    if target.exists():
        proof = load(target / "validation.json")
        if proof.get("status") != "complete" or proof["manifest_identity"] != fingerprint(manifest):
            raise ValueError("final corpus identity differs")
        for name, expected in proof["artifacts_sha256"].items():
            if name not in {"baseline.bin", "teacher.bin", "rows.jsonl", "excluded.jsonl", "report.json"}:
                raise ValueError("unexpected final corpus artifact")
            if teacher.digest(target / name) != expected:
                raise ValueError("final corpus artifact changed")
        return load(target / "report.json")
    paired.headroom(out, manifest["count"] * 2048, floor_gib)
    staging = out / "corpus.partial"
    if staging.exists():
        if staging.is_symlink():
            raise ValueError("partial corpus must not be a symlink")
        staging.rename(out / ("corpus.interrupted-" + str(dt.datetime.now().timestamp())))
    staging.mkdir()
    errors, groups = [], set()
    counts = {"labelled": 0, "excluded_bounds": 0, "earlier_completed_iteration": 0,
              "teacher_quiet_best": 0, "teacher_mates": 0}
    with contextlib.ExitStack() as stack:
        files = {name: stack.enter_context((staging / name).open("xb"))
                 for name in ["baseline.bin", "teacher.bin"]}
        rows_file = stack.enter_context((staging / "rows.jsonl").open("x"))
        excluded_file = stack.enter_context((staging / "excluded.jsonl").open("x"))
        for path, report in chunks:
            for name, file in files.items():
                with (path / name).open("rb") as source:
                    shutil.copyfileobj(source, file)
            for key in counts:
                counts[key] += report["summary"][key]
            for row in report["summary"]["rows"]:
                rows_file.write(json.dumps(row, separators=(",", ":")) + "\n")
                errors.append(abs(row["probability_delta_white"]))
                groups.add(paired.board_key(bytes.fromhex(row["raw_hex"])))
            for row in report["summary"]["excluded"]:
                excluded_file.write(json.dumps(row, separators=(",", ":")) + "\n")
    errors.sort()
    if counts["labelled"] + counts["excluded_bounds"] != manifest["count"] or not errors:
        raise ValueError("final corpus coverage incomplete")
    summary = {**counts, "target_mode": "wdl", "distinct_board_turn": len(groups),
               "mean_abs_probability_delta": sum(errors) / len(errors),
               "p95_abs_probability_delta": errors[(len(errors) - 1) * 95 // 100],
               "delta_at_least_0_1": sum(x >= 0.1 for x in errors),
               "delta_at_least_0_2": sum(x >= 0.2 for x in errors)}
    report = {"status": "complete", "finished": now(), "manifest_identity": fingerprint(manifest),
              "manifest": str((out / "manifest.json").resolve()), "summary": summary,
              "limitations": manifest["limitations"], "chunks": [
                  {"path": str(p.resolve()), "report_sha256": teacher.digest(p / "report.json")} for p, _ in chunks]}
    paired.save(staging / "report.json", report)
    proof = {"status": "complete", "manifest_identity": fingerprint(manifest),
             "corpus_report": str((target / "report.json").resolve()),
             "checks": "Captured source hashes, eligibility, complete disjoint offset coverage, identical non-score bytes, WDL target encoding and legal completed reference PVs; no strength claim.",
             "artifacts_sha256": {p.name: teacher.digest(p) for p in staging.iterdir() if p.is_file()}}
    paired.save(staging / "validation.json", proof)
    staging.rename(target)
    return report


def advance(out, workers=18, cpu_budget=18, max_chunks=1, floor_gib=8):
    out = Path(out)
    paired.experimental_output(out)
    if min(workers, cpu_budget, max_chunks) < 1 or workers > cpu_budget or cpu_budget > (os.cpu_count() or 1):
        raise ValueError("invalid reserved CPU or chunk budget")
    with locked(out):
        manifest, items = checked_inputs(out)
        ledger = {}
        if (out / "progress.json").exists():
            progress = load(out / "progress.json")
            if progress["manifest_identity"] != fingerprint(manifest):
                raise ValueError("progress belongs to another corpus")
            ledger = {entry["path"]: entry["report_sha256"] for entry in progress["chunks"]}
        completed, added = [], 0
        for start in range(0, len(items), manifest["chunk_size"]):
            chunk = items[start:start + manifest["chunk_size"]]
            path = out / "chunks" / f"{start:08d}"
            if path.exists():
                if path.is_symlink():
                    raise ValueError("completed chunk must not be a symlink")
                if path.name in ledger and teacher.digest(path / "report.json") != ledger[path.name]:
                    raise ValueError("completed chunk report changed")
                report = checked_chunk(path, manifest, chunk)
            elif added < max_chunks:
                paired.headroom(out, len(chunk) * 2048, floor_gib)
                partial = path.with_name(path.name + ".partial")
                label(partial, manifest, chunk, workers)
                if teacher.digest(manifest["engine"]) != manifest["engine_sha256"]:
                    raise ValueError("reference changed during labeling")
                partial.rename(path)
                report = checked_chunk(path, manifest, chunk)
                added += 1
            else:
                break
            completed.append((path, report))
            paired.save(out / "progress.json", {"updated": now(), "manifest_identity": fingerprint(manifest),
                        "completed_records": sum(r["summary"]["labelled"] + r["summary"]["excluded_bounds"] for _, r in completed),
                        "target_records": len(items), "chunks": [
                            {"path": p.name, "report_sha256": teacher.digest(p / "report.json")} for p, _ in completed]})
        if len(completed) == (len(items) + manifest["chunk_size"] - 1) // manifest["chunk_size"]:
            return finalize(out, manifest, completed, floor_gib)
        return {"status": "pending", "completed_records": sum(
            r["summary"]["labelled"] + r["summary"]["excluded_bounds"] for _, r in completed),
                "target_records": len(items), "added_chunks": added}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ["data", "state", "engine", "out"]:
        prep.add_argument("--" + name, type=Path, required=True)
    for name, default in [("samples", 65536), ("nodes", 100000), ("seed", 20261101), ("chunk-size", 512)]:
        prep.add_argument("--" + name, type=int, default=default)
    run = sub.add_parser("advance")
    run.add_argument("--out", type=Path, required=True)
    for name, default in [("workers", 18), ("cpu-budget", 18), ("max-chunks", 1)]:
        run.add_argument("--" + name, type=int, default=default)
    for command in [prep, run]:
        command.add_argument("--min-free-gb", type=float, default=8)
    args = parser.parse_args()
    if args.command == "prepare":
        report = prepare(args.data, args.state, args.engine, args.out, args.samples, args.nodes,
                         args.seed, args.chunk_size, args.min_free_gb)
        print(json.dumps({k: report[k] for k in ["status", "count", "distinct_board_turn"]}))
    else:
        report = advance(args.out, args.workers, args.cpu_budget, args.max_chunks, args.min_free_gb)
        print(json.dumps({k: v for k, v in report.items() if k in ["status", "completed_records", "target_records", "added_chunks", "summary"]}))


if __name__ == "__main__":
    main()
