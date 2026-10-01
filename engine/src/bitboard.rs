//! Bitboard helpers and attack lookups (magic bitboards for sliders).

use crate::types::{Bitboard, Color, Square};

mod tables {
    #![allow(clippy::unreadable_literal)]
    include!(concat!(env!("OUT_DIR"), "/tables.rs"));
}

pub use tables::{BETWEEN, LINE, ZOBRIST_CASTLING, ZOBRIST_EP, ZOBRIST_PIECES, ZOBRIST_SIDE};

pub const FILE_A: Bitboard = 0x0101_0101_0101_0101;
pub const FILE_H: Bitboard = FILE_A << 7;
pub const RANK_1: Bitboard = 0xFF;
pub const RANK_2: Bitboard = RANK_1 << 8;
pub const RANK_3: Bitboard = RANK_1 << 16;
pub const RANK_4: Bitboard = RANK_1 << 24;
pub const RANK_5: Bitboard = RANK_1 << 32;
pub const RANK_6: Bitboard = RANK_1 << 40;
pub const RANK_7: Bitboard = RANK_1 << 48;
pub const RANK_8: Bitboard = RANK_1 << 56;
pub const LIGHT_SQUARES: Bitboard = 0x55AA_55AA_55AA_55AA;
pub const DARK_SQUARES: Bitboard = !LIGHT_SQUARES;

#[inline(always)]
pub const fn bb(sq: Square) -> Bitboard {
    1u64 << sq
}

#[inline(always)]
pub const fn file_bb(file: u8) -> Bitboard {
    FILE_A << file
}

#[inline(always)]
pub const fn rank_bb(rank: u8) -> Bitboard {
    RANK_1 << (rank * 8)
}

#[inline(always)]
pub fn lsb(b: Bitboard) -> Square {
    debug_assert!(b != 0);
    b.trailing_zeros() as Square
}

#[inline(always)]
pub fn msb(b: Bitboard) -> Square {
    debug_assert!(b != 0);
    63 - b.leading_zeros() as Square
}

#[inline(always)]
pub fn pop_lsb(b: &mut Bitboard) -> Square {
    let s = lsb(*b);
    *b &= *b - 1;
    s
}

#[inline(always)]
pub const fn more_than_one(b: Bitboard) -> bool {
    b & b.wrapping_sub(1) != 0
}

/// Iterate the squares of a bitboard from a1 upwards.
#[derive(Copy, Clone)]
pub struct Squares(pub Bitboard);

impl Iterator for Squares {
    type Item = Square;
    #[inline(always)]
    fn next(&mut self) -> Option<Square> {
        if self.0 == 0 {
            None
        } else {
            Some(pop_lsb(&mut self.0))
        }
    }
}

#[inline(always)]
pub fn squares(b: Bitboard) -> Squares {
    Squares(b)
}

#[inline(always)]
pub fn knight_attacks(sq: Square) -> Bitboard {
    tables::KNIGHT_ATTACKS[sq as usize]
}

#[inline(always)]
pub fn king_attacks(sq: Square) -> Bitboard {
    tables::KING_ATTACKS[sq as usize]
}

#[inline(always)]
pub fn pawn_attacks(color: Color, sq: Square) -> Bitboard {
    tables::PAWN_ATTACKS[color.idx()][sq as usize]
}

#[inline(always)]
pub fn rook_attacks(sq: Square, occ: Bitboard) -> Bitboard {
    let s = sq as usize;
    let idx = ((occ & tables::ROOK_MASKS[s]).wrapping_mul(tables::ROOK_MAGICS[s])
        >> tables::ROOK_SHIFTS[s]) as usize
        + tables::ROOK_OFFSETS[s] as usize;
    debug_assert!(idx < tables::ROOK_TABLE.len());
    // SAFETY: the magic index is < 2^(64-shift), the size of this square's
    // slice, and offsets partition the table; checked by build.rs and perft.
    unsafe { *tables::ROOK_TABLE.get_unchecked(idx) }
}

#[inline(always)]
pub fn bishop_attacks(sq: Square, occ: Bitboard) -> Bitboard {
    let s = sq as usize;
    let idx = ((occ & tables::BISHOP_MASKS[s]).wrapping_mul(tables::BISHOP_MAGICS[s])
        >> tables::BISHOP_SHIFTS[s]) as usize
        + tables::BISHOP_OFFSETS[s] as usize;
    debug_assert!(idx < tables::BISHOP_TABLE.len());
    // SAFETY: as for rook_attacks.
    unsafe { *tables::BISHOP_TABLE.get_unchecked(idx) }
}

#[inline(always)]
pub fn queen_attacks(sq: Square, occ: Bitboard) -> Bitboard {
    rook_attacks(sq, occ) | bishop_attacks(sq, occ)
}

#[inline(always)]
pub fn between(a: Square, b: Square) -> Bitboard {
    BETWEEN[a as usize][b as usize]
}

#[inline(always)]
pub fn line(a: Square, b: Square) -> Bitboard {
    LINE[a as usize][b as usize]
}

/// All squares attacked by pawns of `color` standing on `pawns`.
#[inline(always)]
pub fn pawn_attacks_bb(color: Color, pawns: Bitboard) -> Bitboard {
    match color {
        Color::White => ((pawns & !FILE_A) << 7) | ((pawns & !FILE_H) << 9),
        Color::Black => ((pawns & !FILE_A) >> 9) | ((pawns & !FILE_H) >> 7),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn slow_rook(sq: Square, occ: Bitboard) -> Bitboard {
        let (f0, r0) = ((sq % 8) as i32, (sq / 8) as i32);
        let mut att = 0;
        for (df, dr) in [(1, 0), (-1, 0), (0, 1), (0, -1)] {
            let (mut f, mut r) = (f0 + df, r0 + dr);
            while (0..8).contains(&f) && (0..8).contains(&r) {
                let b = 1u64 << (r * 8 + f);
                att |= b;
                if occ & b != 0 {
                    break;
                }
                f += df;
                r += dr;
            }
        }
        att
    }

    #[test]
    fn rook_magics_match_ray_walk() {
        let mut x = 0x1234_5678_9abc_def1u64;
        for _ in 0..20_000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let occ = x & (x >> 3);
            let sq = (x % 64) as Square;
            assert_eq!(rook_attacks(sq, occ), slow_rook(sq, occ), "sq {sq} occ {occ:x}");
        }
    }

    #[test]
    fn line_and_between() {
        // a1..h8 diagonal
        assert_eq!(between(0, 63).count_ones(), 6);
        assert_eq!(line(0, 63).count_ones(), 8);
        assert_eq!(between(0, 1), 0);
        assert_eq!(line(0, 10), 0); // a1-c2 not aligned
        assert_eq!(between(4, 60), 0x0010_1010_1010_1000); // e1-e8
    }
}
