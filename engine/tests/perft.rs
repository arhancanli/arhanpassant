//! Move-generator correctness: leaf counts for positions with published results
//! (Chess Programming Wiki perft positions and Martin Sedlak's edge-case suite).
//! Shallow depths run in `cargo test`; full depths run with
//! `cargo test --release -- --ignored`.

use arhanpassant::{perft, Position};

const SUITE: &[(&str, &[u64])] = &[
    ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", &[20, 400, 8902, 197281, 4865609, 119060324]),
    ("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", &[48, 2039, 97862, 4085603, 193690690]),
    ("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", &[14, 191, 2812, 43238, 674624, 11030083, 178633661]),
    ("r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", &[6, 264, 9467, 422333, 15833292, 706045033]),
    ("r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1", &[6, 264, 9467, 422333, 15833292, 706045033]),
    ("rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", &[44, 1486, 62379, 2103487, 89941194]),
    ("r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10", &[46, 2079, 89890, 3894594, 164075551]),
];

/// (fen, depth, nodes): positions built to break specific rules.
const EDGE_CASES: &[(&str, u32, u64)] = &[
    ("3k4/3p4/8/K1P4r/8/8/8/8 b - - 0 1", 6, 1134888), // illegal ep move #1
    ("8/8/4k3/8/2p5/8/B2P2K1/8 w - - 0 1", 6, 1015133), // illegal ep move #2
    ("8/8/1k6/2b5/2pP4/8/5K2/8 b - d3 0 1", 6, 1440467), // ep capture checks opponent
    ("5k2/8/8/8/8/8/8/4K2R w K - 0 1", 6, 661072),       // short castling gives check
    ("3k4/8/8/8/8/8/8/R3K3 w Q - 0 1", 6, 803711),       // long castling gives check
    ("r3k2r/1b4bq/8/8/8/8/7B/R3K2R w KQkq - 0 1", 4, 1274206), // castling (incl. losing rights)
    ("r3k2r/8/3Q4/8/8/5q2/8/R3K2R b KQkq - 0 1", 4, 1720476), // castling prevented
    ("2K2r2/4P3/8/8/8/8/8/3k4 w - - 0 1", 6, 3821001),   // promote out of check
    ("8/8/1P2K3/8/2n5/1q6/8/5k2 b - - 0 1", 5, 1004658), // discovered check
    ("4k3/1P6/8/8/8/8/K7/8 w - - 0 1", 6, 217342),       // promote to give check
    ("8/P1k5/K7/8/8/8/8/8 w - - 0 1", 6, 92683),         // under-promote to give check
    ("K1k5/8/P7/8/8/8/8/8 w - - 0 1", 6, 2217),          // self stalemate
    ("8/k1P5/8/1K6/8/8/8/8 w - - 0 1", 7, 567584),       // stalemate and checkmate
    ("8/8/2k5/5q2/5n2/8/5K2/8 b - - 0 1", 4, 23527),     // stalemate and checkmate
];

fn check_suite(max_nodes: u64) {
    for (fen, counts) in SUITE {
        let pos = Position::from_fen(fen).unwrap();
        for (i, &expected) in counts.iter().enumerate() {
            if expected > max_nodes {
                break;
            }
            let depth = i as u32 + 1;
            assert_eq!(perft(&pos, depth), expected, "{fen} depth {depth}");
        }
    }
}

#[test]
fn suite_shallow() {
    check_suite(3_000_000);
}

#[test]
fn edge_cases() {
    for &(fen, depth, expected) in EDGE_CASES {
        let pos = Position::from_fen(fen).unwrap();
        assert_eq!(perft(&pos, depth), expected, "{fen} depth {depth}");
    }
}

#[test]
#[ignore = "slow: full published depths (run with --release -- --ignored)"]
fn suite_full() {
    check_suite(u64::MAX);
}
