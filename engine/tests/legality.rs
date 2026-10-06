//! The single-move legality check must agree with move generation on every
//! possible 16-bit move code, in positions full of pins, checks, castling,
//! en passant and promotions.

use arhanpassant::Position;

const FENS: &[&str] = &[
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
    "r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1",
    "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
    "3k4/3p4/8/K1P4r/8/8/8/8 b - - 0 1",
    "8/8/4k3/8/2p5/8/B2P2K1/8 w - - 0 1",
    "8/8/1k6/2b5/2pP4/8/5K2/8 b - d3 0 1",
    "r3k2r/1b4bq/8/8/8/8/7B/R3K2R w KQkq - 0 1",
    "r3k2r/8/3Q4/8/8/5q2/8/R3K2R b KQkq - 0 1",
    "2K2r2/4P3/8/8/8/8/8/3k4 w - - 0 1",
    "8/8/1P2K3/8/2n5/1q6/8/5k2 b - - 0 1",
    "8/P1k5/K7/8/8/8/8/8 w - - 0 1",
    "4k3/8/8/2KpP2r/8/8/8/8 w - d6 0 1",
    "4k3/8/8/r1pP1K2/8/8/8/8 w - c6 0 1",
];

fn check(pos: &Position) {
    let mut legal = 0;
    for code in 1..=u16::MAX {
        let m = arhanpassant::Move(code);
        let fast = pos.is_legal(m);
        assert_eq!(fast, pos.is_legal_by_generation(m), "{m:?} ({code}) in {}", pos.fen());
        legal += fast as usize;
    }
    assert_eq!(legal, pos.legal_moves().len(), "{}", pos.fen());
}

#[test]
fn fast_legality_matches_generation_on_every_move_code() {
    let mut seed = 0x2545_F491_4F6C_DD1Du64;
    let mut checked = 0;
    for fen in FENS {
        let start = Position::from_fen(fen).unwrap();
        check(&start);
        checked += 1;
        // Random walks from each position reach many more pins, checks and ep squares.
        for _ in 0..6 {
            let mut pos = start;
            for _ in 0..40 {
                let moves = pos.legal_moves();
                if moves.is_empty() {
                    break;
                }
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                pos.play(moves[(seed % moves.len() as u64) as usize]);
                if seed % 3 == 0 {
                    check(&pos);
                    checked += 1;
                }
            }
        }
    }
    assert!(checked > 300, "{checked} positions");
}
