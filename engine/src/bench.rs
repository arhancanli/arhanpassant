//! Fixed-depth search over a fixed position set. The node total is a
//! signature of the search: any functional change alters it.

use crate::nnue::Network;
use crate::position::Position;
use crate::search::{Limits, Searcher, Shared};
use std::sync::Arc;
use std::time::Instant;

pub const DEFAULT_DEPTH: i32 = 11;

pub const POSITIONS: &[&str] = &[
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
    "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
    "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10",
    "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
    "rnbqkb1r/1p2pppp/p2p1n2/8/3NP3/2N1B3/PPP2PPP/R2QKB1R b KQkq - 1 6",
    "rnbqkb1r/ppp2ppp/4pn2/3p2B1/2PP4/2N5/PP2PPPP/R2QKBNR b KQkq - 3 4",
    "rnbq1rk1/ppp1ppbp/3p1np1/8/2PPP3/2N2N2/PP3PPP/R1BQKB1R w KQ - 2 6",
    "rnbqkbnr/pp3ppp/4p3/2ppP3/3P4/8/PPP2PPP/RNBQKBNR w KQkq - 0 4",
    "rnbqkbnr/pp2pppp/2p5/3p4/3PP3/8/PPP2PPP/RNBQKBNR w KQkq - 0 3",
    "r2q1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N1PN2/PP2BPPP/R2Q1RK1 w - - 0 10",
    "r3k2r/ppp2ppp/2nqbn2/3pp3/3PP3/2NQBN2/PPP2PPP/R3K2R w KQkq - 6 9",
    "r1b1kb1r/pppp1ppp/2n2q2/4n3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 0 6",
    "1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1",
    "8/8/8/4k3/8/8/4P3/4K3 w - - 0 1",
    "8/8/1k6/8/1P6/1K6/8/r6R w - - 0 1",
    "8/8/8/3k4/8/3r4/8/3QK3 w - - 0 1",
    "8/8/8/8/3k4/8/8/KBN5 w - - 0 1",
    "8/5pk1/6p1/8/R7/6P1/5PK1/r7 w - - 0 40",
    "8/p7/8/8/8/8/7P/k6K w - - 0 1",
];

/// Search every bench position to `depth` with a fresh 16 MB table; prints and
/// returns the total node count.
pub fn run(depth: i32, network: Option<Arc<Network>>) -> u64 {
    let start = Instant::now();
    let mut total = 0u64;
    for fen in POSITIONS {
        let pos = Position::from_fen(fen).expect("bench FEN");
        let shared = Shared::new(16, network.clone());
        let mut s = Searcher::new(shared);
        s.verbose = false;
        let limits = Limits { depth: Some(depth), ..Default::default() };
        let r = s.go(&pos, &[pos.hash()], &limits, true, 0, &mut |_| {});
        total += r.nodes;
    }
    let ms = start.elapsed().as_millis().max(1) as u64;
    println!("Bench: {total} nodes {} nps ({ms} ms, depth {depth})", total * 1000 / ms);
    println!("{total} nodes {} nps", total * 1000 / ms);
    total
}
