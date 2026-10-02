"""Pentanomial GSPRT and Elo estimates: a Python twin of arena/src/sprt.rs.

The distributed gate sums game-pair results from many machines and decides
here, so this must agree with the Rust implementation; tests/check_sprt.py
compares both on recorded gate results.
"""

import math

SCORES = (0.0, 0.25, 0.5, 0.75, 1.0)


def logistic(elo):
    return 1.0 / (1.0 + 10.0 ** (-elo / 400.0))


def elo_from_score(s):
    s = min(max(s, 1e-9), 1.0 - 1e-9)
    return -400.0 * math.log10(1.0 / s - 1.0)


def _mle(p, s):
    d = [a - s for a in SCORES]
    lo, hi = -1.0 / max(d), -1.0 / min(d)
    f = lambda lam: sum(p[i] * d[i] / (1.0 + lam * d[i]) for i in range(5))
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    lam = 0.5 * (lo + hi)
    return [p[i] / (1.0 + lam * d[i]) for i in range(5)]


def llr(counts, elo0, elo1):
    c = [max(float(x), 1e-3) for x in counts]
    n = sum(c)
    p = [x / n for x in c]
    p0 = _mle(p, logistic(elo0))
    p1 = _mle(p, logistic(elo1))
    return n * sum(p[i] * math.log(p1[i] / p0[i]) for i in range(5))


def bounds(alpha, beta):
    return math.log(beta / (1.0 - alpha)), math.log((1.0 - beta) / alpha)


def elo_estimate(counts):
    n = float(sum(counts))
    if n == 0:
        return 0.0, float("-inf"), float("inf")
    mean = sum(counts[i] * SCORES[i] for i in range(5)) / n
    var = sum(counts[i] * (SCORES[i] - mean) ** 2 for i in range(5)) / n
    se = math.sqrt(var / n)
    return elo_from_score(mean), elo_from_score(mean - 1.96 * se), elo_from_score(mean + 1.96 * se)
