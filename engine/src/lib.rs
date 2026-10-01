//! ArhanPassant: a UCI chess engine and chess library.
//!
//! The library exposes legal move generation, FEN and perft so it can be used
//! on its own; the engine adds search, evaluation and self-play tooling.

pub mod bench;
pub mod bitboard;
pub mod datagen;
pub mod eval;
pub mod history;
pub mod movegen;
pub mod movepick;
pub mod nnue;
pub mod params;
pub mod position;
pub mod search;
pub mod see;
pub mod tt;
pub mod types;
pub mod uci;

pub use movegen::{perft, MoveList};
pub use position::{FenError, Position, START_FEN};
pub use types::{Color, Move, Piece, PieceType, Square};
