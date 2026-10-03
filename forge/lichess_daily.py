"""Collect a day of the Lichess bot's games and turn them into training data.

    python forge/lichess_daily.py [YYYY-MM-DD]      # default: yesterday (UTC)

Downloads every game the bot (lichess.org/@/arhanpassant) finished that UTC
day, keeps the PGN, scores each position with the current champion network
(`arhanpassant rescore`, the same terms as self-play) into
~/arhanpassant-data/selfplay/lichess/DAY.bin so the forge loop trains on it,
and appends the day's results per time control to
~/arhanpassant-data/lichess/summary.json. Run daily by launchd
(forge/launchd/com.arhanpassant.lichess-daily.plist).
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish_data  # noqa: E402

BOT = "arhanpassant"
DATA = os.path.expanduser("~/arhanpassant-data")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(DATA, "bin", "ap-current")


def fetch_pgn(day):
    start = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)
    since, until = int(start.timestamp() * 1000), int((start + dt.timedelta(days=1)).timestamp() * 1000)
    url = (f"https://lichess.org/api/games/user/{BOT}?since={since}&until={until}"
           "&moves=true&tags=true&clocks=false&evals=false&opening=false&finished=true")
    req = urllib.request.Request(url, headers={"Accept": "application/x-chess-pgn", "User-Agent": "arhanpassant-forge"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8", errors="replace")


def games(pgn):
    for game in re.split(r"\n\n(?=\[Event )", pgn.strip()):
        tags = dict(re.findall(r'\[(\w+) "(.*)"\]', game))
        if not tags:
            continue
        body = re.sub(r"\{[^}]*\}|\([^)]*\)|\[[^\]]*\]", " ", game)
        moves = [re.sub(r"^\d+\.+", "", t) for t in body.split()
                 if not re.match(r"^\d+\.+$", t) and t not in ("1-0", "0-1", "1/2-1/2", "*")]
        yield tags, [re.sub(r"[?!]+$", "", m) for m in moves if m]


def speed(tags):
    """Lichess's speed from the time control: estimated game time = base + 40 x increment."""
    tc = tags.get("TimeControl", "-")
    base, _, inc = tc.partition("+")
    # Correspondence games export as "-", "1/259200" or "2 days per move".
    if not base.isdigit() or not (inc or "0").isdigit():
        return "correspondence"
    est = int(base) + 40 * int(inc or 0)
    return "ultrabullet" if est < 30 else "bullet" if est < 180 else "blitz" if est < 480 else "rapid" if est < 1500 else "classical"


def main():
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    pgn_dir = os.path.join(DATA, "lichess", "pgn")
    os.makedirs(pgn_dir, exist_ok=True)
    pgn = fetch_pgn(day)
    with open(os.path.join(pgn_dir, f"{day}.pgn"), "w") as f:
        f.write(pgn)

    per_speed, lines = {}, []
    for tags, moves in games(pgn):
        result = tags.get("Result")
        if result not in ("1-0", "0-1", "1/2-1/2"):
            continue
        white = tags.get("White", "").lower() == BOT
        opponent_bot = tags.get("BlackTitle" if white else "WhiteTitle") == "BOT"
        s = per_speed.setdefault(speed(tags), {"games": 0, "won": 0, "drawn": 0, "lost": 0, "vsBots": 0, "vsPeople": 0, "ratingChange": 0})
        s["games"] += 1
        s["vsBots" if opponent_bot else "vsPeople"] += 1
        outcome = "drawn" if result == "1/2-1/2" else "won" if (result == "1-0") == white else "lost"
        s[outcome] += 1
        try:
            s["ratingChange"] += int(tags.get("WhiteRatingDiff" if white else "BlackRatingDiff", "0"))
        except ValueError:
            pass
        if tags.get("Variant", "Standard") == "Standard" and "FEN" not in tags and moves:
            lines.append(f"{result} {' '.join(moves)}")

    records = 0
    if lines:
        state = json.load(open(os.path.join(DATA, "forge", "state.json")))
        games_txt = os.path.join(pgn_dir, f"{day}.games.txt")
        with open(games_txt, "w") as f:
            f.write("\n".join(lines) + "\n")
        out_dir = os.path.join(DATA, "selfplay", "lichess")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{day}.bin")
        if os.path.exists(out):
            os.remove(out)  # a rerun replaces the day
        r = subprocess.run([ENGINE, "rescore", "--in", games_txt, "--out", out, "--nodes", "5000",
                            "--threads", "6", "--net", state["champion_net"]], capture_output=True, text=True, check=True)
        records = int(re.search(r"positions (\d+)", r.stdout).group(1))

    entry = {"date": str(day), "games": sum(s["games"] for s in per_speed.values()), "positions": records, "bySpeed": per_speed}
    path = os.path.join(DATA, "lichess", "summary.json")
    summary = json.load(open(path)) if os.path.exists(path) else {"days": []}
    summary["days"] = [d for d in summary["days"] if d["date"] != str(day)] + [entry]
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
        f.write("\n")
    print(json.dumps(entry))
    publish_data.publish_quietly()


if __name__ == "__main__":
    main()
