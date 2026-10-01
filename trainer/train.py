"""Train an ArhanPassant NNUE network on self-play data.

Architecture: (768 -> H) x 2 perspectives -> 1, squared clipped ReLU, matching
engine/src/nnue.rs. The network predicts win probability via
sigmoid(output) where output is in units of 400 centipawns. Targets blend the
search score and the game result.

    python trainer/train.py --data ~/arhanpassant-data/local/gen0 --hidden 256 \
        --epochs 12 --out engine/nets/gen1.nnue
"""

import argparse
import glob
import math
import os
import queue
import struct
import sys
import threading
import time

import numpy as np
import torch
import torch.nn as nn

REC = 32
QA, QB, SCALE = 255, 64, 400.0


def load(paths):
    files = []
    for p in paths:
        p = os.path.expanduser(p)
        files += sorted(glob.glob(os.path.join(p, "**", "*.bin"), recursive=True)) if os.path.isdir(p) else [p]
    if not files:
        sys.exit("no data files found")
    arrays = []
    for f in files:
        a = np.fromfile(f, dtype=np.uint8)
        n = len(a) // REC
        arrays.append(a[: n * REC].reshape(n, REC))
    data = np.concatenate(arrays)
    print(f"loaded {len(data):,} positions from {len(files)} files", flush=True)
    return data


def decode(batch):
    """Records -> (row, stm feature, nstm feature) index lists, stm-relative score and result."""
    b = batch.shape[0]
    bits = np.unpackbits(np.ascontiguousarray(batch[:, :8]), axis=1, bitorder="little")
    nib = batch[:, 8:24]
    codes = np.empty((b, 32), np.uint8)
    codes[:, 0::2] = nib & 0xF
    codes[:, 1::2] = nib >> 4
    rank = np.cumsum(bits, axis=1) - 1
    rows, sqs = np.nonzero(bits)
    code = codes[rows, rank[rows, sqs]].astype(np.int64)
    color, pt = code >> 3, code & 7
    sqs = sqs.astype(np.int64)
    white_f = np.where(color == 0, 0, 384) + pt * 64 + sqs
    black_f = np.where(color == 1, 0, 384) + pt * 64 + (sqs ^ 56)
    stm = batch[:, 27].astype(np.int64)
    stm_rows = stm[rows]
    f_stm = np.where(stm_rows == 0, white_f, black_f)
    f_nstm = np.where(stm_rows == 0, black_f, white_f)
    score = np.frombuffer(np.ascontiguousarray(batch[:, 24:26]).tobytes(), dtype="<i2").astype(np.float32)
    result = batch[:, 26].astype(np.float32) / 2.0
    score_stm = np.where(stm == 0, score, -score)
    result_stm = np.where(stm == 0, result, 1.0 - result)
    return rows, f_stm, f_nstm, score_stm, result_stm


class Net(nn.Module):
    def __init__(self, hidden):
        super().__init__()
        self.hidden = hidden
        self.ft = nn.Linear(768, hidden)
        self.out = nn.Linear(2 * hidden, 1)

    def forward(self, stm, nstm):
        a = torch.clamp(self.ft(stm), 0.0, 1.0).pow(2)
        b = torch.clamp(self.ft(nstm), 0.0, 1.0).pow(2)
        return self.out(torch.cat([a, b], dim=1))


def to_inputs(dec, b, device):
    rows, f_stm, f_nstm, score, result = dec
    r = torch.from_numpy(rows).to(device)
    stm = torch.zeros(b, 768, device=device)
    nstm = torch.zeros(b, 768, device=device)
    stm[r, torch.from_numpy(f_stm).to(device)] = 1.0
    nstm[r, torch.from_numpy(f_nstm).to(device)] = 1.0
    return stm, nstm, torch.from_numpy(score).to(device), torch.from_numpy(result).to(device)


def batches(data, idx, batch_size):
    for i in range(0, len(idx) - batch_size + 1, batch_size):
        chunk = data[np.sort(idx[i : i + batch_size])]
        yield chunk.shape[0], decode(chunk)


def prefetch(gen, depth=6):
    q = queue.Queue(maxsize=depth)
    done = object()

    def run():
        for item in gen:
            q.put(item)
        q.put(done)

    threading.Thread(target=run, daemon=True).start()
    while True:
        item = q.get()
        if item is done:
            return
        yield item


def export(net, path):
    with torch.no_grad():
        ftw = (net.ft.weight.T.cpu() * QA).round().clamp(-32768, 32767).to(torch.int16).numpy()
        ftb = (net.ft.bias.cpu() * QA).round().clamp(-32768, 32767).to(torch.int16).numpy()
        ow = (net.out.weight[0].cpu() * QB).round().clamp(-32768, 32767).to(torch.int16).numpy()
        ob = int(round(float(net.out.bias[0].cpu()) * QA * QB))
    with open(path, "wb") as f:
        f.write(b"APNN")
        f.write(struct.pack("<II", 1, net.hidden))
        f.write(ftw.astype("<i2").tobytes())
        f.write(ftb.astype("<i2").tobytes())
        f.write(ow.astype("<i2").tobytes())
        f.write(struct.pack("<i", ob))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=16384)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--wdl", type=float, default=0.3, help="weight of the game result in the target")
    ap.add_argument("--val", type=float, default=0.01, help="held-out fraction (last games)")
    ap.add_argument("--max-positions", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    data = load(args.data)
    if args.max_positions:
        data = data[: args.max_positions]
    n_val = max(args.batch, int(len(data) * args.val))
    train_idx = np.arange(len(data) - n_val)
    val_idx = np.arange(len(data) - n_val, len(data))
    print(f"train {len(train_idx):,} val {len(val_idx):,} hidden {args.hidden} device {device}", flush=True)

    net = Net(args.hidden).to(device)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=0.0)
    steps_per_epoch = len(train_idx) // args.batch
    total = steps_per_epoch * args.epochs
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: 0.5 * (1 + math.cos(math.pi * min(s, total) / total)) * 0.99 + 0.01)

    def loss_of(stm, nstm, score, result):
        pred = torch.sigmoid(net(stm, nstm)).squeeze(1)
        target = (1 - args.wdl) * torch.sigmoid(score / SCALE) + args.wdl * result
        return torch.mean((pred - target) ** 2)

    def validate():
        net.eval()
        tot, n = 0.0, 0
        with torch.no_grad():
            for b, dec in batches(data, val_idx, args.batch):
                tot += float(loss_of(*to_inputs(dec, b, device))) * b
                n += b
        net.train()
        return tot / max(n, 1)

    print(f"epoch 0 val {validate():.6f}", flush=True)
    step = 0
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        perm = rng.permutation(train_idx)
        run, cnt = 0.0, 0
        for b, dec in prefetch(batches(data, perm, args.batch)):
            loss = loss_of(*to_inputs(dec, b, device))
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            with torch.no_grad():
                net.out.weight.clamp_(-1.98, 1.98)
            run += float(loss)
            cnt += 1
            step += 1
        val = validate()
        rate = cnt * args.batch / (time.time() - t0)
        print(f"epoch {epoch} train {run / max(cnt, 1):.6f} val {val:.6f} lr {sched.get_last_lr()[0]:.2e} {rate:,.0f} pos/s", flush=True)
        export(net, args.out)
        torch.save(net.state_dict(), args.out + ".pt")
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
