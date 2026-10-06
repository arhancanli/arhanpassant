"""Gauntlet: one engine build against the milestone opponents, same book and
settings as the 0.12 milestone, then per-opponent comparison with it.

    python3 gauntlet.py run NAME ENGINE [--tc 10+0.1] [--games 200] [--opts k=v ...]
    python3 gauntlet.py report NAME [--tc 10+0.1]
"""
import argparse, json, math, os, subprocess, sys

DATA = os.path.expanduser("~/arhanpassant-data")
CATALOG = f"{DATA}/opponents/catalog.json"
BASE_MILESTONE = f"{DATA}/forge/milestones/e7325f62d71c192789a74044"
BOOK = f"{BASE_MILESTONE}/openings.epd"
ARENA = f"{DATA}/bin/arena"


def opponents():
    cat = json.load(open(CATALOG))
    items = cat["opponents"] if "opponents" in cat else next(v for v in cat.values() if isinstance(v, list))
    return items


def elo(score):
    score = min(max(score, 1e-3), 1 - 1e-3)
    return -400 * math.log10(1 / score - 1)


def run(args):
    out = f"{DATA}/elo/gauntlet/{args.name}"
    os.makedirs(out, exist_ok=True)
    for o in opponents():
        if args.only and o["name"] not in args.only:
            continue
        res = f"{out}/{o['name']}-{args.tc}.json"
        if os.path.exists(res):
            continue
        opp_opts = [f"opt.{k}={v}" for k, v in o.get("options", {}).items() if v != ""]
        cmd = [ARENA, "--engine", "name=ArhanPassant", f"cmd={args.engine}", "opt.Hash=64", "opt.Threads=1",
               *[f"opt.{x}" for x in args.opts],
               "--engine", f"name={o['name']}", f"cmd={o['path']}", *opp_opts,
               "--tc", args.tc, "--book", BOOK, "--concurrency", str(args.concurrency),
               "--games", str(args.games), "--seed", "20261006",
               "--out", res, "--games-out", f"{out}/{o['name']}-{args.tc}.games.jsonl", "--quiet"]
        print(f"{o['name']}: {args.games} games", flush=True)
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        r = json.load(open(res))
        print(f"  +{r['wins']} -{r['losses']} ={r['draws']} score {(r['wins'] + r['draws'] / 2) / r['games']:.3f}", flush=True)
    print("complete", flush=True)


def report(args):
    out = f"{DATA}/elo/gauntlet/{args.name}"
    rows, deltas = [], []
    for o in opponents():
        f = f"{out}/{o['name']}-{args.tc}.json"
        b = f"{BASE_MILESTONE}/{o['name']}-{args.tc}.json"
        if not (os.path.exists(f) and os.path.exists(b)):
            continue
        r, base = json.load(open(f)), json.load(open(b))
        s = (r["wins"] + r["draws"] / 2) / r["games"]
        sb = base["score"]
        d = elo(s) - elo(sb)
        rows.append((o["name"], sb, s, d))
        if 0.08 < sb < 0.92 and 0.08 < s < 0.92:
            deltas.append(d)
    for name, sb, s, d in rows:
        print(f"{name:>16}  0.12: {sb:.3f}  now: {s:.3f}  {d:+6.1f} Elo")
    if deltas:
        print(f"mean Elo change over {len(deltas)} opponents with scores inside 8-92%: {sum(deltas) / len(deltas):+.1f}")
    # CCRL Blitz scale: every opponent scored 20-80% implies a rating; combine with
    # inverse-variance weights (variance of a score from the game count).
    ccrl = {}
    for run in json.load(open(os.path.expanduser("~/arhanpassant-elo/forge/engines.json")))["runs"]:
        for m in run["matches"]:
            ccrl[m["opponent"]] = m["ccrl"]
    ccrl.update({"stockfish-17.1": 3771, "koivisto-9.0": 3632})
    num = den = 0.0
    for o in opponents():
        f = f"{out}/{o['name']}-{args.tc}.json"
        if not os.path.exists(f) or o["name"] not in ccrl:
            continue
        r = json.load(open(f))
        sc = (r["wins"] + r["draws"] / 2) / r["games"]
        if 0.2 <= sc <= 0.8:
            implied = ccrl[o["name"]] + elo(sc)
            var = (400 / math.log(10) / (sc * (1 - sc))) ** 2 * sc * (1 - sc) / r["games"]
            num += implied / var
            den += 1 / var
            print(f"{o['name']:>16}  CCRL {ccrl[o['name']]}  implied {implied:.0f}")
    if den:
        est, se = num / den, (1 / den) ** 0.5
        print(f"CCRL Blitz scale estimate: {est:.0f} (95% interval {est - 1.96 * se:.0f} to {est + 1.96 * se:.0f}, statistics only)")


ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=["run", "report"])
ap.add_argument("name")
ap.add_argument("engine", nargs="?")
ap.add_argument("--tc", default="10+0.1")
ap.add_argument("--games", type=int, default=200)
ap.add_argument("--concurrency", type=int, default=14)
ap.add_argument("--opts", nargs="*", default=[])
ap.add_argument("--only", nargs="*", default=[])
a = ap.parse_args()
run(a) if a.cmd == "run" else report(a)
