"""Absolute strength from games against Stockfish at fixed UCI_Elo levels.

    python forge/anchors.py VERSION OUT.json RESULT.json [RESULT.json ...]

Each RESULT.json is an arena result whose second engine is named
stockfish...-elo<LEVEL> (forge/azure/gauntlet-node.sh); results for the same
level are pooled. Each level gives an estimate (level + the pentanomial Elo
difference, with its 95% interval); the combined estimate is their
inverse-variance weighted mean.
"""

import json
import math
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import sprt  # noqa: E402


def main():
    version, out, *paths = sys.argv[1:]
    pooled = {}
    for path in paths:
        r = json.load(open(path))
        level = int(re.search(r"elo(\d+)", r["baseline"]).group(1))
        p = pooled.setdefault(level, {"games": 0, "wins": 0, "losses": 0, "draws": 0, "penta": [0] * 5})
        for k in ("games", "wins", "losses", "draws"):
            p[k] += r[k]
        p["penta"] = [a + b for a, b in zip(p["penta"], r["penta"])]
    anchors, num, den = [], 0.0, 0.0
    for level in sorted(pooled):
        p = pooled[level]
        e, lo, hi = sprt.elo_estimate(p["penta"])
        se = max((hi - lo) / (2 * 1.96), 1e-6)
        num += (level + e) / se ** 2
        den += 1 / se ** 2
        anchors.append({"stockfish_uci_elo": level, **p, "elo_diff": round(e, 1), "estimate": round(level + e),
                        "estimate_lo": round(level + lo), "estimate_hi": round(level + hi)})
    combined, se = num / den, 1 / math.sqrt(den)
    doc = {
        "version": version,
        "date": __import__("datetime").date.today().isoformat(),
        "method": "Gauntlet vs Stockfish 19 with UCI_LimitStrength (UCI_Elo calibrated by Stockfish at 120s+1s, anchored to CCRL 40/4); "
                  "10+0.1, 2moves_v1 book, 1 thread each, Azure VMs",
        "anchors": anchors,
        "combined_estimate": round(combined),
        "combined_ci95": [round(combined - 1.96 * se), round(combined + 1.96 * se)],
        "caveat": "Statistical interval only. Games were 10+0.1, faster than Stockfish's calibration control, so the absolute level is approximate.",
    }
    with open(out, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"v{version}: {doc['combined_estimate']} {doc['combined_ci95']} from {sum(a['games'] for a in anchors)} games")


if __name__ == "__main__":
    main()
