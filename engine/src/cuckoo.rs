//! Upcoming-repetition detection (Marcel van Kervinck's cuckoo tables).
//!
//! Every reversible move of a piece (not a pawn) between two squares changes
//! the position key by Z[piece][a] ^ Z[piece][b] ^ Z[side]. Those keys are
//! stored in a cuckoo hash, so search can ask in a few lookups whether the
//! side to move has a move that returns to an earlier position.

use crate::bitboard::*;
use crate::types::*;
use std::sync::OnceLock;

const SIZE: usize = 8192;

pub struct Cuckoo {
    keys: [u64; SIZE],
    moves: [Move; SIZE],
}

#[inline(always)]
fn h1(k: u64) -> usize {
    (k & (SIZE as u64 - 1)) as usize
}

#[inline(always)]
fn h2(k: u64) -> usize {
    ((k >> 16) & (SIZE as u64 - 1)) as usize
}

fn attacks_empty(pt: PieceType, sq: Square) -> Bitboard {
    match pt {
        PieceType::Knight => knight_attacks(sq),
        PieceType::Bishop => bishop_attacks(sq, 0),
        PieceType::Rook => rook_attacks(sq, 0),
        PieceType::Queen => queen_attacks(sq, 0),
        PieceType::King => king_attacks(sq),
        PieceType::Pawn => 0,
    }
}

/// The table, built on first use (3,668 piece moves).
pub fn table() -> &'static Cuckoo {
    static TABLE: OnceLock<Box<Cuckoo>> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = Box::new(Cuckoo { keys: [0; SIZE], moves: [Move::NULL; SIZE] });
        let mut count = 0;
        for color in [Color::White, Color::Black] {
            for pt in [PieceType::Knight, PieceType::Bishop, PieceType::Rook, PieceType::Queen, PieceType::King] {
                let piece = Piece::new(color, pt);
                for s1 in 0..64u8 {
                    for s2 in s1 + 1..64u8 {
                        if attacks_empty(pt, s1) & bb(s2) == 0 {
                            continue;
                        }
                        let mut mv = Move::new(s1, s2, flag::QUIET);
                        let mut key = ZOBRIST_PIECES[piece.idx()][s1 as usize] ^ ZOBRIST_PIECES[piece.idx()][s2 as usize] ^ ZOBRIST_SIDE;
                        let mut i = h1(key);
                        loop {
                            std::mem::swap(&mut t.keys[i], &mut key);
                            std::mem::swap(&mut t.moves[i], &mut mv);
                            if mv.is_null() {
                                break;
                            }
                            i = if i == h1(key) { h2(key) } else { h1(key) };
                        }
                        count += 1;
                    }
                }
            }
        }
        assert_eq!(count, 3668);
        t
    })
}

impl Cuckoo {
    /// The reversible piece move whose key change is `key`, if any.
    #[inline(always)]
    pub fn lookup(&self, key: u64) -> Option<Move> {
        let i = h1(key);
        if self.keys[i] == key {
            return Some(self.moves[i]);
        }
        let j = h2(key);
        (self.keys[j] == key).then_some(self.moves[j])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::position::Position;

    #[test]
    fn every_reversible_knight_move_is_found() {
        let t = table();
        let pos = Position::from_fen("4k3/8/8/8/3N4/8/8/4K3 w - - 0 1").unwrap();
        for m in pos.legal_moves().iter() {
            let key = pos.hash() ^ pos.after(m).hash();
            let found = t.lookup(key).expect("reversible move in table");
            assert_eq!((found.from().min(found.to()), found.from().max(found.to())), (m.from().min(m.to()), m.from().max(m.to())));
        }
    }
}
