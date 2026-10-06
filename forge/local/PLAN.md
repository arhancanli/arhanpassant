# ArhanPassant Elo push (2026-10-06 →)

Work tree ~/arhanpassant-elo, branch engine/elo-20261006 (pushed). Every new
search idea is a tunable that defaults to 0/off (bench 378,643 at depth 11
with everything off). Tests: Mac queue (~/arhanpassant-data/elo/queue, runner.sh,
sprt2.sh) and Oracle ap-a1 (ssh ubuntu@141.145.154.73, ~/queue, vm-tests.sh).

## Accepted (SPRT [0,5] at 5+0.05)
- speed patch 7262ce5: +28.3 [11.4, 45.4] (504 g)
- order bundle threat_hist=1 pawn_hist=1 threat_order=100 check_order=16384: +11.2 [3.7, 18.6] (2,676 g)
- (passed earlier, 3,072 games) corr_joint=1 corr_cont=128: +10.2

## Network 0.13.0-rl1 (dae1ac9, bench 372,903) — RL round 1 PASSED +27.3 [14.9, 39.8] (1,032 g)
champion 0.12 fine-tuned 3 epochs, lr 1e-4, factorised, on 20M fresh positions
(gen11, gen12, active1, oci). Binary bin/ap-net-0.13.0-rl1. Promote with
forge/promote_net.sh. Round 2 data: selfplay/gen13 + active2 (Mac), Oracle
selfplay/<host>/<rev>/ (rev 1cbb2f5+). Next round when ~20M new (≈21:30Z).
SPSA-A (pruning, 6,000 pairs, 5+0.05, conc 7) running on ap-e2b (~/spsa-A.json).
ap-a1 back on datagen (16 threads).

## Shipped as defaults (f544e99, bench 403,313)
order bundle + corr_joint=1/corr_cont=128. Binary ~/arhanpassant-data/bin/ap-d1.
Rejected/stopped: search-b bundle (+2.3 at 1,220 g, split into parts below),
hindsight (trending H0).

## Queued / running (old list below; Mac queue reordered at 17:17Z on ap-d1)
Mac: search-b (fh_blend lmr_cutoff lmr_capt=8192 se_neg fut_hist=8192 improving_v2),
tm-falling=600, probcut-v2=200, qsfut-v2=250, qs-lmp=3, corr-minor=128,
cap-fut=250, see-capt-hist=64.
A1: hindsight=158, lmr-corr=100, lmr-pv, upcoming-rep, tt-cut-node, nmp-cutnode,
smp-skip (4 threads each side, conc 3).

## Next
1. Combine accepted → defaults (forge/set_defaults.py), rebuild, bench signature.
2. Confirm combo vs ap-base-daf3469 (STC + a longer TC).
3. SPSA overnight: A (Mac) pruning/reduction params, B (A1) history/ordering/
   correction/time params, from the combined defaults; merge; verify by SPRT.
4. Gauntlet vs the 14 milestone opponents (elo/gauntlet.py) → compare with 0.12.
5. Network: self-play into selfplay/gen12, active1 (seeded from losses), Oracle
   bucket; RL round = fine-tune champion (or widen to 1024, exact) on fresh data
   (elo/rl_round.py), SPRT vs champion. Needs ≥50M fresh; 1024 needs far more.
6. Restart Lichess bot on the improved build (owner rule: always top level).

## Owner decisions pending
- Oracle Pay As You Go upgrade (trial caps us at ~21 OCPUs; PAYG ~125).
- Disk: 16 GB free; ~/.npm 23 GB, ~/.cache 26 GB could be cleared (ask first).

## 17:45Z status
- Mac: tests at concurrency 10 (tm-falling running; next gauntlet g-rl1 100 g/opponent
  vs 9 informative opponents, then qs-lmp, corr-minor, upcoming-rep, cap-fut,
  smp-skip-4t); 10 low-value tests parked in elo/parked/.
- Self-play: Mac 6+2 threads (gen13, active2), ap-a1 16 threads, ap-e2c 2 threads.
- RL loop (forge/rl_loop.py, state ~/arhanpassant-data/rl/state.json, log rl/loop.log):
  round 2 trains at 15M fresh positions (ETA ~19:50Z), SPRT 000-rl2-try1.
- SPSA-A on ap-e2b: ~600 pairs/h, 6,000 pairs → ~03:30Z.
- 16-king-bucket expansion ready in trainer (exact); test with round-2 data.
- Stopped/no gain: search-b1 (+1.0 at 700 g), hindsight (H0 trend).
