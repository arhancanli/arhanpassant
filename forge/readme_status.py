"""Rewrite the README's Status section from the records, so no number is typed by hand.

    python forge/readme_status.py

Reads forge/ledger.json (promotions), forge/tests.json (search changes) and
forge/anchors.json (absolute strength), forge/engines.json (matches against
other engines, when present), and replaces everything between
"## Status" and the next "## " heading.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


NAMES = {"tm-nodes": "node-based time management", "corr-pawn": "pawn-structure evaluation correction",
         "prior-bonus": "a history bonus for the move that made the opponent fail low", "tt-hist": "table-cutoff history",
         "eval-hist": "evaluation-swing history", "lmr-ttcap": "an extra reduction under a capture table move",
         "cont4": "four-ply continuation history", "corr-joint": "joint correction learning",
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
        f"Search changes are tested the same way, locally or on the cloud fleet: {len(passed)} were accepted "
        f"({', '.join(NAMES.get(t['name'], t['name']) for t in passed)}), and {len(failed)} did not. Every result, "
        "including the failures, is in [`forge/tests.json`](forge/tests.json).",
        "",
    ]
    old_anchor = (f"{games} games of version {anchors['version']} against Stockfish 19 at fixed `UCI_Elo` levels of "
                  f"{levels}, which Stockfish calibrates to the CCRL 40/4 list")
    engines_path = os.path.join(ROOT, "forge", "engines.json")
    if os.path.exists(engines_path):
        # Matches against other open-source engines (forge/engines_report.py); the first run is the headline.
        runs = json.load(open(engines_path))["runs"]
        head, rest = runs[0], runs[1:]
        families = sorted({m["opponent"].split("-")[0].capitalize() for m in head["matches"]})
        lo_e, hi_e = head["ci95"]
        others = "; ".join(f"at {r['tc']}, {r['games']:,} games give {r['estimate']:,} ({r['ci95'][0]:,} to {r['ci95'][1]:,})"
                           for r in rest)
        top = {}
        for r in runs:
            for m in r["matches"]:
                if m["opponent"] not in r["fit_opponents"] and m["score"] < 0.2:
                    t = top.setdefault(m["opponent"], {"ccrl": m["ccrl"], "games": 0, "points": 0.0})
                    t["games"] += m["games"]
                    t["points"] += m["score"] * m["games"]
        top_text = ", ".join(f"{o.replace('-', ' ', 1).title()} (CCRL {t['ccrl']:,}) {100 * t['points'] / t['games']:.1f}% "
                             f"of {t['games']} games" for o, t in sorted(top.items(), key=lambda kv: -kv[1]["ccrl"]))
        lines += [
            f"**Measured strength: about {head['estimate']:,} on the CCRL Blitz scale** (95% interval {lo_e:,} to "
            f"{hi_e:,} from game statistics alone), from {head['games']:,} games at {head['tc']} against "
            f"{len(head['matches'])} versions of {', '.join(families[:-1])} and {families[-1]} with published CCRL "
            "Blitz ratings: each opponent's rating is read from the CCRL list and one rating is fitted to the "
            f"{len(head['fit_opponents'])} matches the engine scores 20-80% in" + (f"; {others}" if others else "") + ". "
            + (f"Stronger engines are too far ahead to rate against: {top_text}. " if top_text else "")
            + "The matches ran before the last search changes were accepted. "
            "These are our own matches on cloud machines, not an official CCRL rating, and the opponents' own "
            "ratings carry another 10-20 Elo of uncertainty. Details: [`forge/engines.json`](forge/engines.json).",
            "",
            f"An earlier measurement, {old_anchor}, gave about {anchors['combined_estimate']:,} "
            "([`forge/anchors.json`](forge/anchors.json)).",
            "",
        ]
    else:
        lines += [
            f"**Measured strength: about {anchors['combined_estimate']:,} Elo** (95% interval {lo:,} to {hi:,} from "
            f"game statistics alone), from {old_anchor} at 120s+1s; these games were played at 10s+0.1s, so treat "
            "the absolute number as approximate. Details: [`forge/anchors.json`](forge/anchors.json).",
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
