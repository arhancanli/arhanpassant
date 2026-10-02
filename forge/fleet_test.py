"""Test a search change on the fleet with a sequential probability ratio test.

Both sides run the same build with the same network; they differ only in UCI
options (search settings). Every finished test, passed or not, is appended to
forge/tests.json so the record shows what was tried and how it measured.

    python forge/fleet_test.py --name tm-nodes --change "Time: scale by best-move effort" \
        --cand tm_nodes=1
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet  # noqa: E402

DATA = os.path.expanduser("~/arhanpassant-data")
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests.json")


def options(pairs):
    out = {}
    for p in pairs or []:
        k, v = p.split("=", 1)
        out[k] = v
    return out


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--change", required=True, help="one line: what the candidate does differently")
    ap.add_argument("--cand", nargs="*", default=[], help="candidate options NAME=VALUE")
    ap.add_argument("--champ", nargs="*", default=[], help="baseline options NAME=VALUE")
    ap.add_argument("--net", help="network both sides use (default: the current champion)")
    ap.add_argument("--src", help="build to test (default: the fleet's current build)")
    ap.add_argument("--tc", default="8+0.08")
    ap.add_argument("--elo0", type=float, default=0.0)
    ap.add_argument("--elo1", type=float, default=5.0)
    ap.add_argument("--max-games", type=int, default=12000)
    args = ap.parse_args()

    state = json.load(open(os.path.join(DATA, "forge", "state.json")))
    net = os.path.expanduser(args.net or state["champion_net"])
    src = args.src or fleet.current_src()
    if fleet.read_control().get("SRC") != src:
        fleet.write_control(SRC=src)
        log(f"fleet switched to {src}; nodes rebuild at their next chunk boundary")
    cand, champ = options(args.cand), options(args.champ)
    log(f"test {args.name}: {cand or 'defaults'} vs {champ or 'defaults'} on {os.path.basename(net)}, {src}, {args.tc}")
    r = fleet.remote_gate(net, net, tc=args.tc, elo0=args.elo0, elo1=args.elo1, max_games=args.max_games,
                          cand_opts=cand, champ_opts=champ, src=src, name=args.name, log=log)
    if r is None:
        log(f"test {args.name}: the fleet stalled; no result")
        sys.exit(2)
    os.makedirs(os.path.join(DATA, "forge", "tests"), exist_ok=True)
    json.dump(r, open(os.path.join(DATA, "forge", "tests", f"{r['id']}-{args.name}.json"), "w"), indent=2)
    entry = {
        "date": r["id"][:8], "name": args.name, "change": args.change, "candidate": cand, "baseline": champ,
        "network": os.path.basename(net), "build": src, "tc": args.tc, "bounds": [args.elo0, args.elo1],
        "games": r["games"], "elo": round(r["elo"], 1), "eloLo": round(r["elo_lo"], 1), "eloHi": round(r["elo_hi"], 1),
        "llr": round(r["sprt"]["llr"], 2), "decision": r["decision"],
    }
    ledger = json.load(open(LEDGER)) if os.path.exists(LEDGER) else {"tests": []}
    ledger["tests"].append(entry)
    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=2)
        f.write("\n")
    log(f"test {args.name}: {r['decision']} after {r['games']} games, elo {r['elo']:+.1f} [{r['elo_lo']:+.1f}, {r['elo_hi']:+.1f}]")


if __name__ == "__main__":
    main()
