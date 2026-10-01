//! Core value types: colours, pieces, squares and moves.

use std::fmt;

pub type Bitboard = u64;
pub type Square = u8;

pub const NO_SQUARE: Square = 64;

#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
#[repr(u8)]
pub enum Color {
    White = 0,
    Black = 1,
}

impl Color {
    #[inline(always)]
    pub const fn idx(self) -> usize {
        self as usize
    }

    #[inline(always)]
    pub const fn flip(self) -> Color {
        match self {
            Color::White => Color::Black,
            Color::Black => Color::White,
        }
    }

    #[inline(always)]
    pub const fn from_idx(i: usize) -> Color {
        if i == 0 {
            Color::White
        } else {
            Color::Black
        }
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
#[repr(u8)]
pub enum PieceType {
    Pawn = 0,
    Knight = 1,
    Bishop = 2,
    Rook = 3,
    Queen = 4,
    King = 5,
}

impl PieceType {
    pub const ALL: [PieceType; 6] = [
        PieceType::Pawn,
        PieceType::Knight,
        PieceType::Bishop,
        PieceType::Rook,
        PieceType::Queen,
        PieceType::King,
    ];

    #[inline(always)]
    pub const fn idx(self) -> usize {
        self as usize
    }

    #[inline(always)]
    pub const fn from_idx(i: usize) -> PieceType {
        Self::ALL[i]
    }

    pub const fn to_char(self) -> char {
        match self {
            PieceType::Pawn => 'p',
            PieceType::Knight => 'n',
            PieceType::Bishop => 'b',
            PieceType::Rook => 'r',
            PieceType::Queen => 'q',
            PieceType::King => 'k',
        }
    }

    pub fn from_char(c: char) -> Option<PieceType> {
        Some(match c.to_ascii_lowercase() {
            'p' => PieceType::Pawn,
            'n' => PieceType::Knight,
            'b' => PieceType::Bishop,
            'r' => PieceType::Rook,
            'q' => PieceType::Queen,
            'k' => PieceType::King,
            _ => return None,
        })
    }
}

/// A coloured piece packed as `color * 6 + piece_type` (0..12).
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub struct Piece(pub u8);

impl Piece {
    pub const NONE: Piece = Piece(12);

    #[inline(always)]
    pub const fn new(color: Color, pt: PieceType) -> Piece {
        Piece(color as u8 * 6 + pt as u8)
    }

    #[inline(always)]
    pub const fn is_none(self) -> bool {
        self.0 == 12
    }

    #[inline(always)]
    pub const fn color(self) -> Color {
        Color::from_idx((self.0 / 6) as usize)
    }

    #[inline(always)]
    pub const fn piece_type(self) -> PieceType {
        PieceType::from_idx((self.0 % 6) as usize)
    }

    #[inline(always)]
    pub const fn idx(self) -> usize {
        self.0 as usize
    }

    pub fn to_char(self) -> char {
        let c = self.piece_type().to_char();
        match self.color() {
            Color::White => c.to_ascii_uppercase(),
            Color::Black => c,
        }
    }
}

#[inline(always)]
pub const fn file_of(sq: Square) -> u8 {
    sq & 7
}

#[inline(always)]
pub const fn rank_of(sq: Square) -> u8 {
    sq >> 3
}

#[inline(always)]
pub const fn make_square(file: u8, rank: u8) -> Square {
    rank * 8 + file
}

/// Mirror a square vertically (a1 <-> a8).
#[inline(always)]
pub const fn flip_rank(sq: Square) -> Square {
    sq ^ 56
}

pub fn square_name(sq: Square) -> String {
    let f = (b'a' + file_of(sq)) as char;
    let r = (b'1' + rank_of(sq)) as char;
    format!("{f}{r}")
}

pub fn parse_square(s: &str) -> Option<Square> {
    let b = s.as_bytes();
    if b.len() != 2 || !(b'a'..=b'h').contains(&b[0]) || !(b'1'..=b'8').contains(&b[1]) {
        return None;
    }
    Some(make_square(b[0] - b'a', b[1] - b'1'))
}

/// A move packed into 16 bits: from (6) | to (6) | flags (4).
#[derive(Copy, Clone, PartialEq, Eq, Hash, Default)]
pub struct Move(pub u16);

pub mod flag {
    pub const QUIET: u16 = 0;
    pub const DOUBLE_PUSH: u16 = 1;
    pub const KING_CASTLE: u16 = 2;
    pub const QUEEN_CASTLE: u16 = 3;
    pub const CAPTURE: u16 = 4;
    pub const EP_CAPTURE: u16 = 5;
    pub const PROMO_N: u16 = 8;
    pub const PROMO_B: u16 = 9;
    pub const PROMO_R: u16 = 10;
    pub const PROMO_Q: u16 = 11;
    pub const PROMO_CAPTURE_N: u16 = 12;
    pub const PROMO_CAPTURE_B: u16 = 13;
    pub const PROMO_CAPTURE_R: u16 = 14;
    pub const PROMO_CAPTURE_Q: u16 = 15;
}

impl Move {
    pub const NULL: Move = Move(0);

    #[inline(always)]
    pub const fn new(from: Square, to: Square, flags: u16) -> Move {
        Move(from as u16 | (to as u16) << 6 | flags << 12)
    }

    #[inline(always)]
    pub const fn from(self) -> Square {
        (self.0 & 63) as Square
    }

    #[inline(always)]
    pub const fn to(self) -> Square {
        ((self.0 >> 6) & 63) as Square
    }

    #[inline(always)]
    pub const fn flags(self) -> u16 {
        self.0 >> 12
    }

    #[inline(always)]
    pub const fn is_null(self) -> bool {
        self.0 == 0
    }

    #[inline(always)]
    pub const fn is_capture(self) -> bool {
        self.flags() & flag::CAPTURE != 0
    }

    #[inline(always)]
    pub const fn is_promotion(self) -> bool {
        self.flags() & 8 != 0
    }

    #[inline(always)]
    pub const fn is_castle(self) -> bool {
        matches!(self.flags(), flag::KING_CASTLE | flag::QUEEN_CASTLE)
    }

    #[inline(always)]
    pub const fn is_ep(self) -> bool {
        self.flags() == flag::EP_CAPTURE
    }

    /// Captures and queen promotions: the moves quiescence search looks at.
    #[inline(always)]
    pub const fn is_noisy(self) -> bool {
        self.is_capture() || self.flags() == flag::PROMO_Q
    }

    #[inline(always)]
    pub const fn is_quiet(self) -> bool {
        !self.is_noisy()
    }

    pub const fn promotion(self) -> Option<PieceType> {
        if !self.is_promotion() {
            return None;
        }
        Some(match self.flags() & 3 {
            0 => PieceType::Knight,
            1 => PieceType::Bishop,
            2 => PieceType::Rook,
            _ => PieceType::Queen,
        })
    }

    /// Long algebraic (UCI) notation, e.g. `e2e4`, `e7e8q`. Castling is king-to-target.
    pub fn to_uci(self) -> String {
        if self.is_null() {
            return "0000".to_string();
        }
        let mut s = square_name(self.from());
        s.push_str(&square_name(self.to()));
        if let Some(p) = self.promotion() {
            s.push(p.to_char());
        }
        s
    }
}

impl fmt::Debug for Move {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_uci())
    }
}

impl fmt::Display for Move {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_uci())
    }
}

pub mod castle {
    pub const WK: u8 = 1;
    pub const WQ: u8 = 2;
    pub const BK: u8 = 4;
    pub const BQ: u8 = 8;
}
