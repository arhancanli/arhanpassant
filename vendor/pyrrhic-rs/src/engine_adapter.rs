/// The color of a chess player
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum Color {
    Black = 0,
    White = 1,
}

/// The type of a chess piece
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum Piece {
    Pawn = 0,
    Knight = 1,
    Bishop = 2,
    Rook = 3,
    Queen = 4,
    King = 5,
}

/// Interface to plug in existing chess engine move generation
/// ## Notes:
/// All functions in this trait output a *bitboard* of moves, which is a 64-bit mask
/// with `1` bits in the squares the piece can move to, and `0` bits everywhere else.
/// 
/// The least significant bit corresponds to the A1 square on a chessboard and increasing
/// bit indices move left-to-right, then bottom-to-top (from white's perspective).
/// 
/// E.g. square A1 is bit 0, B1 is bit 1, H1 is bit 7 and A2 is bit 8.
/// 
/// The `square` argument of all these functions also corresponds to these bit indices.
/// e.g. `[EngineAdapter::knight_attacks(1)]` generates moves for a knight on B1.
/// 
/// Functions for sliding pieces additionally require an 'occupancy mask', which is a bitboard with
/// `1` bits in all squares containing any piece, and `0` bits on empty squares
pub trait EngineAdapter: Clone {
    /// Generate attacking moves for a pawn of a given color on a given square
    fn pawn_attacks(color: Color, square: u64) -> u64;
    /// Generate moves for a knight on a given square
    fn knight_attacks(square: u64) -> u64;
    /// Generate moves for a bishop on a given square with a given occupancy mask
    fn bishop_attacks(square: u64, occupied: u64) -> u64;
    /// Generate moves for a rook on a given square with a given occupancy mask
    fn rook_attacks(square: u64, occupied: u64) -> u64;
    /// Generate moves for a queen on a given square with a given occupancy mask
    fn queen_attacks(square: u64, occupied: u64) -> u64;
    /// Generate moves for a king on a given square
    fn king_attacks(square: u64) -> u64;
}
