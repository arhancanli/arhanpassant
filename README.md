# ArhanPassant

A UCI chess engine and chess library in Rust that keeps getting stronger. Every
new version learns from the engine's own self-play games, and it only replaces
the previous version after beating it in a statistical test over thousands of
games.

**[Play it in your browser](https://arhanpassant.vercel.app/play)** ·
**[Adaptive puzzle trainer](https://arhanpassant.vercel.app/train)** ·
**[Promotion ledger](https://arhanpassant.vercel.app/engine)**

## Status

Version 0.1.0 is the starting point: magic-bitboard move generation, a full
alpha-beta search and a hand-written evaluation. Neural-network (NNUE) versions
trained on self-play take over through the loop below, and each promotion is
recorded with the games it took and the strength it gained.

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
