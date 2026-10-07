//! Chess960: FEN castling rights, king-takes-rook notation, castling moves, and the
//! network's incremental update after castling. Move counts against python-chess
//! are checked by tools/chess960_perft.py.

use arhanpassant::nnue::{Accumulators, Network};
use arhanpassant::Position;
use std::sync::Arc;

/// Positions where castling is legal for the side to move (edge cases, then random
/// Chess960 games from python-chess).
const CASTLING: &[&str] = &[
    "1rk4r/8/8/8/8/8/8/1RK4R w HBhb - 0 1",
    "k6r/8/8/8/8/8/8/5KR1 w G - 0 1",
    "4k3/8/8/8/8/8/8/R5KR w HA - 0 1",
    "r3k2r/8/8/8/8/8/8/RK4R1 w GAha - 0 1",
    "nbrqbknr/ppppp1p1/8/5p1p/P7/3P3N/1PP1PPPP/NBRQBK1R w HChc - 0 4",
    "1rqb2kr/ppp1pp1n/1n4p1/3p3p/b4K2/1P3P1P/P1PPP1P1/NRQBBNR1 b hb - 0 7",
    "bnrknrqb/pppppppp/8/8/8/8/PPPPPPPP/BNRKNRQB w FCfc - 0 1",
    "rk5r/pppqpn2/7p/n5p1/1P1p1BP1/P2b1P2/6NP/R1QK1R1B b a - 3 25",
    "rnnqbkrb/pppppppp/8/8/8/8/PPPPPPPP/RNNQBKRB w GAga - 0 1",
    "r1k1bn2/pp5r/n7/B1ppppp1/2PP3p/NP3PP1/RP2PN1P/1K2QR2 b a - 5 26",
    "nrknqrbb/pp1ppp1p/2p5/8/2P3pP/P1N1PP2/1PNP2PB/1RK1QR1B w FBfb - 5 9",
    "rn1bk1rq/pppnpppp/8/3p4/6b1/P2P1N2/1PP1PPPP/R1BBKNRQ b GAga - 4 4",
    "rk2br1n/p3p2p/1n2qpp1/1p1p4/2pPPP2/KP4P1/N1Pb2QP/R2BBR1N b fa - 2 13",
    "bb4rr/ppp1pppk/5Bn1/2np4/PPP1BP1p/3q2N1/1N1PP1PP/2Q1R1KR w HE - 8 12",
    "qbnrbk1r/pppp2p1/5p1n/2N1p2p/5P2/5N2/PPPPP1PP/QB1RBK1R w HDhd - 2 5",
    "2kr4/rpp4p/2np1Pnb/p5p1/N1P1Pp2/1PQ2P2/P4RPP/RNK3BB b Ad - 2 24",
    "qrbbnkrn/pppppppp/8/8/8/8/PPPPPPPP/QRBBNKRN w GBgb - 0 1",
    "qrnk1rbb/pp1pp1pp/3n4/2p2p2/7P/3NP3/PPPP1PP1/QR1KNRBB w FBfb - 0 4",
    "brnqkrnb/p1pppppp/8/1p6/8/7N/PPPPPPPP/BRNQKR1B w FBfb - 0 2",
    "r1kn1rqb/pp1pp1pp/3n4/2p2p1b/1PPP4/1NN5/P3PPPP/RK2BRQB w FA - 2 6",
    "qnrkrbbn/pppppppp/8/8/8/8/PPPPPPPP/QNRKRBBN w ECec - 0 1",
    "brqbnkrn/pppppppp/8/8/8/8/PPPPPPPP/BRQBNKRN w GBgb - 0 1",
    "n1r3kr/1p2Qpb1/1n6/3PP1pp/1q3B2/1bp2P2/2P2KPP/N1RBN2R b h - 0 24",
    "n3k2r/r4b2/P2ppq2/b1P3pp/5PQn/2NPN1PP/P6B/1R2KB1R b Hh - 0 29",
    "brqnnkrb/pppppppp/8/8/8/8/PPPPPPPP/BRQNNKRB w GBgb - 0 1",
    "brnkrbqn/pp1ppppp/8/2p5/8/1N6/PPPPPPPP/BR1KRBQN w EBeb - 0 2",
    "nrbqnkrb/pppppppp/8/8/8/8/PPPPPPPP/NRBQNKRB w GBgb - 0 1",
    "bnrbn1kr/pppppqpp/8/5p2/8/P4NP1/1PPPPP1P/BNRB1QKR b HChc - 0 3",
    "rbbq1nkr/p2pppp1/2p1n2p/1p6/2N5/P1PB2N1/RP1PPPPP/2B1Q1KR w Hha - 2 8",
    "rbbqn1kr/p1pnppp1/7p/1p1p1P2/8/P2P2PP/1PP1P3/RBBQNNKR b HAha - 0 6",
    "nrbbqkrn/pppppppp/8/8/8/8/PPPPPPPP/NRBBQKRN w GBgb - 0 1",
    "r1bnk1rb/pp1p2pp/2p5/2q1pp2/PPn2P2/3N2PP/3BP1Q1/NR2K1RB b GBg - 3 10",
    "nrnbbkrq/pppppppp/8/8/8/8/PPPPPPPP/NRNBBKRQ w GBgb - 0 1",
    "bnrkqrnb/pppppppp/8/8/8/8/PPPPPPPP/BNRKQRNB w FCfc - 0 1",
    "brqknnrb/p3pppp/1ppp4/8/2P5/2Q4P/PP1PPPP1/BR1KNNRB w GBgb - 0 4",
    "rnkrbnqb/1pp1p1p1/3p1p2/p6p/1B1P2P1/4N3/PPP1PPQP/RNKR3B w DAda - 0 6",
    "bnrqknrb/ppp1ppp1/8/3p3p/1P6/4N1P1/P1PPPP1P/BNRQK1RB w GCgc - 0 4",
    "2rnkrbb/qpppppp1/p2n4/3N3p/2P5/3NP3/PP1P1PPP/QR2KRBB w FBf - 4 6",
    "nqrnkbb1/ppppp3/6pr/5p1p/8/1P1P1P2/P1P1PNPP/NQR1KBBR w HCc - 2 5",
    "nr2kbnr/p4qp1/bp1p3p/2p1pp2/2P2P2/1P6/P1QPPNPP/NRB2BKR b hb - 3 11",
    "nqrknbbr/pppppppp/8/8/8/8/PPPPPPPP/NQRKNBBR w HChc - 0 1",
    "1nr1kb1r/ppnqp1p1/8/2pp1p1p/P4P1P/1N4P1/2PPP3/1NRQKBBR b HChc - 3 9",
    "rbb1qnkr/pppppppp/2n5/8/8/6N1/PPPPPPPP/RBBNQ1KR w HAha - 2 2",
    "1n1bqrkr/p1pppp2/2b4p/5n2/pQP1P1p1/1B1P4/NP3PPP/BN3RKR w HFhf - 0 10",
];

#[test]
fn fen_round_trip() {
    for fen in CASTLING {
        let pos = Position::from_fen_mode(fen, true).expect(fen);
        let again = Position::from_fen_mode(&pos.fen(), true).expect(fen);
        // Same position, same rights and rooks (the text may switch to KQkq when the rooks stand on standard squares).
        assert_eq!(pos, again, "{fen} -> {}", pos.fen());
        assert_eq!(pos.castling_rights(), again.castling_rights());
        assert_eq!(pos.castle_rooks(), again.castle_rooks());
    }
    // Standard positions keep KQkq, and X-FEN letters name the outermost rooks.
    let start = Position::from_fen_mode("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", true).unwrap();
    assert_eq!(start.fen().split(' ').nth(2), Some("KQkq"));
    let xfen = Position::from_fen_mode("1r2k1r1/8/8/8/8/8/8/RK4R1 w KQkq - 0 1", true).unwrap();
    assert_eq!(xfen.fen().split(' ').nth(2), Some("GAgb"));
    // Without Chess960, rights the standard game cannot have are dropped.
    let plain = Position::from_fen("1r2k1r1/8/8/8/8/8/8/RK4R1 w GAgb - 0 1").unwrap();
    assert_eq!(plain.castling_rights(), 0);
}

#[test]
fn king_takes_rook_notation() {
    // King f1, rook h1: f1g1 is a plain king move, f1h1 castles.
    let pos = Position::from_fen_mode("k7/8/8/8/8/8/8/5K1R w H - 0 1", true).unwrap();
    let plain = pos.parse_uci_move_mode("f1g1", true).unwrap();
    assert!(!plain.is_castle());
    let castle = pos.parse_uci_move_mode("f1h1", true).unwrap();
    assert!(castle.is_castle());
    assert_eq!(pos.uci_move(castle, true), "f1h1");
    let after = pos.after(castle);
    assert_eq!(after.fen().split(' ').next(), Some("k7/8/8/8/8/8/8/5RK1"));
    // King c1, rook b1: O-O-O leaves the king on c1 and puts the rook on d1.
    let pos = Position::from_fen_mode("1rk4r/8/8/8/8/8/8/1RK4R w HBhb - 0 1", true).unwrap();
    let ooo = pos.parse_uci_move_mode("c1b1", true).unwrap();
    assert!(ooo.is_castle());
    assert_eq!(pos.after(ooo).fen().split(' ').take(3).collect::<Vec<_>>(), ["1rk4r/8/8/8/8/8/8/2KR3R", "b", "hb"]);
    // Standard chess keeps accepting both e1g1 and e1h1.
    let std = Position::from_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1").unwrap();
    assert!(std.parse_uci_move("e1g1").unwrap().is_castle());
    assert!(std.parse_uci_move("e1h1").unwrap().is_castle());
    assert_eq!(std.uci_move(std.parse_uci_move("e1h1").unwrap(), false), "e1g1");
}

#[test]
fn every_position_has_castling_and_network_updates_match() {
    let net = Network::embedded().map(Arc::new);
    let mut castles = 0;
    for fen in CASTLING {
        let pos = Position::from_fen_mode(fen, true).expect(fen);
        let moves: Vec<_> = pos.legal_moves().iter().filter(|m| m.is_castle()).collect();
        assert!(!moves.is_empty(), "no castling found in {fen}");
        for m in moves {
            castles += 1;
            let child = pos.after(m);
            assert_eq!(child.hash(), child.compute_hash(), "{fen} {}", pos.uci_move(m, true));
            if let Some(net) = &net {
                let mut acc = Accumulators::new(net.clone(), 4);
                acc.refresh(&pos, 0);
                acc.push(&pos, m, &child, 1);
                assert_eq!(acc.evaluate(&child, 1), net.evaluate_full(&child), "{fen} {}", pos.uci_move(m, true));
            }
        }
    }
    assert!(castles >= CASTLING.len());
}
