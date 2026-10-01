//! Pentanomial generalized SPRT with logistic-Elo bounds, and Elo estimates.
//!
//! Games are played in pairs (same opening, colours swapped). A pair scores
//! 0, 0.5, 1, 1.5 or 2 for the first engine; `counts[i]` is the number of
//! pairs that scored i/2. The log-likelihood ratio uses the constrained
//! maximum-likelihood distributions of M. Van den Bergh's GSPRT.

pub const SCORES: [f64; 5] = [0.0, 0.25, 0.5, 0.75, 1.0];

pub fn logistic(elo: f64) -> f64 {
    1.0 / (1.0 + 10f64.powf(-elo / 400.0))
}

pub fn elo_from_score(s: f64) -> f64 {
    let s = s.clamp(1e-9, 1.0 - 1e-9);
    -400.0 * (1.0 / s - 1.0).log10()
}

/// Constrained MLE of the pentanomial distribution with expected score `s`.
fn mle(p: &[f64; 5], s: f64) -> [f64; 5] {
    let d: Vec<f64> = SCORES.iter().map(|a| a - s).collect();
    let d_max = d.iter().cloned().fold(f64::MIN, f64::max);
    let d_min = d.iter().cloned().fold(f64::MAX, f64::min);
    // The root lies strictly inside (-1/d_max, -1/d_min); f decreases from +inf to -inf.
    let (mut lo, mut hi) = (-1.0 / d_max, -1.0 / d_min);
    let f = |l: f64| (0..5).map(|i| p[i] * d[i] / (1.0 + l * d[i])).sum::<f64>();
    for _ in 0..200 {
        let mid = 0.5 * (lo + hi);
        if f(mid) > 0.0 {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let l = 0.5 * (lo + hi);
    let mut q = [0.0; 5];
    for i in 0..5 {
        q[i] = p[i] / (1.0 + l * d[i]);
    }
    q
}

/// Log-likelihood ratio of H1 (Elo = elo1) against H0 (Elo = elo0).
pub fn llr(counts: &[u64; 5], elo0: f64, elo1: f64) -> f64 {
    let c: Vec<f64> = counts.iter().map(|&x| (x as f64).max(1e-3)).collect();
    let n: f64 = c.iter().sum();
    let mut p = [0.0; 5];
    for i in 0..5 {
        p[i] = c[i] / n;
    }
    let p0 = mle(&p, logistic(elo0));
    let p1 = mle(&p, logistic(elo1));
    n * (0..5).map(|i| p[i] * (p1[i] / p0[i]).ln()).sum::<f64>()
}

/// (lower, upper) LLR decision bounds.
pub fn bounds(alpha: f64, beta: f64) -> (f64, f64) {
    ((beta / (1.0 - alpha)).ln(), ((1.0 - beta) / alpha).ln())
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EloEstimate {
    pub elo: f64,
    pub lo: f64,
    pub hi: f64,
    pub score: f64,
}

/// Elo with a 95% confidence interval, from pentanomial pair results.
pub fn elo_estimate(counts: &[u64; 5]) -> EloEstimate {
    let n: f64 = counts.iter().sum::<u64>() as f64;
    if n == 0.0 {
        return EloEstimate { elo: 0.0, lo: f64::NEG_INFINITY, hi: f64::INFINITY, score: 0.5 };
    }
    let mean = (0..5).map(|i| counts[i] as f64 * SCORES[i]).sum::<f64>() / n;
    let var = (0..5).map(|i| counts[i] as f64 * (SCORES[i] - mean).powi(2)).sum::<f64>() / n;
    let se = (var / n).sqrt();
    EloEstimate {
        elo: elo_from_score(mean),
        lo: elo_from_score(mean - 1.96 * se),
        hi: elo_from_score(mean + 1.96 * se),
        score: mean,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Rng(u64);
    impl Rng {
        fn f(&mut self) -> f64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            (self.0 >> 11) as f64 / (1u64 << 53) as f64
        }
    }

    /// Pair-outcome distribution for independent games at a true Elo and draw rate.
    fn pair_probs(elo: f64, draw: f64) -> [f64; 5] {
        let s = logistic(elo);
        let w = s - draw / 2.0;
        let l = 1.0 - s - draw / 2.0;
        [l * l, 2.0 * l * draw, draw * draw + 2.0 * w * l, 2.0 * w * draw, w * w]
    }

    /// Fraction of SPRT runs that accept H1.
    fn accept_rate(true_elo: f64, elo0: f64, elo1: f64, runs: usize, seed: u64) -> f64 {
        let probs = pair_probs(true_elo, 0.5);
        let (a, b) = bounds(0.05, 0.05);
        let mut rng = Rng(seed);
        let mut accepted = 0;
        for _ in 0..runs {
            let mut c = [0u64; 5];
            loop {
                for _ in 0..4 {
                    let mut x = rng.f();
                    let mut k = 0;
                    while k < 4 && x >= probs[k] {
                        x -= probs[k];
                        k += 1;
                    }
                    c[k] += 1;
                }
                let l = llr(&c, elo0, elo1);
                if l >= b {
                    accepted += 1;
                    break;
                }
                if l <= a {
                    break;
                }
            }
        }
        accepted as f64 / runs as f64
    }

    #[test]
    fn llr_sign_follows_the_data() {
        // 1000 pairs at exactly the H1 score favour H1; at the H0 score favour H0.
        let strong = [100, 200, 400, 200, 100];
        assert!(llr(&strong, 0.0, 5.0) < 0.0); // a dead-even result favours H0 = 0 Elo
        let better = [80, 190, 400, 220, 110];
        assert!(llr(&better, 0.0, 5.0) > 0.0);
        assert!(bounds(0.05, 0.05).0 < -2.9 && bounds(0.05, 0.05).1 > 2.9);
    }

    #[test]
    fn elo_estimate_is_centred() {
        let e = elo_estimate(&[100, 200, 400, 200, 100]);
        assert!(e.elo.abs() < 1e-9 && e.lo < 0.0 && e.hi > 0.0);
    }

    /// The gate's error rates must match its stated alpha and beta: run the
    /// sequential test on simulated matches where the truth is known.
    #[test]
    #[ignore = "simulation: run with cargo test --release -- --ignored"]
    fn calibrated_error_rates() {
        // At the bounds the acceptance rates are alpha and 1 - beta; halfway
        // between them a symmetric test accepts about half the time.
        let fp = accept_rate(0.0, 0.0, 10.0, 600, 1);
        let power = accept_rate(10.0, 0.0, 10.0, 600, 2);
        let mid = accept_rate(5.0, 0.0, 10.0, 600, 4);
        println!("false positive rate {fp:.3}, power {power:.3}, midpoint {mid:.3}");
        assert!((0.025..=0.08).contains(&fp), "false positives {fp}");
        assert!((0.92..=0.98).contains(&power), "power {power}");
        assert!((0.38..=0.62).contains(&mid), "midpoint acceptance {mid}");
        // A regression must essentially never pass.
        let bad = accept_rate(-5.0, 0.0, 10.0, 300, 3);
        assert!(bad < 0.01, "regression accepted {bad}");
    }
}
