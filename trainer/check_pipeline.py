"""End-to-end checks for the training pipeline.

1. An independent Python board decoder agrees with the engine's (`arhanpassant dump`).
2. For each network layout (1x1 and 8 king buckets x 8 output buckets), a
   network exported by the trainer evaluates in the engine exactly as an
   integer re-implementation computes it from the file and the trainer's
   features, and the float network is close (a scrambled export is not).

    python trainer/check_pipeline.py --engine target/release/arhanpassant --data FILE.bin
"""

import argparse
import struct
import subprocess
import sys

import numpy as np
import torch

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import train  # noqa: E402

PIECES = "pnbrqk"


def placement(rec):
    """FEN placement straight from the record bytes (no trainer code involved)."""
    occ = int.from_bytes(bytes(rec[:8]), "little")
    board, i = [None] * 64, 0
    for sq in range(64):
        if occ >> sq & 1:
            code = (rec[8 + i // 2] >> (4 * (i % 2))) & 0xF
            ch = PIECES[code & 7]
            board[sq] = ch if code & 8 else ch.upper()
            i += 1
    rows = []
    for rank in range(7, -1, -1):
        s, empty = "", 0
        for file in range(8):
            p = board[rank * 8 + file]
            if p is None:
                empty += 1
            else:
                s += (str(empty) if empty else "") + p
                empty = 0
        rows.append(s + (str(empty) if empty else ""))
    return "/".join(rows)


def fen_of(rec):
    return placement(rec) + (" w" if rec[27] == 0 else " b") + " - - 0 1"


def tdiv(a, b):  # Rust integer division truncates toward zero
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def emulate(path, recs):
    """Integer forward pass of an exported file, using the trainer's features."""
    blob = open(path, "rb").read()
    version, h = struct.unpack_from("<II", blob, 4)
    o = 12
    ib, ob = 1, 1
    if version == 2:
        ib, ob, _flags = struct.unpack_from("<III", blob, o)
        o += 12 + 64
    ftw = np.frombuffer(blob, "<i2", ib * 768 * h, o).reshape(ib * 768, h).astype(np.int64); o += ib * 768 * h * 2
    ftb = np.frombuffer(blob, "<i2", h, o).astype(np.int64); o += h * 2
    ow = np.frombuffer(blob, "<i2", ob * 2 * h, o).reshape(ob, 2 * h).astype(np.int64); o += ob * 2 * h * 2
    obias = np.frombuffer(blob, "<i4", ob, o).astype(np.int64)
    out = []
    for rec in recs:
        f_stm, f_nstm, _, bucket, _, _ = train.decode(rec[None, :])
        cs = np.clip(ftb + ftw[f_stm].sum(axis=0), 0, 255)
        cn = np.clip(ftb + ftw[f_nstm].sum(axis=0), 0, 255)
        k = int(bucket[0])
        total = int((cs * cs * ow[k, :h]).sum() + (cn * cn * ow[k, h:]).sum())
        out.append(tdiv((tdiv(total, 255) + int(obias[k])) * 400, 255 * 64))
    return out


def engine_evals(engine, path, fens):
    cmds = [f"setoption name EvalFile value {path}"]
    for fen in fens:
        cmds += [f"position fen {fen}", "eval"]
    cmds.append("quit")
    out = subprocess.run([engine], input="\n".join(cmds) + "\n", capture_output=True, text=True, check=True).stdout
    vals = [int(l.split()[1]) for l in out.splitlines() if l.startswith("nnue ")]
    assert len(vals) == len(fens), out[:300]
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=3000)
    args = ap.parse_args()
    raw = np.fromfile(args.data, dtype=np.uint8)
    recs = raw[: (len(raw) // 32) * 32].reshape(-1, 32)[: args.n]

    # 1. decoder agreement
    dump = subprocess.run([args.engine, "dump", args.data, str(args.n)], capture_output=True, text=True, check=True).stdout.splitlines()
    mism = sum(placement(r) + (" w" if r[27] == 0 else " b") != " ".join(line.split()[:2]) for r, line in zip(recs, dump))
    print(f"decoder: {len(dump)} records compared, {mism} mismatches")
    assert len(dump) == len(recs) and mism == 0

    # 2. export agreement per layout
    sample = recs[:200]
    fens = [fen_of(r) for r in sample]
    for ib, ob in [(1, 1), (8, 8)]:
        train.set_layout(ib, ob)
        torch.manual_seed(0)
        net = train.Net(64)
        with torch.no_grad():
            net.ft.weight.uniform_(-0.1, 0.1)
            net.ft_bias.uniform_(0.0, 0.3)
            net.out.weight.uniform_(-1.0, 1.0)
            net.out.bias.uniform_(-0.2, 0.2)
        path = f"/tmp/check-pipeline-{ib}x{ob}.nnue"
        train.export(net, path)
        eng = engine_evals(args.engine, path, fens)
        emu = emulate(path, sample)
        exact = sum(a == b for a, b in zip(eng, emu))
        err = []
        with torch.no_grad():
            for rec, ev in zip(sample, eng):
                inputs, _, _ = train.to_inputs(train.decode(rec[None, :]), torch.device("cpu"))
                err.append(abs(float(net(*inputs)[0]) * train.SCALE - ev))
        print(f"layout {ib}x{ob}: engine == integer emulation on {exact}/{len(eng)} positions; float-vs-quantised error mean {np.mean(err):.2f} cp")
        assert exact == len(eng)
        assert np.mean(err) < 20, "export distorts the network (wrong layout?)"


if __name__ == "__main__":
    main()
