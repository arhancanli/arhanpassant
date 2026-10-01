"""End-to-end checks for the training pipeline.

1. The Python record decoder agrees with the engine's decoder (`arhanpassant dump`).
2. A network trained here and exported evaluates the same inside the engine.

    python trainer/check_pipeline.py --engine target/release/arhanpassant --data FILE.bin
"""

import argparse
import subprocess
import sys

import numpy as np
import torch

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import train  # noqa: E402

PIECES = "PNBRQK"


def placement(rec):
    rows, f_stm, f_nstm, _, _ = train.decode(rec[None, :])
    stm = rec[27]
    board = [None] * 64
    # Rebuild from White-perspective features (f_stm if White to move else f_nstm).
    feats = f_stm if stm == 0 else f_nstm
    for f in feats:
        own, rest = divmod(int(f), 384)
        pt, sq = divmod(rest, 64)
        ch = PIECES[pt]
        board[sq] = ch if own == 0 else ch.lower()
    out = []
    for rank in range(7, -1, -1):
        empty, s = 0, ""
        for file in range(8):
            p = board[rank * 8 + file]
            if p is None:
                empty += 1
            else:
                if empty:
                    s += str(empty)
                    empty = 0
                s += p
        if empty:
            s += str(empty)
        out.append(s)
    return "/".join(out)


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
    mism = 0
    for rec, line in zip(recs, dump):
        place, side, score, result = line.split()
        rows, _, _, s_stm, r_stm = train.decode(rec[None, :])
        stm = "w" if rec[27] == 0 else "b"
        exp_score = float(score) if stm == "w" else -float(score)
        exp_res = int(result) / 2 if stm == "w" else 1 - int(result) / 2
        if placement(rec) != place or side != stm or s_stm[0] != exp_score or r_stm[0] != exp_res:
            mism += 1
    print(f"decoder: {len(dump)} records compared, {mism} mismatches")
    assert len(dump) == len(recs) and mism == 0

    # 2. export agreement: random small network, float vs engine (quantised)
    torch.manual_seed(0)
    net = train.Net(64)
    with torch.no_grad():
        net.ft.weight.uniform_(-0.1, 0.1)
        net.ft.bias.uniform_(0.0, 0.3)
        net.out.weight.uniform_(-1.0, 1.0)
        net.out.bias.uniform_(-0.2, 0.2)
    path = "/tmp/check-pipeline.nnue"
    train.export(net, path)
    fens = [placement(r) + (" w" if r[27] == 0 else " b") + " - - 0 1" for r in recs[:200]]
    cmds = [f"setoption name EvalFile value {path}"]
    for fen in fens:
        cmds += [f"position fen {fen}", "eval"]
    cmds.append("quit")
    out = subprocess.run([args.engine], input="\n".join(cmds) + "\n", capture_output=True, text=True, check=True).stdout
    engine_vals = [int(l.split()[1]) for l in out.splitlines() if l.startswith("nnue ")]
    assert len(engine_vals) == len(fens), out[:500]
    # Exact integer emulation of engine/src/nnue.rs from the exported file.
    blob = open(path, "rb").read()
    h = int.from_bytes(blob[8:12], "little")
    o = 12
    ftw = np.frombuffer(blob[o : o + 768 * h * 2], "<i2").reshape(768, h).astype(np.int64); o += 768 * h * 2
    ftb = np.frombuffer(blob[o : o + h * 2], "<i2").astype(np.int64); o += h * 2
    ow = np.frombuffer(blob[o : o + 2 * h * 2], "<i2").astype(np.int64); o += 2 * h * 2
    ob = int.from_bytes(blob[o : o + 4], "little", signed=True)

    def tdiv(a, b):  # Rust integer division truncates toward zero
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b > 0) else -q

    exact, quant_err = 0, []
    for rec, ev in zip(recs[:200], engine_vals):
        _, f_stm, f_nstm, _, _ = train.decode(rec[None, :])
        acc_s = ftb + ftw[f_stm].sum(axis=0)
        acc_n = ftb + ftw[f_nstm].sum(axis=0)
        cs, cn = np.clip(acc_s, 0, 255), np.clip(acc_n, 0, 255)
        total = int((cs * cs * ow[:h]).sum() + (cn * cn * ow[h:]).sum())
        emu = tdiv((tdiv(total, 255) + ob) * 400, 255 * 64)
        exact += emu == ev
        stm, nstm, _, _ = train.to_inputs(train.decode(rec[None, :]), 1, torch.device("cpu"))
        with torch.no_grad():
            quant_err.append(abs(float(net(stm, nstm)) * train.SCALE - ev))
    print(f"export: engine == integer emulation on {exact}/{len(engine_vals)} positions; "
          f"float-vs-quantised error mean {np.mean(quant_err):.2f} cp (rounding of a random test net)")
    assert exact == len(engine_vals)
    assert np.mean(quant_err) < 20, "export distorts the network (wrong layout?)"


if __name__ == "__main__":
    main()
