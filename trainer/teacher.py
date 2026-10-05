"""Create an experimental paired corpus with independent Stockfish scores.

The legacy 32-byte records omit castling, en-passant and repetition history.
Only irreversible-move positions with unambiguous remaining state are used.
Scores replace bytes 24..25 only; game outcomes and all other bytes survive.
This tool never writes into the live self-play dataset or promotes a model.
Run it within a reserved CPU slot: each worker owns one single-thread engine.
"""

import argparse
import bisect
import collections
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct

import chess
import chess.engine

REC = 32
OPTIONS = {"Threads": 1, "Hash": 64,
           "UCI_LimitStrength": False, "Skill Level": 20, "UCI_ShowWDL": True,
           "SyzygyPath": "", "SyzygyProbeLimit": 0}


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def decode(record):
    if len(record) != REC or record[26] > 2 or record[27] > 1 or record[31]:
        raise ValueError("invalid legacy record metadata")
    occupancy = int.from_bytes(record[:8], "little")
    if chess.popcount(occupancy) > 32:
        raise ValueError("too many occupied squares")
    board = chess.Board(None)
    for i, square in enumerate(chess.scan_forward(occupancy)):
        code = (record[8 + i // 2] >> (4 * (i % 2))) & 15
        if (code & 7) > 5:
            raise ValueError("invalid piece code")
        board.set_piece_at(square, chess.Piece((code & 7) + 1, not bool(code & 8)))
    board.turn = record[27] == 0
    board.halfmove_clock = record[28]
    board.fullmove_number = int.from_bytes(record[29:31], "little")
    if board.fullmove_number < 1 or not board.is_valid():
        raise ValueError("invalid recorded position")
    return board, struct.unpack_from("<h", record, 24)[0], record[26]


def skip_reason(board):
    # An irreversible last move prevents any earlier repetition from being
    # relevant. Preserve its recorded clock rather than inventing a history.
    if board.halfmove_clock:
        return "repetition history unavailable"
    for color, king, rooks in [(chess.WHITE, chess.E1, (chess.A1, chess.H1)),
                               (chess.BLACK, chess.E8, (chess.A8, chess.H8))]:
        if board.king(color) == king and any(
                board.piece_at(sq) == chess.Piece(chess.ROOK, color) for sq in rooks):
            return "castling rights unavailable"
    # A pawn beside an enemy pawn on the EP capture rank might have an EP
    # right. The legacy record cannot distinguish a double push from a capture.
    rank = 4 if board.turn == chess.WHITE else 3
    for square in board.pieces(chess.PAWN, board.turn):
        if chess.square_rank(square) != rank:
            continue
        file = chess.square_file(square)
        for adjacent in [file - 1, file + 1]:
            if 0 <= adjacent < 8 and board.piece_at(chess.square(adjacent, rank)) == chess.Piece(chess.PAWN, not board.turn):
                direction = 1 if board.turn == chess.WHITE else -1
                target = chess.square(adjacent, rank + direction)
                origin = chess.square(adjacent, rank + 2 * direction)
                if board.piece_at(target) is None and board.piece_at(origin) is None:
                    possible = board.copy()
                    possible.ep_square = target
                    if possible.has_legal_en_passant():
                        return "en-passant state unavailable"
    if board.is_check():
        return "in check"
    if board.is_game_over(claim_draw=False):
        return "terminal position"
    return None


def replace_score(record, white_cp):
    score = max(-32000, min(32000, int(white_cp)))
    return record[:24] + struct.pack("<h", score) + record[26:]


def fresh_sample(data, state, count, seed):
    data = Path(data).resolve()
    sizes = {}
    for name, length in state.get("sizes", {}).items():
        canonical = str(Path(name).resolve())
        sizes[canonical] = max(sizes.get(canonical, 0), length)
    sources, endpoints, total = [], [], 0
    for path in sorted((data / "selfplay").glob("**/*.bin")):
        start = sizes.get(str(path), 0) // REC
        end = path.stat().st_size // REC
        if end > start:
            sources.append({"path": str(path), "first_record": start, "end_record": end})
            total += end - start
            endpoints.append(total)
    if total < count:
        raise ValueError("not enough fresh records")
    rng = random.Random(seed)
    # Reject already accepted offsets rather than allocating count * 100
    # candidate indices up front. Every unseen eligible record still has the
    # same chance on each draw, so accepted records are uniform without
    # replacement. Memory grows with the returned corpus, not the draw budget.
    accepted_offsets = set()
    selected, skipped = [], collections.Counter()
    with contextlib.ExitStack() as stack:
        handles = {}
        for _ in range(count * 100):
            offset = rng.randrange(total)
            if offset in accepted_offsets:
                skipped["duplicate accepted offset"] += 1
                continue
            source_index = bisect.bisect_right(endpoints, offset)
            source = sources[source_index]
            preceding = endpoints[source_index - 1] if source_index else 0
            index = source["first_record"] + offset - preceding
            path = source["path"]
            if path not in handles:
                handles[path] = stack.enter_context(open(path, "rb"))
            handles[path].seek(index * REC)
            raw = handles[path].read(REC)
            board, score, result = decode(raw)
            reason = skip_reason(board)
            if reason:
                skipped[reason] += 1
                continue
            accepted_offsets.add(offset)
            selected.append({"index": len(selected), "source": path, "record_index": index,
                             "raw_hex": raw.hex(), "record_sha256": hashlib.sha256(raw).hexdigest(),
                             "fen": board.fen(), "original_white_cp": score, "result": result})
            if len(selected) == count:
                break
    if len(selected) != count:
        raise ValueError(f"only {len(selected)}/{count} unambiguous records in sampling budget")
    return selected, {"sources": sources, "available_fresh_records": total,
                      "selected": len(selected), "skipped": dict(skipped), "seed": seed}


def scored_row(board, info):
    if not info.get("pv") or "score" not in info:
        raise ValueError("reference returned no scored PV")
    check = board.copy()
    for move in info["pv"]:
        if move not in check.legal_moves:
            raise ValueError("illegal reference PV")
        check.push(move)
    score = info["score"].pov(chess.WHITE)
    white_cp = max(-32000, min(32000, score.score(mate_score=32000)))
    row = {"teacher_white_cp": white_cp, "teacher_mate_white": score.mate(),
            "move": info["pv"][0].uci(), "pv": [m.uci() for m in info["pv"]],
            "teacher_best_move_quiet": not board.is_capture(info["pv"][0]) and not info["pv"][0].promotion,
            "depth": info.get("depth"), "nodes": info.get("nodes"),
            "lowerbound": info.get("lowerbound", False), "upperbound": info.get("upperbound", False)}
    if "wdl" in info:
        wdl = info["wdl"].pov(chess.WHITE)
        counts = [wdl.wins, wdl.draws, wdl.losses]
        if any(n < 0 for n in counts) or sum(counts) != 1000:
            raise ValueError("invalid reference WDL counts")
        row["teacher_wdl_white"] = counts
        row["teacher_expected_score_white"] = (wdl.wins + 0.5 * wdl.draws) / 1000
    return row


def completed_result(board, stream):
    """Use the last completed exact iteration, retaining the final raw bound.

    A fixed node budget can interrupt an aspiration re-search. Its final bound
    is not a point target, while the preceding completed iteration still is.
    The chosen result retains its own depth, nodes, score and legal PV together.
    """
    last = exact = None
    for info in stream:
        if "score" in info and info.get("pv"):
            last = info
            if not info.get("lowerbound", False) and not info.get("upperbound", False):
                exact = info
    raw = scored_row(board, last or {})
    chosen = scored_row(board, exact) if exact else raw.copy()
    chosen["final_raw_result"] = raw
    chosen["used_earlier_completed_iteration"] = exact is not None and exact is not last
    return chosen


def label_chunk(engine_path, items, nodes):
    results = []
    with chess.engine.SimpleEngine.popen_uci(["nice", "-n", "10", str(engine_path)], timeout=120) as engine:
        if "stockfish" not in engine.id.get("name", "").lower():
            raise ValueError("the independent teacher must identify as Stockfish")
        engine.configure(OPTIONS)
        identity = dict(engine.id)
        for item in items:
            board = chess.Board(item["fen"])
            # Aggregated analyse() info can retain bound flags from an earlier
            # iteration. Use one actual scored UCI row, and fresh state per root.
            with engine.analysis(board, chess.engine.Limit(nodes=nodes), multipv=1, game=object()) as analysis:
                row = completed_result(board, analysis)
            row.update(index=item["index"], reference_identity=identity)
            results.append(row)
    return results


def probability(cp):
    return 1 / (1 + math.exp(-cp / 400))


def expected_score_cp(expected):
    """Encode expected points on the trainer's sigmoid(score / 400) scale.

    WDL comes from the teacher's own self-play model; it is not a measured
    ArhanPassant winning probability. Draws contribute half a point.
    """
    if not math.isfinite(expected) or not 0 <= expected <= 1:
        raise ValueError("expected score must be finite and in [0, 1]")
    if expected == 0:
        return -32000
    if expected == 1:
        return 32000
    return max(-32000, min(32000, round(400 * math.log(expected / (1 - expected)))))


def write_pairs(out, inputs, scored, target_mode="raw-cp"):
    if target_mode not in ("raw-cp", "wdl"):
        raise ValueError("unknown teacher target mode")
    scored = sorted(scored, key=lambda r: r["index"])
    if len(inputs) != len(scored) or any(a["index"] != b["index"] for a, b in zip(inputs, scored)):
        raise ValueError("reference coverage incomplete or misordered")
    accepted, excluded = [], []
    with (out / "baseline.bin").open("xb") as old, (out / "teacher.bin").open("xb") as new:
        for item, row in zip(inputs, scored):
            if row["lowerbound"] or row["upperbound"]:
                excluded.append({**row, "reason": "bounded score"})
                continue
            raw = bytes.fromhex(item["raw_hex"])
            if target_mode == "wdl":
                if "teacher_expected_score_white" not in row:
                    raise ValueError("completed reference iteration has no WDL target")
                target_cp = expected_score_cp(row["teacher_expected_score_white"])
            else:
                target_cp = row["teacher_white_cp"]
            labelled = replace_score(raw, target_cp)
            if raw[:24] != labelled[:24] or raw[26:] != labelled[26:]:
                raise ValueError("teacher changed non-score bytes")
            old.write(raw)
            new.write(labelled)
            accepted.append({**item, **row, "target_mode": target_mode, "target_white_cp": target_cp,
                             "probability_delta_white": probability(target_cp) - probability(item["original_white_cp"])})
    if not accepted:
        raise ValueError("no unbounded teacher labels")
    errors = sorted(abs(r["probability_delta_white"]) for r in accepted)
    return {"target_mode": target_mode, "labelled": len(accepted), "excluded_bounds": len(excluded),
            "earlier_completed_iteration": sum(r.get("used_earlier_completed_iteration", False) for r in accepted),
            "teacher_quiet_best": sum(r["teacher_best_move_quiet"] for r in accepted),
            "teacher_mates": sum(r["teacher_mate_white"] is not None for r in accepted),
            "distinct_board_turn": len({" ".join(r["fen"].split()[:2]) for r in accepted}),
            "mean_abs_probability_delta": sum(errors) / len(errors),
            "p95_abs_probability_delta": errors[(len(errors) - 1) * 95 // 100],
            "delta_at_least_0_1": sum(e >= 0.1 for e in errors),
            "delta_at_least_0_2": sum(e >= 0.2 for e in errors),
            "rows": accepted, "excluded": excluded}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--engine", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--samples", type=int, default=4096)
    ap.add_argument("--nodes", type=int, default=100000)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--target-mode", choices=("raw-cp", "wdl"), required=True,
                    help="raw-cp audits normalized Stockfish scores; wdl encodes teacher expected points on the trainer sigmoid400 scale")
    args = ap.parse_args()
    if min(args.samples, args.nodes, args.workers) < 1 or args.workers > (os.cpu_count() or 1):
        ap.error("positive sample/node/worker budgets within available CPU cores are required")
    if args.out.resolve().is_relative_to((args.data / "selfplay").resolve()):
        ap.error("experimental labels must stay outside the live self-play tree")
    state_bytes = args.state.read_bytes()
    state = json.loads(state_bytes)
    args.out.mkdir(parents=True, exist_ok=False)
    report = {"started": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "running",
              "engine": str(args.engine.resolve()), "engine_sha256": digest(args.engine),
              "state_sha256": hashlib.sha256(state_bytes).hexdigest(), "script_sha256": digest(__file__),
              "cpu_workers": args.workers, "nodes_per_position": args.nodes, "reference_options": OPTIONS,
              "target_mode": args.target_mode,
              "limitations": "Uniform pilot conditional on unambiguous irreversible-move positions, not a representative strength test. Legacy omitted state is excluded, not inferred. Raw-cp mode directly records normalized Stockfish CP for audit, not a calibrated trainer target. WDL mode maps the teacher self-play expected points to sigmoid400 using inverse logit; it is not ArhanPassant's measured win probability. The last completed unbounded iteration is used; interrupted final bounds are retained separately. Quiet and nonquiet teacher choices are retained separately. Before model comparisons, group held-out splits by board/turn and keep identical input rows and training resources. No live data/model promotion."}
    try:
        items, sampling = fresh_sample(args.data, state, args.samples, args.seed)
        report["sampling"] = sampling
        (args.out / "inputs.json").write_text(json.dumps(items, indent=1) + "\n")
        (args.out / "sample.bin").write_bytes(b"".join(bytes.fromhex(r["raw_hex"]) for r in items))
        scored = []
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            jobs = [pool.submit(label_chunk, args.engine, items[i::args.workers], args.nodes) for i in range(args.workers)]
            for job in concurrent.futures.as_completed(jobs):
                scored.extend(job.result())
                print(f"Scored {len(scored)}/{len(items)} sampled positions", flush=True)
        if digest(args.engine) != report["engine_sha256"]:
            raise ValueError("reference binary changed during labeling")
        report["summary"] = write_pairs(args.out, items, scored, args.target_mode)
        report["artifacts_sha256"] = {p.name: digest(p) for p in args.out.iterdir() if p.is_file()}
        report["status"] = "complete"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (args.out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    summary = {k: v for k, v in report["summary"].items() if k not in ("rows", "excluded")}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
