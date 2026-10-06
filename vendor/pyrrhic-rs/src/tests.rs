use std::str::FromStr;

use crate::{
    engine_adapter::{Color, EngineAdapter},
    tablebases::{TableBases, WdlProbeResult},
    DtzProbeValue, TBError,
};
use cozy_chess::*;

const SYZYGY_PATH: &str = env!("SYZYGY_PATH");
#[derive(Copy, Clone)]
struct CozyChessAdapter;

impl EngineAdapter for CozyChessAdapter {
    fn pawn_attacks(color: Color, sq: u64) -> u64 {
        let attacks = get_pawn_attacks(
            Square::index(sq as usize),
            if color == Color::Black {
                cozy_chess::Color::Black
            } else {
                cozy_chess::Color::White
            },
        );
        attacks.0
    }
    fn knight_attacks(sq: u64) -> u64 {
        get_knight_moves(Square::index(sq as usize)).0
    }
    fn bishop_attacks(sq: u64, occ: u64) -> u64 {
        get_bishop_moves(Square::index(sq as usize), BitBoard(occ)).0
    }
    fn rook_attacks(sq: u64, occ: u64) -> u64 {
        get_rook_moves(Square::index(sq as usize), BitBoard(occ)).0
    }
    fn king_attacks(sq: u64) -> u64 {
        get_king_moves(Square::index(sq as usize)).0
    }
    fn queen_attacks(sq: u64, occ: u64) -> u64 {
        (get_bishop_moves(Square::index(sq as usize), BitBoard(occ))
            | get_rook_moves(Square::index(sq as usize), BitBoard(occ)))
        .0
    }
}

#[test]
fn test_probe_kpvk() {
    let tb = loop {
        let test = TableBases::<CozyChessAdapter>::new(SYZYGY_PATH);
        if let Ok(tb) = test {
            break tb;
        }
    };
    let test_pos_wins = [
        ("6k1/8/8/3P4/4K3/8/8/8 w - - 0 1", 1),
        ("8/7k/1p6/1P6/7K/8/8/8 w - - 0 1", 21),
    ];
    let test_pos_draw = "6k1/8/8/3P4/4K3/8/8/8 b - - 0 1";

    for (win_pos, dtz_expected) in test_pos_wins {
        let test_board_win = Board::from_str(win_pos).unwrap();

        let wdl_win = tb.probe_wdl(
            test_board_win.colors(cozy_chess::Color::White).0,
            test_board_win.colors(cozy_chess::Color::Black).0,
            test_board_win.pieces(Piece::King).0,
            test_board_win.pieces(Piece::Queen).0,
            test_board_win.pieces(Piece::Rook).0,
            test_board_win.pieces(Piece::Bishop).0,
            test_board_win.pieces(Piece::Knight).0,
            test_board_win.pieces(Piece::Pawn).0,
            0, // no ep square
            test_board_win.side_to_move() == cozy_chess::Color::White,
        );
        assert!(wdl_win == Ok(WdlProbeResult::Win));
        let dtz_result = tb.probe_root(
            test_board_win.colors(cozy_chess::Color::White).0,
            test_board_win.colors(cozy_chess::Color::Black).0,
            test_board_win.pieces(Piece::King).0,
            test_board_win.pieces(Piece::Queen).0,
            test_board_win.pieces(Piece::Rook).0,
            test_board_win.pieces(Piece::Bishop).0,
            test_board_win.pieces(Piece::Knight).0,
            test_board_win.pieces(Piece::Pawn).0,
            0,
            0,
            test_board_win.side_to_move() == cozy_chess::Color::White,
        );

        assert!(match dtz_result.unwrap().root {
            DtzProbeValue::DtzResult(result) => result.dtz == dtz_expected,
            _ => false,
        })
    }
    let test_board_draw = Board::from_str(test_pos_draw).unwrap();
    let wdl_draw = tb.probe_wdl(
        test_board_draw.colors(cozy_chess::Color::White).0,
        test_board_draw.colors(cozy_chess::Color::Black).0,
        test_board_draw.pieces(Piece::King).0,
        test_board_draw.pieces(Piece::Queen).0,
        test_board_draw.pieces(Piece::Rook).0,
        test_board_draw.pieces(Piece::Bishop).0,
        test_board_draw.pieces(Piece::Knight).0,
        test_board_draw.pieces(Piece::Pawn).0,
        0, // no ep square
        test_board_draw.side_to_move() == cozy_chess::Color::White,
    );

    assert!(wdl_draw == Ok(WdlProbeResult::Draw));
}

#[test]
fn test_double_init() {
    let first_tb = loop {
        let test = TableBases::<CozyChessAdapter>::new(SYZYGY_PATH);
        if let Ok(tb) = test {
            break tb;
        }
    };
    let second_tb = TableBases::<CozyChessAdapter>::new(SYZYGY_PATH);

    assert!(matches!(second_tb, Err(TBError::AlreadyInitialized)));
    std::hint::black_box(first_tb);
}

#[test]
fn test_multithread() {
    let pos = "8/7k/1p6/1P6/7K/8/8/8 w - - 0 1";
    let first_tb = loop {
        let test = TableBases::<CozyChessAdapter>::new(SYZYGY_PATH);
        if let Ok(tb) = test {
            break tb;
        }
    };
    let second_tb = first_tb.clone();

    let worker = std::thread::spawn(move || {
        let board = Board::from_fen(pos, false).unwrap();
        for _ in 0..1000 {
            std::hint::black_box({
                let _ = second_tb.probe_wdl(
                    board.colors(cozy_chess::Color::White).0,
                    board.colors(cozy_chess::Color::Black).0,
                    board.pieces(Piece::King).0,
                    board.pieces(Piece::Queen).0,
                    board.pieces(Piece::Rook).0,
                    board.pieces(Piece::Bishop).0,
                    board.pieces(Piece::Knight).0,
                    board.pieces(Piece::Pawn).0,
                    0,
                    board.side_to_move() == cozy_chess::Color::White,
                );
            });
        }
    });
    let board = Board::from_fen(pos, false).unwrap();
    for _ in 0..10000 {
        std::hint::black_box({
            let _ = first_tb.probe_wdl(
                board.colors(cozy_chess::Color::White).0,
                board.colors(cozy_chess::Color::Black).0,
                board.pieces(Piece::King).0,
                board.pieces(Piece::Queen).0,
                board.pieces(Piece::Rook).0,
                board.pieces(Piece::Bishop).0,
                board.pieces(Piece::Knight).0,
                board.pieces(Piece::Pawn).0,
                0,
                board.side_to_move() == cozy_chess::Color::White,
            );
        });
    }
    worker.join().unwrap();
}
