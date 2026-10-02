"""Where ArhanPassant loses: Stockfish's verdict on every move it made in lost games.

    python forge/blunders.py GAMES.txt [GAMES.txt ...] --stockfish PATH [--nodes 300000]
                                [--sample 150] [--threads 4] [--out report.json]

GAMES.txt files are the runner's --games-out lines (one JSON object per game).
For each sampled loss, every position is evaluated once by Stockfish at a fixed
node count; each of our moves is scored by the drop in our winning chances
(Lichess's win-percentage curve) between the position before it and the
position after it. Drops of 20+ points are blunders, 10+ mistakes. They are
counted by game phase, normalised by how many moves we played in that phase,
and the worst ones are kept with Stockfish's preferred move.
Needs python-chess (the Lichess bot's virtualenv has it).
"""

import argparse
import json
import math
import random
from concurrent.futures import ThreadPoolExecutor

import chess
import chess.engine

US = "arhanpassant"


def win_pct(cp):
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def phase(board):
    """opening (first 12 moves), endgame (no queens, or little material), else middlegame."""
    if board.fullmove_number <= 12:
        return "opening"
    weights = {chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    material = sum(w * len(board.pieces(pt, c)) for pt, w in weights.items() for c in chess.COLORS)
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))
    return "endgame" if material <= 26 or (queens == 0 and material <= 34) else "middlegame"


def score_cp(info, pov):
    s = info["score"].pov(pov)
    return s.score(mate_score=10000)


def analyse_game(game, sf_path, nodes):
    us_white = game["white"] == US
    us = chess.WHITE if us_white else chess.BLACK
    board = chess.Board(game["fen"])
    moves = game["moves"].split()
    with chess.engine.SimpleEngine.popen_uci(sf_path) as sf:
        sf.configure({"Hash": 64, "Threads": 1})
        evals, best = [], []
        boards = []
        for m in moves + [None]:
            if board.is_game_over():
                evals.append(None)
                best.append(None)
                boards.append(board.copy())
                break
            info = sf.analyse(board, chess.engine.Limit(nodes=nodes))
            evals.append(score_cp(info, us))
            best.append(info.get("pv", [None])[0])
            boards.append(board.copy())
            if m is None:
                break
            board.push_uci(m)
    rows = []
    for i, m in enumerate(moves):
        b = boards[i]
        if b.turn != us or i + 1 >= len(evals) or evals[i] is None or evals[i + 1] is None:
            continue
        drop = win_pct(evals[i]) - win_pct(evals[i + 1])
        rows.append({"phase": phase(b), "drop": round(drop, 1), "fen": b.fen(), "played": m,
                     "best": best[i].uci() if best[i] else None, "eval_before": evals[i], "eval_after": evals[i + 1],
                     "move": b.fullmove_number, "opponent": game["black"] if us_white else game["white"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("games", nargs="+")
    ap.add_argument("--stockfish", required=True)
    ap.add_argument("--nodes", type=int, default=300_000)
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args()

    losses = []
    for path in args.games:
        for line in open(path):
            if not line.strip():
                continue
            g = json.loads(line)
            if US not in (g["white"], g["black"]):
                continue
            lost = (g["result"] == "1-0" and g["black"] == US) or (g["result"] == "0-1" and g["white"] == US)
            if lost:
                losses.append(g)
    random.Random(args.seed).shuffle(losses)
    sample = losses[:args.sample]
    with ThreadPoolExecutor(args.threads) as pool:
        per_game = list(pool.map(lambda g: analyse_game(g, args.stockfish, args.nodes), sample))

    moves = [r for rows in per_game for r in rows]
    by_phase = {}
    for r in moves:
        p = by_phase.setdefault(r["phase"], {"moves": 0, "mistakes": 0, "blunders": 0, "drop_total": 0.0})
        p["moves"] += 1
        p["drop_total"] += max(r["drop"], 0)
        p["mistakes"] += r["drop"] >= 10
        p["blunders"] += r["drop"] >= 20
    # Where each lost game turned: the phase of its largest drop.
    turned = {}
    for rows in per_game:
        if rows:
            worst = max(rows, key=lambda r: r["drop"])
            turned[worst["phase"]] = turned.get(worst["phase"], 0) + 1
    worst = sorted(moves, key=lambda r: -r["drop"])[:40]
    report = {"losses_available": len(losses), "losses_analysed": len(sample), "stockfish_nodes": args.nodes,
              "by_phase": {k: {**v, "drop_per_move": round(v["drop_total"] / max(v["moves"], 1), 2),
                               "blunders_per_100": round(100 * v["blunders"] / max(v["moves"], 1), 2)}
                           for k, v in by_phase.items()},
              "game_turned_in": turned, "worst_moves": worst}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    print(f"{len(sample)} of {len(losses)} losses analysed at {args.nodes:,} nodes")
    for k in ("opening", "middlegame", "endgame"):
        v = report["by_phase"].get(k)
        if v:
            print(f"  {k:<10} {v['moves']:>5} moves  {v['mistakes']:>4} mistakes  {v['blunders']:>4} blunders"
                  f"  ({v['blunders_per_100']}/100 moves, mean drop {v['drop_per_move']})  games turned here: {turned.get(k, 0)}")


if __name__ == "__main__":
    main()
