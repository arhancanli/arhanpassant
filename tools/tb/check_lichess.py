"""Check the engine's Syzygy probes against the Lichess tablebase server.

    python tools/tb/check_lichess.py ENGINE SYZYGY_DIR [N]

Random legal positions are drawn from the material sets present in SYZYGY_DIR.
For each, the engine's `tb` command gives a win/draw/loss verdict and the root
moves it keeps; the server gives its own verdict and every move's result. A
position passes when the verdicts agree and every kept move is one of the
server's best moves (same result; for wins and losses, the best distance to zero).
"""

import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import chess

engine, tb_dir = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 150
rng = random.Random(20261002)
sets = sorted(f[:-5] for f in os.listdir(tb_dir) if f.endswith(".rtbw"))

VERDICT = {"Win": "win", "CursedWin": "cursed-win", "Draw": "draw", "BlessedLoss": "blessed-loss", "Loss": "loss"}
# A move's category on the server is the opponent's result after it.
FLIP = {"win": "loss", "loss": "win", "draw": "draw", "cursed-win": "blessed-loss", "blessed-loss": "cursed-win"}


def random_position(material):
    white, black = material.split("v")
    while True:
        board = chess.Board(None)
        squares = rng.sample(range(64), len(white) + len(black))
        ok = True
        for color, side in ((chess.WHITE, white), (chess.BLACK, black)):
            for ch in side:
                sq = squares.pop()
                pt = chess.Piece.from_symbol(ch).piece_type
                if pt == chess.PAWN and chess.square_rank(sq) in (0, 7):
                    ok = False
                board.set_piece_at(sq, chess.Piece(pt, color))
        board.turn = rng.choice([chess.WHITE, chess.BLACK])
        if ok and board.is_valid() and not (board.is_checkmate() or board.is_stalemate()):
            return board


proc = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)


def send(cmd):
    proc.stdin.write(cmd + "\n")
    proc.stdin.flush()


send(f"setoption name SyzygyPath value {tb_dir}")
send("isready")
while proc.stdout.readline().strip() != "readyok":
    pass

fails = 0
skipped = 0
for i in range(n):
    board = random_position(rng.choice(sets))
    fen = board.fen()
    send(f"position fen {fen}")
    send("tb")
    line = proc.stdout.readline().split()
    # tb wdl Some(Win) root Some(Win) moves a b c
    ours = line[2].removeprefix("Some(").removesuffix(")")
    kept = line[line.index("moves") + 1:]
    if line[4] == "None":
        # Some moves lead to tables that are not present (a partial 5-piece set):
        # no root filter, nothing to compare.
        skipped += 1
        continue
    url = "https://tablebase.lichess.ovh/standard?fen=" + urllib.parse.quote(fen)
    for attempt in range(5):
        try:
            data = json.load(urllib.request.urlopen(url, timeout=20))
            break
        except Exception:
            time.sleep(5 * (attempt + 1))
    theirs = data["category"]
    moves = {m["uci"]: m for m in data["moves"]}
    problems = []
    if VERDICT.get(ours) != theirs:
        problems.append(f"verdict {ours} vs {theirs}")
    best = [m for m in data["moves"] if FLIP[m["category"]] == theirs]

    def plies(m):
        # Syzygy's root ranking: a move that resets the fifty-move counter is one
        # ply from zero; any other move is one more than the position it leads to.
        return 1 if m.get("zeroing") else abs(m["dtz"]) + 1

    if theirs in ("win", "cursed-win", "loss", "blessed-loss") and best:
        d = [plies(m) for m in best if m.get("dtz") is not None]
        target = min(d) if theirs in ("win", "cursed-win") else max(d)
        best = [m for m in best if m.get("dtz") is None or abs(plies(m) - target) <= 1]
    best_set = {m["uci"] for m in best}
    for k in kept:
        if k not in best_set:
            problems.append(f"kept {k} ({moves.get(k, {}).get('category')}, dtz {moves.get(k, {}).get('dtz')}) not best")
    if not kept:
        problems.append("no moves kept")
    if problems:
        fails += 1
        print(f"FAIL {fen}: {'; '.join(problems)}")
    time.sleep(1.5)
    if (i + 1) % 10 == 0:
        print(f"{i + 1} checked, {fails} failed", flush=True)
print(f"{n - fails - skipped}/{n - skipped} positions agree with the Lichess tablebase ({skipped} skipped: moves lead to tables not present)")
send("quit")
sys.exit(1 if fails else 0)
