"""Daily games in every time control against Stockfish at fixed strengths.

    python forge/gauntlet.py [--target 100] [--hours 8] [--concurrency 3]

Lichess allows a bot only 100 games a day against other bots, across all time
controls, so this plays the rest of the target on our own machine: for each
of bullet, blitz, rapid and classical, up to TARGET games against Stockfish 19
at several UCI_Elo levels, the modes interleaved in small batches so each one
progresses, at low priority (the Lichess bot comes first), until the targets
are met or the time box ends. Every game is scored into training records
(~/arhanpassant-data/selfplay/gauntlet/DAY-MODE.bin) and the day's results per
mode and level go to ~/arhanpassant-data/gauntlet/summary.json.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import time

DATA = os.path.expanduser("~/arhanpassant-data")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(DATA, "bin", "ap-current")
ARENA = os.path.join(ROOT, "target", "release", "arena")
STOCKFISH = os.path.join(DATA, "anchors", "stockfish", "stockfish-macos-universal")
BOOK = os.path.join(ROOT, "tools", "books", "2moves_v1.epd")
MODES = {"bullet": "60+0", "blitz": "180+2", "rapid": "600+5", "classical": "1800+20"}
LEVELS = [2800, 3000, 3190]  # 3190 is Stockfish's strongest limited level
BATCH = 4  # games per batch: two colour-swapped pairs from one opening each


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def implied(level, score, games):
    """Rating implied by a score against a fixed level (clamped away from 0% and 100%)."""
    s = min(max(score / games, 0.5 / games), 1 - 0.5 / games)
    return round(level - 400 * math.log10(1 / s - 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=100, help="games per mode")
    ap.add_argument("--hours", type=float, default=8.0, help="time box")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--modes", default=",".join(MODES))
    args = ap.parse_args()

    day = dt.datetime.now(dt.timezone.utc).date()
    state = json.load(open(os.path.join(DATA, "forge", "state.json")))
    accepted = json.load(open(os.path.join(DATA, "forge", "search.json")))["accepted"]
    net = state["champion_net"]
    work = os.path.join(DATA, "gauntlet", str(day))
    os.makedirs(work, exist_ok=True)
    modes = [m for m in args.modes.split(",") if m in MODES]
    played = {m: {lv: {"games": 0, "score": 0.0} for lv in LEVELS} for m in modes}
    deadline = time.time() + args.hours * 3600
    batch = 0
    while time.time() < deadline:
        todo = [m for m in modes if sum(x["games"] for x in played[m].values()) < args.target]
        if not todo:
            break
        for mode in todo:
            if time.time() >= deadline:
                break
            level = LEVELS[batch % len(LEVELS)]
            batch += 1
            out, games_out = os.path.join(work, f"r{batch}.json"), os.path.join(work, f"{mode}.ndjson.part")
            cmd = ["nice", "-n", "15", ARENA,
                   "--engine", "name=arhanpassant", f"cmd={ENGINE}", f"opt.EvalFile={net}", *[f"opt.{k}={v}" for k, v in accepted.items()],
                   "--engine", f"name=stockfish-{level}", f"cmd={STOCKFISH}", "opt.UCI_LimitStrength=true", f"opt.UCI_Elo={level}",
                   "--tc", MODES[mode], "--book", BOOK, "--concurrency", str(min(args.concurrency, BATCH)), "--games", str(BATCH),
                   "--seed", str(int(time.time()) + batch), "--quiet", "--out", out, "--games-out", games_out]
            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                log(f"{mode} vs {level}: the match failed; stopping")
                deadline = 0
                break
            r = json.load(open(out))
            os.remove(out)
            with open(games_out) as src, open(os.path.join(work, f"{mode}.ndjson"), "a") as dst:
                dst.write(src.read())
            os.remove(games_out)
            p = played[mode][level]
            p["games"] += r["games"]
            p["score"] += r["wins"] + r["draws"] / 2

    # Training records from every game, scored like self-play.
    out_dir = os.path.join(DATA, "selfplay", "gauntlet")
    os.makedirs(out_dir, exist_ok=True)
    positions = {}
    for mode in modes:
        path = os.path.join(work, f"{mode}.ndjson")
        if not os.path.exists(path):
            continue
        lines = []
        for line in open(path):
            g = json.loads(line)
            lines.append(f"{g['result']}\t{g['fen']}\t{g['moves']}")
        txt = os.path.join(work, f"{mode}.games.txt")
        with open(txt, "w") as f:
            f.write("\n".join(lines) + "\n")
        binf = os.path.join(out_dir, f"{day}-{mode}.bin")
        if os.path.exists(binf):
            os.remove(binf)
        r = subprocess.run(["nice", "-n", "15", ENGINE, "rescore", "--in", txt, "--out", binf, "--nodes", "5000", "--threads", "4",
                            "--net", net], capture_output=True, text=True, check=True)
        positions[mode] = int(re.search(r"positions (\d+)", r.stdout).group(1))

    entry = {"date": str(day), "network": os.path.basename(net), "settings": accepted, "byMode": {}}
    for mode in modes:
        levels = {str(lv): {**p, "implied": implied(lv, p["score"], p["games"]) if p["games"] else None} for lv, p in played[mode].items()}
        entry["byMode"][mode] = {"tc": MODES[mode], "games": sum(p["games"] for p in played[mode].values()),
                                 "positions": positions.get(mode, 0), "levels": levels}
    path = os.path.join(DATA, "gauntlet", "summary.json")
    summary = json.load(open(path)) if os.path.exists(path) else {"days": []}
    summary["days"] = [d for d in summary["days"] if d["date"] != str(day)] + [entry]
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
        f.write("\n")
    log(json.dumps({m: e["games"] for m, e in entry["byMode"].items()}))


if __name__ == "__main__":
    main()
