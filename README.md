# ArhanPassant

A UCI chess engine and chess library in Rust that keeps getting stronger. Every
new version learns from the engine's own self-play games, and it only replaces
the previous version after beating it in a statistical test over thousands of
games.

**[Play it in your browser](https://arhanpassant.com/play)** ·
**[Play it on Lichess](https://lichess.org/@/arhanpassant)** ·
**[Adaptive puzzle trainer](https://arhanpassant.com/train)** ·
**[Promotion ledger](https://arhanpassant.com/engine)**

## Play it here on GitHub

Everyone plays White, together, against the engine on one shared board. Pick a
move on the [play page](https://github.com/arhancanli/arhanpassant/blob/play/README.md),
press **Submit new issue**, and the engine answers on your issue within a few
minutes.

<p align="center"><a href="https://github.com/arhancanli/arhanpassant/blob/play/README.md"><img src="https://raw.githubusercontent.com/arhancanli/arhanpassant/play/board.svg" width="360" alt="The community game's current position"></a></p>

## Status

Version 0.11.0: NNUE network (8 king buckets, 8 output buckets), 512 hidden units, trained on 353.9M self-play positions. It is the 10th network in a row to pass the gate against its predecessor; every promotion is recorded in [`forge/ledger.json`](forge/ledger.json) with the games it took and the strength it gained.

Search changes are tested the same way, locally or on the cloud fleet: 6 were accepted (node-based time management, pawn-structure evaluation correction, piece-set evaluation correction, deeper/shallower re-searches, mop-up endgame knowledge, a history bonus for the move that made the opponent fail low), and 7 did not. Every result, including the failures, is in [`forge/tests.json`](forge/tests.json).

**Measured strength: about 3,426 on the CCRL Blitz scale** (95% interval 3,414 to 3,437 from game statistics alone), from 2,284 games at 60+0.6 against 8 versions of Demolito, Ethereal, Laser, Stash and Weiss with published CCRL Blitz ratings: each opponent's rating is read from the CCRL list and one rating is fitted to the 6 matches the engine scores 20-80% in; at 10+0.1, 5,200 games give 3,448 (3,436 to 3,459). Stronger engines are too far ahead to rate against: Stockfish 17.1 (CCRL 3,771) 3.9% of 400 games, Koivisto 9.0 (CCRL 3,632) 10.6% of 400 games. The matches ran before the last search changes were accepted. These are our own matches on cloud machines, not an official CCRL rating, and the opponents' own ratings carry another 10-20 Elo of uncertainty. Details: [`forge/engines.json`](forge/engines.json).

An earlier measurement, 480 games of version 0.7.0 against Stockfish 19 at fixed `UCI_Elo` levels of 2500, 2800, 3100, which Stockfish calibrates to the CCRL 40/4 list, gave about 3,026 ([`forge/anchors.json`](forge/anchors.json)).

## What is inside

- **Move generation**: legal moves from magic bitboards, matching 21 published
  perft results, including the 706,045,033-position test from the Chess
  Programming Wiki and Martin Sedlak's en passant and castling edge cases.
- **Search**: iterative deepening, aspiration windows, principal variation
  search, quiescence search, a lock-free shared hash table and lazy SMP threads;
  null-move, reverse futility, futility, late-move and static-exchange pruning;
  late-move reductions; singular extensions; history, continuation-history,
  killer and counter-move ordering.
- **Evaluation**: a (768 → N) × 2 → 1 NNUE with squared clipped ReLU, int16
  quantised and updated incrementally; tapered piece-square tables as the
  baseline.
- **Interfaces**: UCI, a Rust library (FEN, legal moves, SAN, perft) and a
  WebAssembly build that runs in browsers.
- **Self-improvement tooling**: self-play data generation, an NNUE trainer and a
  match runner with a calibrated sequential test.

## Quick start

```sh
cargo build --release
./target/release/arhanpassant bench
```

Add `target/release/arhanpassant` to any UCI chess GUI (Cute Chess, Arena,
Banksia, En Croissant). Options:

| Option          | Default      | Meaning                                          |
| --------------- | ------------ | ------------------------------------------------ |
| `Hash`          | 16           | Hash table size in MB                            |
| `Threads`       | 1            | Search threads                                   |
| `Move Overhead` | 30           | Milliseconds kept in reserve per move            |
| `EvalFile`      | `<embedded>` | Network file, or `<none>` for the hand-written evaluation |
| `SyzygyPath`    | `<empty>`    | Folders of Syzygy tablebase files, separated by `:` |
| `SyzygyProbeLimit` | 7         | Largest piece count probed during search         |

With tablebases loaded, the engine plays only moves that keep the tables'
result at the root (the fastest wins, the slowest losses) and scores
positions the tables cover right after a capture or pawn move.

## As a library

```rust
use arhanpassant::Position;

let pos = Position::from_fen("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3").unwrap();
for m in pos.legal_moves().iter() {
    println!("{} {}", m.to_uci(), pos.san(m));
}
assert_eq!(arhanpassant::perft(&Position::startpos(), 5), 4_865_609);
```

## How it gets stronger

1. **Self-play.** `arhanpassant datagen` plays games at a fixed number of nodes
   per move and stores every quiet position with its search score and the game
   result (32 bytes per position).
2. **Training.** `trainer/train.py` trains a network on those positions.
   `trainer/check_pipeline.py` proves the engine evaluates the exported network
   exactly as an integer re-implementation does, and that a scrambled export is
   caught.
3. **The gate.** `arena` plays the candidate against the champion in pairs of
   games (same opening, colours swapped) and runs a pentanomial sequential
   probability ratio test with bounds [0, 5] Elo and α = β = 0.05.
4. **Promotion.** Versions that pass are released and logged in the ledger, and
   become the opponent for the next round.

The gate is checked before it is trusted: on 600 simulated matches of known
strength it passes 5.0% of no-better versions and 95.2% of versions that really
are 5 Elo stronger (50.7% halfway between), an engine against an identical copy
measures exactly 0 Elo, and a deliberately broken likelihood ratio fails the
calibration test.

```sh
# a candidate network against the current engine
./target/release/arena \
  --engine name=candidate cmd=./target/release/arhanpassant opt.EvalFile=new.nnue \
  --engine name=champion  cmd=./target/release/arhanpassant \
  --tc 8+0.08 --book UHO_4060_v4.epd --concurrency 8 --games 40000 --sprt 0,5
```

## Repository

On macOS, keep self-play, training, network gates and the queued search
experiments running under a supervised service:

```sh
python3 forge/macbook.py install --cpu-budget 18 --min-new 20000000
python3 forge/macbook.py status
python3 forge/test_queue.py add NAME "change description" parameter=value
python3 forge/macbook.py stop
python3 forge/macbook.py start
```

This profile uses all 18 cores of the owner's Mac and starts a new training
round after 20 million fresh positions. Other Macs can choose their own budget;
omitting it reserves two cores. While search tests
are pending, half the budget generates self-play and the rest plays test
games. Network gates pause self-play and use the full budget; training uses
Metal when available and eight parallel record decoders. Local gates save
completed pairs every 64 games and resume after a restart. A build, network,
book, settings or execution profile change starts a separate gate. Only a passed gate promotes
a network or accepts search settings; the settings also apply to self-play.

Self-play rotates into chunks of about one million positions. The controller
limits generated data to 14 GiB and pauses generation below 8 GiB of free
disk space. It retires only complete generated chunks that were included in
a successful training round, preserving untrained data, networks and match
evidence. The service restarts after a crash and starts at login. It makes
progress while the Mac is awake; `status` reports its current phase, fresh
positions, pending tests and saved gate results.

For a multicore search experiment, pass `common.Threads=3` to `test_queue.py add`
(saved as `"common_options": {"Threads": "3"}`). Both engines receive these options, and the controller
reduces concurrent games to fit their threads within the remaining CPU budget.
Common options are recorded with the evidence and are never promoted as tuned
settings. `smp_vote=1` is an experimental vote among completed main and helper
searches; its shipped default is zero until a strength test passes.

| Path       | Contents                                             |
| ---------- | ---------------------------------------------------- |
| `engine/`  | The engine and library (`arhanpassant` crate)        |
| `arena/`   | Match runner and SPRT gate                           |
| `wasm/`    | WebAssembly interface for browsers                   |
| `trainer/` | NNUE training and pipeline checks                    |
| `forge/`   | Cloud self-play fleet scripts                        |

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or
[MIT license](LICENSE-MIT) at your option.
