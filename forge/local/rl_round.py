"""One reinforcement-learning round: train a candidate network on fresh
self-play (including seeded 'active' games) starting from the champion, then
queue an SPRT of candidate vs champion (same engine binary, EvalFile differs).

    python3 rl_round.py --name rl1 --data selfplay/gen11 selfplay/gen12 selfplay/active1 \
        --engine bin/ap-xxx --epochs 4 --lr 2e-4 [--factorize] [--queue]
"""
import argparse, os, subprocess, sys, time

DATA = os.path.expanduser("~/arhanpassant-data")
REPO = os.path.expanduser("~/arhanpassant-elo")
PY = os.path.expanduser("~/arhanpassant/.venv/bin/python")

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)
ap.add_argument("--data", nargs="+", required=True)
ap.add_argument("--champion", default=f"{DATA}/nets/champion-0.12.0.nnue")
ap.add_argument("--engine", required=True)
ap.add_argument("--epochs", type=int, default=4)
ap.add_argument("--lr", type=float, default=2e-4)
ap.add_argument("--wdl", type=float, default=0.3)
ap.add_argument("--factorize", action="store_true")
ap.add_argument("--queue", action="store_true", help="queue the SPRT after training")
ap.add_argument("--opts", default="opt.corr_joint=1 opt.corr_cont=128")
a = ap.parse_args()

out = f"{DATA}/nets/{a.name}.nnue"
log = f"{DATA}/nets/{a.name}.log"
cmd = [PY, f"{REPO}/trainer/train.py", "--data", *[os.path.join(DATA, d) for d in a.data],
       "--hidden", "512", "--input-buckets", "8", "--output-buckets", "8",
       "--epochs", str(a.epochs), "--lr", str(a.lr), "--wdl", str(a.wdl),
       "--init-nnue", a.champion, "--out", out, "--workers", "4", "--threads", "4"]
if a.factorize:
    cmd.append("--factorize")
print(" ".join(cmd), flush=True)
with open(log, "w") as f:
    rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
print(open(log).read()[-1500:], flush=True)
if rc != 0:
    sys.exit(f"training failed ({rc})")
if a.queue:
    q = f"{DATA}/elo/queue/{int(time.time())}-{a.name}.sh"
    with open(q, "w") as f:
        f.write(f'B={a.engine}\nCAND_OPTS="opt.EvalFile={out}" BASE_OPTS="opt.EvalFile={a.champion}" '
                f'~/arhanpassant-data/elo/sprt2.sh {a.name} $B $B 5+0.05 0 5 14 {a.opts}\n')
    print(f"queued {q}", flush=True)
