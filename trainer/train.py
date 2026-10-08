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

import initialize
from stream import complete_batches, frozen_record_count, prefetch, prepare_file_limit

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


def buckets16():
    """16 king buckets, each inside one of the 8 default buckets: the back
    rank by file pair as before, the second rank by file, ranks 3 and 4 and the
    pairs 5-6 and 7-8 each split into queen side and king side."""
    m = np.zeros(64, dtype=np.int64)
    for sq in range(64):
        file, rank = sq % 8, sq // 8
        f = file if file < 4 else 7 - file
        side = int(f >= 2)
        m[sq] = (f if rank == 0 else 4 + f if rank == 1 else 8 + side if rank == 2 else 10 + side if rank == 3
                 else 12 + side if rank in (4, 5) else 14 + side)
    return m


# Network layout; set from the command line before anything is decoded.
LAYOUT = {"input_buckets": 1, "output_buckets": 1, "mirror": False, "map": np.zeros(64, dtype=np.int64)}


def set_layout(input_buckets, output_buckets):
    LAYOUT["input_buckets"] = input_buckets
    LAYOUT["output_buckets"] = output_buckets
    LAYOUT["mirror"] = input_buckets > 1
    LAYOUT["map"] = {1: np.zeros(64, dtype=np.int64), 8: default_buckets(), 16: buckets16()}.get(input_buckets)
    if LAYOUT["map"] is None:
        sys.exit("--input-buckets must be 1, 8 or 16")


BLOCK = 4096  # records read contiguously; blocks are shuffled, then records within a buffer


def data_files(paths):
    """Record files named directly or found (recursively) in named directories."""
    files = []
    for p in paths:
        p = os.path.expanduser(p)
        files += sorted(glob.glob(os.path.join(p, "**", "*.bin"), recursive=True)) if os.path.isdir(p) else [p]
    return files


class Dataset:
    """Self-play records memory-mapped from many files, read in shuffled blocks.

    Files can be far larger than memory: each epoch visits every block once in
    random order and shuffles records inside a buffer of many blocks. Every
    `val_every`-th block is held out for validation, so the split is the same
    in every run and never mixes with training. With `val_files`, only blocks of
    those files are held out (every `val_every`-th of them); all others train.
    """

    def __init__(self, paths, val_every=100, max_positions=0, file_sizes=None, val_files=None):
        files = data_files(paths)
        if not files:
            sys.exit("no data files found")
        prepare_file_limit(len(files))
        eligible = None if val_files is None else {os.path.realpath(f) for f in data_files(val_files)}
        self.maps, blocks, holds = [], [], []
        total = 0
        for f in files:
            n = frozen_record_count(os.path.getsize(f), file_sizes[f] if file_sizes is not None else None, REC)
            if n == 0:
                continue
            if max_positions and total + n > max_positions:
                n = max_positions - total
            m = np.memmap(f, dtype=np.uint8, mode="r", shape=(n * REC,)).reshape(n, REC)
            fi = len(self.maps)
            self.maps.append(m)
            blocks += [(fi, s, min(BLOCK, n - s)) for s in range(0, n, BLOCK)]
            holds.append(eligible is None or os.path.realpath(f) in eligible)
            total += n
            if max_positions and total >= max_positions:
                break
        candidates = [b for b in blocks if holds[b[0]]]
        held = {b for i, b in enumerate(candidates) if i % val_every == val_every - 1}
        self.val_blocks = [b for b in blocks if b in held]
        self.train_blocks = [b for b in blocks if b not in held]
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
        def buffers():
            for i in range(0, len(order), buffer_blocks):
                buf = np.concatenate([self.read(b) for b in order[i : i + buffer_blocks]])
                if rng is not None:
                    buf = buf[rng.permutation(len(buf))]
                yield buf
        yield from complete_batches(buffers(), batch_size, np.concatenate)

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
    """(768 x input buckets -> H) x 2 -> output buckets, squared clipped ReLU.

    With `factorize`, every king bucket's weights are the sum of its own and a
    shared 768-feature table, so what a piece on a square means is learned
    once for all buckets; export merges the two (the engine format is unchanged).
    """

    def __init__(self, hidden, factorize=False):
        super().__init__()
        self.hidden = hidden
        bound = 1.0 / math.sqrt(768)
        self.ft = nn.EmbeddingBag(LAYOUT["input_buckets"] * 768, hidden, mode="sum")
        nn.init.uniform_(self.ft.weight, -bound, bound)
        self.ft_bias = nn.Parameter(torch.empty(hidden).uniform_(-bound, bound))
        self.out = nn.Linear(2 * hidden, LAYOUT["output_buckets"])
        self.factor = None
        if factorize and LAYOUT["input_buckets"] > 1:
            self.factor = nn.EmbeddingBag(768, hidden, mode="sum")
            nn.init.zeros_(self.factor.weight)

    def accumulate(self, idx, offsets):
        a = self.ft(idx, offsets)
        if self.factor is not None:
            a = a + self.factor(torch.remainder(idx, 768), offsets)
        return a + self.ft_bias

    def forward(self, stm, nstm, offsets, bucket):
        a = torch.clamp(self.accumulate(stm, offsets), 0.0, 1.0).pow(2)
        b = torch.clamp(self.accumulate(nstm, offsets), 0.0, 1.0).pow(2)
        return self.out(torch.cat([a, b], dim=1)).gather(1, bucket.unsqueeze(1)).squeeze(1)

    def merged_ft_weight(self):
        w = self.ft.weight
        if self.factor is not None:
            w = w + self.factor.weight.repeat(LAYOUT["input_buckets"], 1)
        return w


def to_inputs(dec, device):
    f_stm, f_nstm, offsets, bucket, score, result = dec
    t = lambda x: torch.from_numpy(np.ascontiguousarray(x)).to(device)
    return (t(f_stm), t(f_nstm), t(offsets), t(bucket)), t(score), t(result)


def export(net, path):
    """Quantise and write the network: version 1 when unbucketed, else version 2."""
    q16 = lambda x, s: (x.detach().cpu() * s).round().clamp(-32768, 32767).to(torch.int16).numpy().astype("<i2")
    ftw = q16(net.merged_ft_weight(), QA)
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


def load_nnue_into(net, path):
    """Dequantise an engine network (same layout) into `net` as a starting point."""
    b = open(path, "rb").read()
    if b[:4] != b"APNN":
        sys.exit(f"{path}: not an ArhanPassant network")
    version, hidden = struct.unpack("<II", b[4:12])
    off = 12
    ib, obk = 1, 1
    file_map = np.zeros(64, dtype=np.int64)
    if version == 2:
        ib, obk, _flags = struct.unpack("<III", b[12:24])
        file_map = np.frombuffer(b, dtype=np.uint8, count=64, offset=24).astype(np.int64)
        off = 24 + 64
    # A wider run starts from k copies of every neuron with output weights / k:
    # the same function (up to the small symmetry-breaking noise), more capacity.
    # More king buckets: each new bucket starts as the file's bucket for the same
    # king squares, which is exact when the new buckets subdivide the old ones.
    run_ib = LAYOUT["input_buckets"]
    source_of = np.zeros(run_ib, dtype=np.int64)
    for sq in range(64):
        source_of[LAYOUT["map"][sq]] = file_map[sq]
    for sq in range(64):
        if file_map[sq] != source_of[LAYOUT["map"][sq]]:
            sys.exit(f"{path}: this run's king buckets do not subdivide the file's")
    if net.hidden % hidden or obk != LAYOUT["output_buckets"]:
        sys.exit(f"{path}: layout {hidden}/{ib}/{obk} does not match this run")
    k = net.hidden // hidden
    def take(n, dtype):
        nonlocal off
        a = np.frombuffer(b, dtype=dtype, count=n, offset=off)
        off += a.nbytes
        return torch.from_numpy(a.astype(np.float32))
    with torch.no_grad():
        ftw = take(ib * 768 * hidden, "<i2").reshape(ib * 768, hidden) / QA
        ftb = take(hidden, "<i2") / QA
        ow = take(obk * 2 * hidden, "<i2").reshape(obk, 2, hidden)
        ftw = ftw.reshape(ib, 768, hidden)[torch.from_numpy(source_of)].reshape(run_ib * 768, hidden)
        net.ft.weight.copy_(ftw.repeat(1, k))
        net.ft_bias.copy_(ftb.repeat(k))
        # Split each quantised output weight into k integers with the same sum
        # (copy j gets floor(w/k), plus 1 for the first w mod k copies), so the
        # widened network exports to exactly the same evaluation.
        q = torch.div(ow, k, rounding_mode="floor")
        r = ow - q * k
        parts = [q + (r > j).float() for j in range(k)]
        net.out.weight.copy_((torch.cat(parts, dim=2) / QB).reshape(obk, 2 * net.hidden))
        net.out.bias.copy_(take(obk, "<i4") / (QA * QB))
        if k > 1:
            net.ft.weight[:, hidden:] += torch.randn_like(net.ft.weight[:, hidden:]) * 1e-4
        if net.factor is not None:
            net.factor.weight.zero_()
    if off != len(b):
        sys.exit(f"{path}: size mismatch")


def engine_copy(net):
    """`net` with every layer rounded as export writes it: the evaluation the engine computes."""
    q = Net(net.hidden)
    with torch.no_grad():
        q.ft.weight.copy_(torch.round(net.merged_ft_weight().detach().cpu() * QA).clamp(-32768, 32767) / QA)
        q.ft_bias.copy_(torch.round(net.ft_bias.detach().cpu() * QA).clamp(-32768, 32767) / QA)
        q.out.weight.copy_(torch.round(net.out.weight.detach().cpu() * QB).clamp(-32768, 32767) / QB)
        q.out.bias.copy_(torch.round(net.out.bias.detach().cpu() * QA * QB) / (QA * QB))
    return q


def gptq_output(net, data, device, positions, damp=0.01, seed=7):
    """Output weights rounded to the engine grid with GPTQ (Frantar et al. 2022) instead of to nearest, and biases
    that absorb each bucket's remaining mean error.

    Rounding to nearest costs more than the whole feature layer's rounding: with QB = 64 it adds several centipawns
    of noise per position and a fixed offset per bucket. For each bucket, H = E[x x^T] over training positions in
    that bucket (x: the squared activations as the engine computes them); weights are rounded one at a time, most
    active input first, and each rounding error is pushed onto the weights not yet rounded through H^-1, so the
    rounded layer reproduces the float layer's output on typical positions rather than weight by weight.
    """
    q = engine_copy(net).to(device).eval()
    d, nb = 2 * net.hidden, LAYOUT["output_buckets"]
    H = torch.zeros(nb, d, d, device=device)
    S = torch.zeros(nb, d, device=device)
    N = torch.zeros(nb, device=device)
    seen = 0
    with torch.no_grad():
        for b, dec in data.batches(data.train_blocks, 16384, np.random.default_rng(seed)):
            (stm, nstm, offsets, bucket), _, _ = to_inputs(dec, device)
            x = torch.cat([torch.clamp(q.accumulate(stm, offsets), 0.0, 1.0).pow(2),
                           torch.clamp(q.accumulate(nstm, offsets), 0.0, 1.0).pow(2)], dim=1)
            for k in range(nb):
                xk = x[bucket == k]
                if len(xk):
                    H[k] += xk.T @ xk
                    S[k] += xk.sum(0)
                    N[k] += len(xk)
            seen += b
            if seen >= positions:
                break
    H, S, N = H.cpu().double(), S.cpu().double(), N.cpu().double()
    w_float = net.out.weight.detach().cpu().double()
    Q = torch.zeros_like(w_float)
    for k in range(nb):
        Hk = H[k] / max(float(N[k]), 1.0)
        diag = torch.diagonal(Hk).clone()
        Hk = Hk + torch.eye(d, dtype=torch.float64) * (damp * float(diag.mean()) + 1e-12)
        order = torch.argsort(diag, descending=True)
        U = torch.linalg.cholesky(torch.cholesky_inverse(torch.linalg.cholesky(Hk[order][:, order])), upper=True)
        w = w_float[k][order].clone()
        qk = torch.zeros(d, dtype=torch.float64)
        for i in range(d):
            qk[i] = torch.clamp(torch.round(w[i] * QB), -127, 127) / QB
            w[i + 1:] -= (w[i] - qk[i]) / U[i, i] * U[i, i + 1:]
        Q[k][order] = qk
        dead = diag == 0  # never active here: nothing to compensate, round to nearest
        Q[k][dead] = torch.clamp(torch.round(w_float[k][dead] * QB), -127, 127) / QB
    mean_x = S / N.clamp(min=1).unsqueeze(1)
    bias = net.out.bias.detach().cpu().double() - ((Q - w_float) * mean_x).sum(1)
    return Q.float(), bias.float()


def held_out_loss(model, vdata, batch, workers, device, wdl, power):
    model.eval()
    tot, n = 0.0, 0
    with torch.no_grad():
        for b, dec in vdata.batches(vdata.val_blocks, batch, workers=workers):
            inputs, score, result = to_inputs(dec, device)
            pred = torch.sigmoid(model(*inputs))
            target = (1 - wdl) * torch.sigmoid(score / SCALE) + wdl * result
            tot += float(torch.mean(torch.abs(pred - target) ** power)) * b
            n += b
    return tot / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--input-buckets", type=int, default=1, help="1 or 8 (king buckets, mirrored)")
    ap.add_argument("--output-buckets", type=int, default=1, help="output heads chosen by piece count")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=16384)
    ap.add_argument("--lr", type=float, help="default: 1e-3 from scratch, 1e-5 from a verified checkpoint")
    ap.add_argument("--init-checkpoint", help="initial PyTorch state dict; requires --init-network")
    ap.add_argument("--init-network", help="NNUE which the initialization checkpoint must reproduce byte for byte")
    ap.add_argument("--wdl", type=float, default=0.3, help="weight of the game result in the target")
    ap.add_argument("--factorize", action="store_true", help="shared piece-square table added to every king bucket")
    ap.add_argument("--init-nnue", help="start from this engine network (dequantised)")
    ap.add_argument("--power", type=float, default=2.0, help="loss exponent |prediction - target|^power")
    ap.add_argument("--val-every", type=int, default=100, help="hold out every N-th block for validation")
    ap.add_argument("--val-files", nargs="+", help="hold out blocks only from these files or directories, for example the "
                    "fresh round when earlier rounds are replayed: the starting network has trained on those, so their "
                    "blocks would favour it over every epoch")
    ap.add_argument("--max-positions", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4, help="parallel record decoders")
    ap.add_argument("--threads", type=int, default=4, help="PyTorch CPU threads")
    ap.add_argument("--manifest", help="JSON mapping of input files to frozen byte lengths")
    ap.add_argument("--val-data", nargs="+", help="validate on every record of these files instead of the held-out blocks "
                    "(a fixed set lets runs on different data be compared; keep these files out of --data)")
    ap.add_argument("--warmup", type=int, default=0, help="steps of linear learning-rate warm-up before the cosine decay")
    ap.add_argument("--gptq", type=int, default=2_000_000, help="round the kept epoch's output layer with GPTQ, "
                    "calibrated on this many training positions, when that beats rounding to nearest on the held-out "
                    "positions (0 = always round to nearest)")
    args = ap.parse_args()
    if bool(args.init_checkpoint) != bool(args.init_network):
        ap.error("--init-checkpoint and --init-network must be supplied together")
    if args.lr is None:
        args.lr = 1e-5 if args.init_checkpoint else 1e-3
    if not math.isfinite(args.lr) or args.lr < 0:
        ap.error("learning rate must be finite and nonnegative")
    if args.workers < 1 or args.threads < 1 or args.epochs < 1 or args.batch < 1:
        ap.error("worker, thread, epoch and batch counts must be positive")
    if not 8 <= args.hidden <= 8192 or args.hidden % 8:
        ap.error("hidden width must be a multiple of eight between 8 and 8192")
    if args.init_checkpoint:
        initialize.protect_inputs(args.init_checkpoint, args.init_network, args.out)
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    set_layout(args.input_buckets, args.output_buckets)

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    file_sizes = None
    if args.manifest:
        with open(args.manifest) as manifest:
            file_sizes = json.load(manifest)
    data = Dataset(list(file_sizes) if file_sizes is not None else args.data,
                   args.val_every, args.max_positions, file_sizes, args.val_files)
    if data.n_train < args.batch or data.n_val < args.batch:
        sys.exit("not enough training and validation data for one batch")
    vdata = data
    if args.val_data:
        if set(map(os.path.realpath, data_files(args.val_data))) & set(map(os.path.realpath, data_files(list(file_sizes) if file_sizes is not None else args.data))):
            sys.exit("--val-data files are also in the training data")
        print("validation set:", end=" ", flush=True)
        vdata = Dataset(args.val_data, 1)
    print(f"hidden {args.hidden}, input buckets {args.input_buckets}, output buckets {args.output_buckets}, device {device}", flush=True)

    net = Net(args.hidden, factorize=args.factorize)
    if args.init_nnue:
        load_nnue_into(net, args.init_nnue)
        print(f"initialised from {args.init_nnue}", flush=True)
    initialization = None
    if args.init_checkpoint:
        initialization = initialize.restore(net, args.init_checkpoint, args.init_network, args.out, export)
        initialization.update(status="training", hidden=args.hidden, input_buckets=args.input_buckets,
                              output_buckets=args.output_buckets, learning_rate=args.lr, seed=args.seed,
                              device=str(device), train_records=data.n_train, validation_records=data.n_val)
        with open(args.out + ".init.json", "w") as proof:
            json.dump(initialization, proof, indent=2)
            proof.write("\n")
        print("initialization: checkpoint reproduces supplied NNUE byte for byte", flush=True)
    net = net.to(device)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=0.0)
    steps_per_epoch = -(-data.n_train // args.batch)
    total = steps_per_epoch * args.epochs
    warm = min(args.warmup, total - 1)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / (warm + 1)) * (0.5 * (1 + math.cos(math.pi * min(s, total) / total)) * 0.99 + 0.01))

    def loss_of(inputs, score, result):
        pred = torch.sigmoid(net(*inputs))
        target = (1 - args.wdl) * torch.sigmoid(score / SCALE) + args.wdl * result
        if args.power == 2.0:
            return torch.mean((pred - target) ** 2)
        return torch.mean(torch.abs(pred - target) ** args.power)

    def validate():
        net.eval()
        tot, n = 0.0, 0
        with torch.no_grad():
            for b, dec in vdata.batches(vdata.val_blocks, args.batch, workers=args.workers):
                value = float(loss_of(*to_inputs(dec, device)))
                if not math.isfinite(value):
                    raise RuntimeError("nonfinite validation loss")
                tot += value * b
                n += b
        if n != vdata.n_val:
            raise RuntimeError(f"validation covered {n} of {vdata.n_val} frozen records")
        net.train()
        return tot / n

    best = validate()
    best_epoch = 0
    # The initial model is the best checkpoint until an epoch improves on it.
    export(net, args.out)
    torch.save(net.state_dict(), args.out + ".pt")
    print(f"epoch 0 val {best:.6f}", flush=True)
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        run, cnt, records = 0.0, 0, 0
        for b, dec in prefetch(data.batches(data.train_blocks, args.batch, rng, workers=args.workers)):
            loss = loss_of(*to_inputs(dec, device))
            value = float(loss.detach())
            if not math.isfinite(value):
                raise RuntimeError("nonfinite training loss")
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            with torch.no_grad():
                net.out.weight.clamp_(-1.98, 1.98)
            run += value * b
            cnt += 1
            records += b
        if records != data.n_train or cnt != steps_per_epoch:
            raise RuntimeError(f"training covered {records} of {data.n_train} frozen records in {cnt} of {steps_per_epoch} steps")
        val = validate()
        rate = records / (time.time() - t0)
        kept = val < best
        print(f"epoch {epoch} train {run / records:.6f} val {val:.6f} lr {sched.get_last_lr()[0]:.2e} {rate:,.0f} pos/s records {records:,}/{data.n_train:,} steps {cnt}/{steps_per_epoch}{' (best, saved)' if kept else ''}", flush=True)
        if kept:
            # Keep the network from the epoch with the lowest held-out loss.
            best = val
            best_epoch = epoch
            export(net, args.out)
            torch.save(net.state_dict(), args.out + ".pt")
    if args.gptq and best_epoch > 0:
        # The kept epoch, rounded for the engine two ways: to nearest (written above) or with GPTQ.
        net.load_state_dict(torch.load(args.out + ".pt", map_location=device, weights_only=True))
        w, b = gptq_output(net, data, device, args.gptq)
        tuned = engine_copy(net)
        with torch.no_grad():
            tuned.out.weight.copy_(w)
            tuned.out.bias.copy_(torch.round(b * QA * QB) / (QA * QB))
        losses = [held_out_loss(m.to(device), vdata, args.batch, args.workers, device, args.wdl, args.power)
                  for m in (engine_copy(net), tuned)]
        print(f"engine rounding on held-out positions: nearest {losses[0]:.6f}, gptq {losses[1]:.6f}", flush=True)
        if losses[1] < losses[0]:
            export(tuned.cpu(), args.out)
            print("exported with gptq output rounding", flush=True)
    if initialization is not None:
        initialize.verify_sources(initialization)
        initialization.update(status="complete", epochs_completed=args.epochs, best_epoch=best_epoch,
                              best_validation_mse=best, output_sha256=initialize.digest(args.out),
                              checkpoint_sha256=initialize.digest(args.out + ".pt"))
        with open(args.out + ".init.json", "w") as proof:
            json.dump(initialization, proof, indent=2)
            proof.write("\n")
    print(f"saved {args.out} (val {best:.6f})", flush=True)


if __name__ == "__main__":
    main()
