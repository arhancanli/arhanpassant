//! ArhanPassant: a UCI chess engine and chess library.
//!
//! The library exposes legal move generation, FEN, SAN and perft so it can be
//! used on its own; the engine adds search, evaluation and self-play tooling.
//!
//! ```
//! use arhanpassant::Position;
//!
//! let pos = Position::from_fen("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3").unwrap();
//! for m in pos.legal_moves().iter() {
//!     println!("{} {}", m.to_uci(), pos.san(m));
//! }
//! assert_eq!(arhanpassant::perft(&Position::startpos(), 5), 4_865_609);
//! ```

pub mod bench;
pub mod bitboard;
pub mod datagen;
pub mod eval;
pub mod game;
pub mod history;
pub mod movegen;
pub mod movepick;
pub mod nnue;
pub mod params;
pub mod position;
pub mod search;
pub mod notation;
pub mod see;
pub mod tt;
pub mod time;
pub mod types;
pub mod uci;

pub use movegen::{perft, MoveList};
pub use position::{FenError, Position, START_FEN};
pub use types::{Color, Move, Piece, PieceType, Square};
