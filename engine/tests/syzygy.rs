//! Syzygy tablebase probing against verdicts and best moves from the Lichess
//! tablebase server, on the small tables kept in tests/syzygy/.
//! (The tables are a process-wide resource, so everything runs in one test.)

use arhanpassant::syzygy::{self, Wdl};
use arhanpassant::Position;

#[test]
fn probes_match_the_lichess_tablebase() {
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/syzygy");
    assert_eq!(syzygy::init(dir), Ok(3), "KPvK, KRvK and KQvK are 3-piece tables");

    let cases: &[(&str, Wdl, &[&str])] = &[
        // (position, verdict for the side to move, root moves kept)
        ("6k1/8/8/3P4/4K3/8/8/8 w - - 0 1", Wdl::Win, &["d5d6"]),
        ("6k1/8/8/3P4/4K3/8/8/8 b - - 0 1", Wdl::Draw, &["g8f7", "g8f8"]),
        ("8/8/8/8/8/2k5/8/K1R5 b - - 0 1", Wdl::Loss, &["c3d3", "c3d4"]),
        ("8/8/8/8/8/2k5/8/KQ6 w - - 0 1", Wdl::Win, &["b1e4"]),
    ];
    for &(fen, wdl, kept) in cases {
        let pos = Position::from_fen(fen).unwrap();
        assert_eq!(syzygy::probe_wdl(&pos), Some(wdl), "{fen}");
        let (root, moves) = syzygy::root_moves(&pos).expect(fen);
        assert_eq!(root, wdl, "{fen}");
        let mut got: Vec<String> = moves.iter().map(|m| m.to_string()).collect();
        got.sort();
        assert_eq!(got, kept, "{fen}");
    }

    // Positions the tables do not cover are not probed.
    let five = Position::from_fen("8/8/8/8/8/2k5/1p6/KQ1R4 w - - 0 1").unwrap();
    assert!(!syzygy::probeable(&five, 7));
    let castling = Position::from_fen("4k3/8/8/8/8/8/8/R3K3 w Q - 0 1").unwrap();
    assert!(!syzygy::probeable(&castling, 7));

    assert_eq!(syzygy::init(""), Ok(0));
    assert_eq!(syzygy::max_pieces(), 0);
}
