"""Where ArhanPassant loses: Stockfish's verdict on every move it made in lost games.

    python forge/blunders.py GAMES.txt [GAMES.txt ...] --stockfish PATH [--nodes 300000]
                                [--sample 150] [--threads 4] [--out report.json]

GAMES.txt files are the runner's --games-out lines (one JSON object per game).
For each of our moves in a sampled loss, Stockfish recommends a move. If our
move differs, both alternatives are searched from the same position with the
same node limit and fresh search state. The difference in winning chances
is the estimated move loss. A recommended move has zero loss; changes in
Stockfish's evaluation on a later search are not attributed to that move.
Drops of 20+ points are blunders, 10+ mistakes. Fixed-node analysis remains
an estimate: a restricted search can disagree with the initial recommendation,
and those disagreements are reported. Phases use the recorded FEN counters;
opening books that reset them make the opening label approximate.
Install the dependencies from forge/requirements-analysis.txt.
"""

import argparse
import hashlib
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


def reference_search(sf, board, nodes, us, root_move=None):
    """Start fresh, including for both restricted alternatives at one root."""
    info = sf.analyse(board, chess.engine.Limit(nodes=nodes), game=object(),
                      root_moves=[root_move] if root_move is not None else None)
    pv = info.get("pv", [])
    if not pv or pv[0] not in board.legal_moves or (root_move is not None and pv[0] != root_move):
        raise ValueError("reference search did not return the requested legal root move")
    return score_cp(info, us), pv[0]


def analyse_game(game, sf_path, nodes):
    if US.casefold() not in (game["white"].casefold(), game["black"].casefold()):
        raise ValueError("game does not contain ArhanPassant")
    us_white = game["white"].casefold() == US.casefold()
    us = chess.WHITE if us_white else chess.BLACK
    board = chess.Board(game["fen"])
    moves = game["moves"].split()
    rows = []
    with chess.engine.SimpleEngine.popen_uci(sf_path) as sf:
        options = {"Hash": 64, "Threads": 1}
        for name, value in (("UCI_LimitStrength", False), ("Skill Level", 20)):
            if name in sf.options:
                options[name] = value
        sf.configure(options)
        for text in moves:
            if board.is_game_over():
                break
            played = board.parse_uci(text)
            if board.turn == us:
                recommended_eval, best = reference_search(sf, board, nodes, us)
                best_eval = played_eval = recommended_eval
                if played != best:
                    best_eval, _ = reference_search(sf, board, nodes, us, best)
                    played_eval, _ = reference_search(sf, board, nodes, us, played)
                drop = max(0.0, win_pct(best_eval) - win_pct(played_eval))
                rows.append({"phase": phase(board), "drop": round(drop, 1), "fen": board.fen(),
                             "played": text, "best": best.uci(), "eval_best": best_eval,
                             "eval_played": played_eval, "eval_recommended": recommended_eval,
                             "reference_disagreement": played_eval > best_eval,
                             "move": board.fullmove_number,
                             "opponent": game["black"] if us_white else game["white"]})
            board.push(played)
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
    if args.nodes <= 0 or args.sample < 0 or args.threads <= 0:
        ap.error("nodes and threads must be positive; sample must be nonnegative")

    losses = []
    for path in args.games:
        with open(path) as source:
            for line in source:
                if not line.strip():
                    continue
                g = json.loads(line)
                white, black = g["white"].casefold(), g["black"].casefold()
                if US.casefold() not in (white, black):
                    continue
                lost = ((g["result"] == "1-0" and black == US.casefold())
                        or (g["result"] == "0-1" and white == US.casefold()))
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
    def digest(path):
        h = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    report = {"schema_version": 2,
              "method": "fresh same-position equal-node restricted alternatives; recommended moves have zero loss",
              "phase_method": "recorded FEN move counter and material; reset opening-book counters are approximate",
              "moves_analysed": len(moves),
              "recommended_moves_played": sum(r["played"] == r["best"] for r in moves),
              "reference_disagreements": sum(r["reference_disagreement"] for r in moves),
              "losses_available": len(losses), "losses_analysed": len(sample), "stockfish_nodes": args.nodes,
              "stockfish_sha256": digest(args.stockfish), "input_sha256": {p: digest(p) for p in args.games},
              "seed": args.seed,
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
