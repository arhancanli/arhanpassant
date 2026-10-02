"""Rating from matches against other open-source engines (forge/azure/engines-node.sh).

    python forge/engines_report.py RUN [--min-games N]

Reads every engines/RUN/*.json result in the fleet container, pools them by
opponent, and places ArhanPassant on the CCRL Blitz scale (2'+1"): each
opponent's published rating is read from the CCRL all-versions list, each
match gives an implied rating (opponent + the pentanomial Elo difference),
and the headline is the rating that best fits every match near our level at
once (opponents we score 20-80% against: Elo drifts across big gaps), with a 95% profile interval.
Games are saved under ~/arhanpassant-data/engines/RUN/, with games.tsv ready for `arhanpassant rescore`.
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet  # noqa: E402
import sprt  # noqa: E402

DATA = os.path.expanduser("~/arhanpassant-data/engines")
CCRL_URL = "https://computerchess.org.uk/ccrl/404/rating_list_all.html"


def ccrl_ratings():
    """{lowercased CCRL name: (rating, games)} from the CCRL Blitz all-versions list (cached per day)."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"ccrl404-{dt.date.today().isoformat()}.html")
    if not os.path.exists(path):
        # The site refuses Python's default user agent.
        req = urllib.request.Request(CCRL_URL, headers={"User-Agent": "Mozilla/5.0 (ArhanPassant rating report)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            open(path, "wb").write(r.read())
    page = open(path, encoding="utf-8", errors="replace").read()
    row = re.compile(r'class="name">(.*?)</td><td rowspan=2 class="number">(?:<b>)?(\d{3,4})(?:</b>)?</td>'
                     r'.*?<a href="games-by-engine/[^"]*">(?:<b>)?(\d+)(?:</b>)?</a>', re.S)
    out = {}
    for m in row.finditer(page):
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip().lower()
        out.setdefault(name, (int(m.group(2)), int(m.group(3))))
    return out, path


def ccrl_name(opponent):
    """weiss-1.2 -> 'weiss 1.2 64-bit'; demolito-2021 -> 'demolito 2021-10-04 64-bit'."""
    engine, version = opponent.split("-", 1)
    version = {"demolito": "2021-10-04"}.get(engine, version)
    return f"{engine} {version} 64-bit".lower()


def smoothed(penta):
    """Pair-score mean and standard error, with one virtual drawn pair so 0% and 100% keep a finite error."""
    counts = list(penta)
    counts[2] += 1
    n = sum(counts)
    mean = sum(c * s for c, s in zip(counts, sprt.SCORES)) / n
    var = sum(c * (s - mean) ** 2 for c, s in zip(counts, sprt.SCORES)) / n
    return mean, math.sqrt(var / n)


def fit(matches):
    """Rating R minimising sum(((score - expected(R - opponent)) / se)^2), with its 95% profile interval."""
    def chi2(r):
        return sum(((m["mean"] - sprt.logistic(r - m["ccrl"])) / m["se"]) ** 2 for m in matches)
    grid = [1500 + x * 0.5 for x in range(5001)]
    vals = [chi2(r) for r in grid]
    i = min(range(len(grid)), key=vals.__getitem__)
    inside = [r for r, v in zip(grid, vals) if v <= vals[i] + 1.96 ** 2]
    return grid[i], min(inside), max(inside), vals[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--min-games", type=int, default=20)
    ap.add_argument("--exclude", action="append", default=[], metavar="OPPONENT=REASON",
                    help="leave a match out of the rating, with the reason recorded in the report")
    args = ap.parse_args()
    excluded = dict(e.split("=", 1) if "=" in e else (e, "excluded") for e in args.exclude)

    ratings, ccrl_path = ccrl_ratings()
    pooled = {}
    tcs = set()
    games_dir = os.path.join(DATA, args.run)
    os.makedirs(games_dir, exist_ok=True)
    for name in sorted(fleet.list_names(f"engines/{args.run}/")):
        local = os.path.join(games_dir, os.path.basename(name))
        if name.endswith(".games.txt"):
            if not os.path.exists(local):
                open(local, "wb").write(fleet.get(name))
            continue
        r = json.loads(fleet.get(name))
        tcs.add(r.get("tc", "?"))
        p = pooled.setdefault(r["baseline"], {"games": 0, "wins": 0, "losses": 0, "draws": 0, "penta": [0] * 5, "hosts": 0})
        for k in ("games", "wins", "losses", "draws"):
            p[k] += r[k]
        p["penta"] = [a + b for a, b in zip(p["penta"], r["penta"])]
        p["hosts"] += 1

    rows = []
    for opp, p in sorted(pooled.items()):
        if p["games"] < args.min_games or opp in excluded:
            continue
        key = ccrl_name(opp)
        if key not in ratings:
            print(f"warning: {opp} ({key}) is not on the CCRL list; left out", file=sys.stderr)
            continue
        mean, se = smoothed(p["penta"])
        diff, lo, hi = sprt.elo_estimate(p["penta"])
        rows.append({"opponent": opp, "ccrl": ratings[key][0], "ccrl_games": ratings[key][1], **p,
                     "score": round((p["wins"] + p["draws"] / 2) / p["games"], 4),
                     "elo_diff": round(diff, 1), "implied": round(ratings[key][0] + diff),
                     "implied_lo": round(ratings[key][0] + lo), "implied_hi": round(ratings[key][0] + hi),
                     "mean": mean, "se": se})
    if not rows:
        print("no results yet")
        return
    total = sum(r["games"] for r in rows)
    # Elo is not transitive across big gaps between engines: lopsided matches imply ratings that drift
    # with the gap (rating lists pool mostly near-equal games). Fit only the matches scored 20-80%.
    near = [r for r in rows if 0.2 <= r["score"] <= 0.8]
    est, lo, hi, chi = fit(near or rows)
    doc = {
        "run": args.run,
        "date": dt.date.today().isoformat(),
        "tc": "/".join(sorted(tcs)),
        "scale": "CCRL Blitz (2'+1\"), all-versions list " + os.path.basename(ccrl_path),
        "method": "Matches on Azure VMs, 1 thread and 64 MB hash each, 8moves_v3 openings with colours reversed; "
                  "rating fitted to every opponent scored 20-80% against, 95% profile interval",
        "chi2_dof": max(len(near or rows) - 1, 1),
        "estimate": round(est), "ci95": [round(lo), round(hi)], "games": total,
        "fit_opponents": [r["opponent"] for r in (near or rows)], "chi2": round(chi, 2),
        "matches": [{k: v for k, v in r.items() if k not in ("mean", "se")} for r in rows],
        "excluded": [{"opponent": o, "reason": why, **{k: pooled[o][k] for k in ("games", "wins", "losses", "draws")}}
                     for o, why in excluded.items() if o in pooled],
    }
    # Training input for `arhanpassant rescore`: result, starting position and moves per game.
    tsv = os.path.join(games_dir, "games.tsv")
    with open(tsv, "w") as f:
        for name in sorted(os.listdir(games_dir)):
            if name.endswith(".games.txt"):
                for line in open(os.path.join(games_dir, name)):
                    if line.strip():
                        g = json.loads(line)
                        f.write(f"{g['result']}\t{g['fen']}\t{g['moves']}\n")
    out = os.path.join(DATA, f"{args.run}.json")
    with open(out, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"{args.run}: {total:,} games; CCRL Blitz estimate {doc['estimate']} [{doc['ci95'][0]}, {doc['ci95'][1]}]"
          f" from {len(near or rows)} opponents (chi2 {chi:.1f})")
    for r in sorted(rows, key=lambda r: r["ccrl"]):
        print(f"  {r['opponent']:<16} CCRL {r['ccrl']}  +{r['wins']} -{r['losses']} ={r['draws']}"
              f"  {100 * r['score']:5.1f}%  implied {r['implied']} [{r['implied_lo']}, {r['implied_hi']}]")


if __name__ == "__main__":
    main()
