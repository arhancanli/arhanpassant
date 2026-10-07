"""Chess960 move generation check: the engine's perft against python-chess.

    python tools/chess960_perft.py ENGINE [--positions 300] [--depth 3] [--deep 6 --deep-depth 5]

Positions: random Chess960 start positions, positions reached by random play
from them (castling rights kept as long as the moves allow), and hand-made
castling edge cases.
"""
import argparse
import random
import subprocess
import sys

import chess

EDGE = [
    # King on b1 and rook on a1: O-O-O puts the king on c1 and the rook on d1.
    "1r2k1r1/8/8/8/8/8/8/RK4R1 w GAgb - 0 1",
    # King already on g1: O-O moves only the rook (h1 to f1).
    "4k3/8/8/8/8/8/8/R5KR w HA - 0 1",
    # The castling rook shields the king from a queen on the back rank.
    "4k3/8/8/8/8/8/8/qR1K3R w HB - 0 1",
    # King's destination attacked; rook path blocked; rights for both sides.
    "rk2r3/8/8/8/8/8/2b5/RK2R3 w EAea - 0 1",
    # King on f1 with the rook on g1: O-O swaps them; f1g1 is also a plain king move.
    "k6r/8/8/8/8/8/8/5KR1 w G - 0 1",
    # King on c1 with rooks on b1 and h1: O-O-O moves only the rook (b1 to d1).
    "1rk4r/8/8/8/8/8/8/1RK4R w HBhb - 0 1",
]


def perft(board, depth):
    if depth == 0:
        return 1
    if depth == 1:
        return board.legal_moves.count()
    n = 0
    for m in board.legal_moves:
        board.push(m)
        n += perft(board, depth - 1)
        board.pop()
    return n


class Engine:
    def __init__(self, path):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.send("uci")
        self.wait("uciok")
        self.send("setoption name UCI_Chess960 value true")

    def send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def wait(self, token):
        lines = []
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine exited")
            lines.append(line.rstrip("\n"))
            if line.startswith(token):
                return lines

    def perft(self, fen, depth):
        self.send(f"position fen {fen}")
        self.send(f"go perft {depth}")
        lines = self.wait("Nodes searched")
        errors = [l for l in lines if l.startswith("info string error")]
        if errors:
            raise RuntimeError(f"engine rejected {fen}: {errors[0]}")
        return int(lines[-1].split()[-1])


def positions(rng, count):
    out = []
    while len(out) < count:
        b = chess.Board.from_chess960_pos(rng.randrange(960))
        out.append(b.fen(shredder=True))
        for _ in range(rng.randrange(1, 30)):
            moves = list(b.legal_moves)
            if not moves:
                break
            # Prefer castling when available so it gets exercised.
            castles = [m for m in moves if b.is_castling(m)]
            b.push(rng.choice(castles) if castles and rng.random() < 0.3 else rng.choice(moves))
        if not b.is_game_over():
            out.append(b.fen(shredder=True))
    return out[:count]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("--positions", type=int, default=300)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--deep", type=int, default=6, help="positions also checked at --deep-depth")
    ap.add_argument("--deep-depth", type=int, default=5)
    ap.add_argument("--seed", type=int, default=960)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    eng = Engine(a.engine)
    fens = EDGE + positions(rng, a.positions)
    bad = 0
    for i, fen in enumerate(fens):
        board = chess.Board(fen, chess960=True)
        depth = a.deep_depth if i < a.deep else a.depth
        want = perft(board, depth)
        got = eng.perft(fen, depth)
        if got != want:
            bad += 1
            print(f"MISMATCH depth {depth}: engine {got} python-chess {want}  {fen}")
    print(f"{len(fens)} positions, {bad} mismatches (first {a.deep} at depth {a.deep_depth}, the rest at depth {a.depth})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
