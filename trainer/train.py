"""Train an ArhanPassant NNUE network on self-play data.

Architecture: (768 x input buckets -> H) x 2 perspectives -> output buckets,
squared clipped ReLU, matching engine/src/nnue.rs. Input buckets follow the
king of each perspective (mirrored left-right when it is on files e-h); the
output head is chosen by the number of pieces. The network predicts win
probability via sigmoid(output), with output in units of 400 centipawns.
Targets blend the search score and the game result.

    python trainer/train.py --data ~/arhanpassant-data/selfplay --hidden 512 \
        --input-buckets 8 --output-buckets 8 --epochs 4 --out net.nnue
"""

import argparse
import glob
import json
import math
import os
import struct
import sys
import time

import numpy as np
import torch
import torch.nn as nn

from stream import prefetch, prepare_file_limit

REC = 32
QA, QB, SCALE = 255, 64, 400.0


def default_buckets():
    """King square -> input bucket; identical to engine/src/nnue.rs DEFAULT_BUCKETS."""
    m = np.zeros(64, dtype=np.int64)
    for sq in range(64):
        file, rank = sq % 8, sq // 8
        f = file if file < 4 else 7 - file
        m[sq] = f if rank == 0 else (4 + (f >= 2)) if rank == 1 else 6 if rank in (2, 3) else 7
    return m


# Network layout; set from the command line before anything is decoded.
LAYOUT = {"input_buckets": 1, "output_buckets": 1, "mirror": False, "map": np.zeros(64, dtype=np.int64)}


def set_layout(input_buckets, output_buckets):
    LAYOUT["input_buckets"] = input_buckets
    LAYOUT["output_buckets"] = output_buckets
    LAYOUT["mirror"] = input_buckets > 1
    LAYOUT["map"] = default_buckets() if input_buckets > 1 else np.zeros(64, dtype=np.int64)
    if input_buckets not in (1, 8):
        sys.exit("--input-buckets must be 1 or 8")


BLOCK = 4096  # records read contiguously; blocks are shuffled, then records within a buffer


class Dataset:
    """Self-play records memory-mapped from many files, read in shuffled blocks.

    Files can be far larger than memory: each epoch visits every block once in
    random order and shuffles records inside a buffer of many blocks. Every
    `val_every`-th block is held out for validation, so the split is the same
    in every run and never mixes with training.
    """

    def __init__(self, paths, val_every=100, max_positions=0, file_sizes=None):
        files = []
        for p in paths:
            p = os.path.expanduser(p)
            files += sorted(glob.glob(os.path.join(p, "**", "*.bin"), recursive=True)) if os.path.isdir(p) else [p]
        if not files:
            sys.exit("no data files found")
        prepare_file_limit(len(files))
        self.maps, blocks = [], []
        total = 0
        for f in files:
            n = min(os.path.getsize(f), file_sizes[f] if file_sizes is not None else os.path.getsize(f)) // REC
            if n == 0:
                continue
            if max_positions and total + n > max_positions:
                n = max_positions - total
            m = np.memmap(f, dtype=np.uint8, mode="r", shape=(n * REC,)).reshape(n, REC)
            fi = len(self.maps)
            self.maps.append(m)
            blocks += [(fi, s, min(BLOCK, n - s)) for s in range(0, n, BLOCK)]
            total += n
            if max_positions and total >= max_positions:
                break
        self.val_blocks = [b for i, b in enumerate(blocks) if i % val_every == val_every - 1]
        self.train_blocks = [b for i, b in enumerate(blocks) if i % val_every != val_every - 1]
        self.n_train = sum(b[2] for b in self.train_blocks)
        self.n_val = sum(b[2] for b in self.val_blocks)
        print(f"{total:,} positions in {len(self.maps)} files (train {self.n_train:,}, val {self.n_val:,})", flush=True)

    def read(self, block):
        fi, start, n = block
        return np.asarray(self.maps[fi][start : start + n])

    def chunks(self, blocks, batch_size, rng=None, buffer_blocks=64):
        """Raw record batches: shuffled blocks, then shuffled records within a buffer."""
        order = list(blocks)
        if rng is not None:
            rng.shuffle(order)
        for i in range(0, len(order), buffer_blocks):
            buf = np.concatenate([self.read(b) for b in order[i : i + buffer_blocks]])
            if rng is not None:
                buf = buf[rng.permutation(len(buf))]
            for j in range(0, len(buf) - batch_size + 1, batch_size):
                yield buf[j : j + batch_size]

    def batches(self, blocks, batch_size, rng=None, workers=4):
        """Decoded batches; decoding runs on several threads (NumPy releases the GIL)."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(workers) as pool:
            pending = []
            for chunk in self.chunks(blocks, batch_size, rng):
                pending.append(pool.submit(lambda c: (c.shape[0], decode(c)), chunk))
                if len(pending) >= 2 * workers:
                    yield pending.pop(0).result()
            for f in pending:
                yield f.result()


def decode(batch):
    """Records -> per-perspective feature indices with row offsets, output buckets,
    stm-relative scores and results. Feature rows are grouped by record."""
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
    king = pt == 5
    wk = np.zeros(b, dtype=np.int64)
    bk = np.zeros(b, dtype=np.int64)
    wk[rows[king & (color == 0)]] = sqs[king & (color == 0)]
    bk[rows[king & (color == 1)]] = sqs[king & (color == 1)]

    def features(own, k, s):
        if LAYOUT["mirror"]:
            mirrored = (k & 7) >= 4
            s = np.where(mirrored, s ^ 7, s)
            k = np.where(mirrored, k ^ 7, k)
        return LAYOUT["map"][k] * 768 + np.where(color == own, 0, 384) + pt * 64 + s

    white_f = features(0, wk[rows], sqs)
    black_f = features(1, bk[rows] ^ 56, sqs ^ 56)
    stm = batch[:, 27].astype(np.int64)
    stm_rows = stm[rows]
    f_stm = np.where(stm_rows == 0, white_f, black_f)
    f_nstm = np.where(stm_rows == 0, black_f, white_f)
    offsets = np.searchsorted(rows, np.arange(b))
    count = np.bincount(rows, minlength=b)
    ob = LAYOUT["output_buckets"]
    out_bucket = np.minimum(ob - 1, np.maximum(count - 2, 0) // -(-32 // ob))
    score = np.frombuffer(np.ascontiguousarray(batch[:, 24:26]).tobytes(), dtype="<i2").astype(np.float32)
    result = batch[:, 26].astype(np.float32) / 2.0
    score_stm = np.where(stm == 0, score, -score)
    result_stm = np.where(stm == 0, result, 1.0 - result)
    return f_stm, f_nstm, offsets, out_bucket, score_stm, result_stm


class Net(nn.Module):
    """(768 x input buckets -> H) x 2 -> output buckets, squared clipped ReLU."""

    def __init__(self, hidden):
        super().__init__()
        self.hidden = hidden
        bound = 1.0 / math.sqrt(768)
        self.ft = nn.EmbeddingBag(LAYOUT["input_buckets"] * 768, hidden, mode="sum")
        nn.init.uniform_(self.ft.weight, -bound, bound)
        self.ft_bias = nn.Parameter(torch.empty(hidden).uniform_(-bound, bound))
        self.out = nn.Linear(2 * hidden, LAYOUT["output_buckets"])

    def forward(self, stm, nstm, offsets, bucket):
        a = torch.clamp(self.ft(stm, offsets) + self.ft_bias, 0.0, 1.0).pow(2)
        b = torch.clamp(self.ft(nstm, offsets) + self.ft_bias, 0.0, 1.0).pow(2)
        return self.out(torch.cat([a, b], dim=1)).gather(1, bucket.unsqueeze(1)).squeeze(1)


def to_inputs(dec, device):
    f_stm, f_nstm, offsets, bucket, score, result = dec
    t = lambda x: torch.from_numpy(np.ascontiguousarray(x)).to(device)
    return (t(f_stm), t(f_nstm), t(offsets), t(bucket)), t(score), t(result)


def export(net, path):
    """Quantise and write the network: version 1 when unbucketed, else version 2."""
    q16 = lambda x, s: (x.detach().cpu() * s).round().clamp(-32768, 32767).to(torch.int16).numpy().astype("<i2")
    ftw = q16(net.ft.weight, QA)
    ftb = q16(net.ft_bias, QA)
    ow = q16(net.out.weight, QB)
    ob = (net.out.bias.detach().cpu() * QA * QB).round().to(torch.int64).numpy().astype("<i4")
    ib, obk = LAYOUT["input_buckets"], LAYOUT["output_buckets"]
    with open(path, "wb") as f:
        f.write(b"APNN")
        if ib == 1 and obk == 1:
            f.write(struct.pack("<II", 1, net.hidden))
        else:
            f.write(struct.pack("<IIIII", 2, net.hidden, ib, obk, 1 if LAYOUT["mirror"] else 0))
            f.write(LAYOUT["map"].astype(np.uint8).tobytes())
        f.write(ftw.tobytes())
        f.write(ftb.tobytes())
        f.write(ow.tobytes())
        f.write(ob.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--input-buckets", type=int, default=1, help="1 or 8 (king buckets, mirrored)")
    ap.add_argument("--output-buckets", type=int, default=1, help="output heads chosen by piece count")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=16384)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--wdl", type=float, default=0.3, help="weight of the game result in the target")
    ap.add_argument("--val-every", type=int, default=100, help="hold out every N-th block for validation")
    ap.add_argument("--max-positions", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4, help="parallel record decoders")
    ap.add_argument("--threads", type=int, default=4, help="PyTorch CPU threads")
    ap.add_argument("--manifest", help="JSON mapping of input files to frozen byte lengths")
    args = ap.parse_args()
    if args.workers < 1 or args.threads < 1 or args.epochs < 1 or args.batch < 1:
        ap.error("worker, thread, epoch and batch counts must be positive")
    if not 8 <= args.hidden <= 8192 or args.hidden % 8:
        ap.error("hidden width must be a multiple of eight between 8 and 8192")
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    set_layout(args.input_buckets, args.output_buckets)

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    file_sizes = json.load(open(args.manifest)) if args.manifest else None
    data = Dataset(list(file_sizes) if file_sizes is not None else args.data,
                   args.val_every, args.max_positions, file_sizes)
    if data.n_train < args.batch or data.n_val < args.batch:
        sys.exit("not enough training and validation data for one batch")
    print(f"hidden {args.hidden}, input buckets {args.input_buckets}, output buckets {args.output_buckets}, device {device}", flush=True)

    net = Net(args.hidden).to(device)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=0.0)
    steps_per_epoch = data.n_train // args.batch
    total = steps_per_epoch * args.epochs
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: 0.5 * (1 + math.cos(math.pi * min(s, total) / total)) * 0.99 + 0.01)

    def loss_of(inputs, score, result):
        pred = torch.sigmoid(net(*inputs))
        target = (1 - args.wdl) * torch.sigmoid(score / SCALE) + args.wdl * result
        return torch.mean((pred - target) ** 2)

    def validate():
        net.eval()
        tot, n = 0.0, 0
        with torch.no_grad():
            for b, dec in data.batches(data.val_blocks, args.batch, workers=args.workers):
                tot += float(loss_of(*to_inputs(dec, device))) * b
                n += b
        net.train()
        return tot / max(n, 1)

    best = validate()
    print(f"epoch 0 val {best:.6f}", flush=True)
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        run, cnt = 0.0, 0
        for b, dec in prefetch(data.batches(data.train_blocks, args.batch, rng, workers=args.workers)):
            loss = loss_of(*to_inputs(dec, device))
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            with torch.no_grad():
                net.out.weight.clamp_(-1.98, 1.98)
            run += float(loss.detach())
            cnt += 1
        val = validate()
        rate = cnt * args.batch / (time.time() - t0)
        kept = val < best
        print(f"epoch {epoch} train {run / max(cnt, 1):.6f} val {val:.6f} lr {sched.get_last_lr()[0]:.2e} {rate:,.0f} pos/s{' (best, saved)' if kept else ''}", flush=True)
        if kept:
            # Keep the network from the epoch with the lowest held-out loss.
            best = val
            export(net, args.out)
            torch.save(net.state_dict(), args.out + ".pt")
    print(f"saved {args.out} (val {best:.6f})", flush=True)


if __name__ == "__main__":
    main()
