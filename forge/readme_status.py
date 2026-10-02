"""Rewrite the README's Status section from the records, so no number is typed by hand.

    python forge/readme_status.py

Reads forge/ledger.json (promotions), forge/tests.json (search changes) and
forge/anchors.json (absolute strength), and replaces everything between
"## Status" and the next "## " heading.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


NAMES = {"tm-nodes": "node-based time management", "corr-pawn": "pawn-structure evaluation correction",
         "corr-np": "piece-set evaluation correction", "corr-cont": "previous-move evaluation correction",
         "razoring": "razoring", "probcut": "ProbCut", "qs-futility": "quiescence futility pruning",
         "lmr-deeper": "deeper/shallower re-searches", "hist-prune": "history pruning", "mopup": "mop-up endgame knowledge"}


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def main():
    ledger = json.load(open(os.path.join(ROOT, "forge", "ledger.json")))["versions"]
    tests = json.load(open(os.path.join(ROOT, "forge", "tests.json")))["tests"]
    anchors = json.load(open(os.path.join(ROOT, "forge", "anchors.json")))
    current = ledger[-1]
    networks = sum(1 for v in ledger if v.get("status") == "promoted")
    passed = [t for t in tests if t["decision"] == "H1"]
    failed = [t for t in tests if t["decision"] != "H1"]
    lo, hi = anchors["combined_ci95"]
    levels = ", ".join(str(a["stockfish_uci_elo"]) for a in anchors["anchors"])
    games = sum(a["games"] for a in anchors["anchors"])
    lines = [
        "## Status",
        "",
        f"Version {current['version']}: {current['change']}. "
        f"It is the {ordinal(networks)} network in a row to pass the gate against its predecessor; every promotion is "
        "recorded in [`forge/ledger.json`](forge/ledger.json) with the games it took and the strength it gained.",
        "",
        f"Search changes are tested the same way, on a fleet of cloud machines: {len(passed)} passed and ship "
        f"({', '.join(NAMES.get(t['name'], t['name']) for t in passed)}), and {len(failed)} did not. Every result, "
        "including the failures, is in [`forge/tests.json`](forge/tests.json).",
        "",
        f"**Measured strength: about {anchors['combined_estimate']:,} Elo** (95% interval {lo:,} to {hi:,} from game "
        f"statistics alone), from {games} games of version {anchors['version']} against Stockfish 19 at fixed `UCI_Elo` "
        f"levels of {levels}. Stockfish calibrates those levels to the CCRL 40/4 list at 120s+1s; these games were "
        "played at 10s+0.1s, so treat the absolute number as approximate. Details: "
        "[`forge/anchors.json`](forge/anchors.json).",
        "",
    ]
    if "--dry-run" in sys.argv:
        print("\n".join(lines))
        return
    path = os.path.join(ROOT, "README.md")
    text = open(path).read()
    start = text.index("## Status")
    end = text.index("\n## ", start + 1) + 1
    open(path, "w").write(text[:start] + "\n".join(lines) + "\n" + text[end:])
    print("\n".join(lines))


if __name__ == "__main__":
    main()
