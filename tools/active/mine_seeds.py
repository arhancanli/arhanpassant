"""Seed positions for self-play from games the engine did not win.

Reads arena/milestone game records (JSON lines with white, black, fen, result,
moves) and writes the positions where `--engine` was to move in its losses,
and in its draws against opponents it scored at least `--draw-min-score`
against, leaving out the last `--skip-end` plies (already decided). These are
the regions where the evaluation misjudged the game; self-play started there
(with a few random moves for variety) turns them into training data.

    python tools/active/mine_seeds.py --games 'milestones/*/*-batches/*.games.jsonl' --out seeds.epd
"""

import argparse
import collections
import glob
import json

import chess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", nargs="+", required=True, help="glob(s) of .games.jsonl files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", default="ArhanPassant")
    ap.add_argument("--skip-end", type=int, default=16)
    ap.add_argument("--skip-start", type=int, default=0)
    ap.add_argument("--draw-min-score", type=float, default=0.6)
    args = ap.parse_args()
    games = []
    for pattern in args.games:
        for path in sorted(glob.glob(pattern)):
            with open(path) as f:
                games += [json.loads(line) for line in f if line.strip()]
    # Score per opponent, to tell which draws were missed wins.
    tally = collections.defaultdict(lambda: [0.0, 0])
    for g in games:
        ours = "white" if g["white"] == args.engine else "black" if g["black"] == args.engine else None
        if ours is None:
            continue
        opp = g["black" if ours == "white" else "white"]
        pts = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[g["result"]]
        tally[opp][0] += pts if ours == "white" else 1 - pts
        tally[opp][1] += 1
    seen, kept, used = set(), [], collections.Counter()
    for g in games:
        ours = "white" if g["white"] == args.engine else "black" if g["black"] == args.engine else None
        if ours is None:
            continue
        opp = g["black" if ours == "white" else "white"]
        lost = g["result"] == ("0-1" if ours == "white" else "1-0")
        drawn = g["result"] == "1/2-1/2"
        if not (lost or (drawn and tally[opp][0] / tally[opp][1] >= args.draw_min_score)):
            continue
        board = chess.Board(g["fen"])
        moves = g["moves"].split()
        color = chess.WHITE if ours == "white" else chess.BLACK
        for i, uci in enumerate(moves):
            if args.skip_start <= i < len(moves) - args.skip_end and board.turn == color and not board.is_check():
                key = board.board_fen() + (" w" if board.turn else " b")
                if key not in seen:
                    seen.add(key)
                    kept.append(board.fen())
            board.push_uci(uci)
        used["lost" if lost else "drawn"] += 1
    with open(args.out, "w") as f:
        f.write("\n".join(kept) + "\n")
    print(f"{len(games)} games read; seeds from {used['lost']} losses and {used['drawn']} missed wins: {len(kept)} positions -> {args.out}")


if __name__ == "__main__":
    main()
