//! Move-ordering statistics learned during search.

use crate::types::*;

pub const HIST_MAX: i32 = 16384;

/// (piece index 0..12 or 12 for "none", destination square)
#[derive(Copy, Clone, Default, PartialEq, Eq, Debug)]
pub struct ContKey {
    pub piece: u8,
    pub to: u8,
}

impl ContKey {
    pub const NONE: ContKey = ContKey { piece: 12, to: 0 };
}

pub struct History {
    /// [colour][from][to]
    pub butterfly: [[[i16; 64]; 64]; 2],
    /// [prev piece][prev to][piece][to]
    pub cont: Vec<[[[i16; 64]; 12]; 64]>,
    /// [piece][to][captured type]
    pub capture: [[[i16; 6]; 64]; 12],
    /// [prev piece][prev to] -> refutation
    pub counter: [[Move; 64]; 13],
}

#[inline(always)]
fn gravity(entry: &mut i16, bonus: i32) {
    let e = *entry as i32;
    let b = bonus.clamp(-HIST_MAX, HIST_MAX);
    *entry = (e + b - e * b.abs() / HIST_MAX) as i16;
}

impl History {
    pub fn new() -> Box<History> {
        Box::new(History {
            butterfly: [[[0; 64]; 64]; 2],
            cont: vec![[[[0; 64]; 12]; 64]; 13],
            capture: [[[0; 6]; 64]; 12],
            counter: [[Move::NULL; 64]; 13],
        })
    }

    pub fn clear(&mut self) {
        self.butterfly = [[[0; 64]; 64]; 2];
        for c in self.cont.iter_mut() {
            *c = [[[0; 64]; 12]; 64];
        }
        self.capture = [[[0; 6]; 64]; 12];
        self.counter = [[Move::NULL; 64]; 13];
    }

    #[inline(always)]
    pub fn cont_score(&self, key: ContKey, piece: Piece, to: Square) -> i32 {
        self.cont[key.piece as usize][key.to as usize][piece.idx()][to as usize] as i32
    }

    /// Combined quiet-move score: butterfly + 1-ply and 2-ply continuation.
    #[inline(always)]
    pub fn quiet_score(&self, stm: Color, piece: Piece, m: Move, c1: ContKey, c2: ContKey) -> i32 {
        self.butterfly[stm.idx()][m.from() as usize][m.to() as usize] as i32
            + self.cont_score(c1, piece, m.to())
            + self.cont_score(c2, piece, m.to())
    }

    pub fn update_quiet(&mut self, stm: Color, piece: Piece, m: Move, c1: ContKey, c2: ContKey, bonus: i32) {
        gravity(&mut self.butterfly[stm.idx()][m.from() as usize][m.to() as usize], bonus);
        for c in [c1, c2] {
            if c.piece != 12 {
                gravity(&mut self.cont[c.piece as usize][c.to as usize][piece.idx()][m.to() as usize], bonus);
            }
        }
    }

    #[inline(always)]
    pub fn capture_score(&self, piece: Piece, to: Square, victim: PieceType) -> i32 {
        self.capture[piece.idx()][to as usize][victim.idx()] as i32
    }

    pub fn update_capture(&mut self, piece: Piece, to: Square, victim: PieceType, bonus: i32) {
        gravity(&mut self.capture[piece.idx()][to as usize][victim.idx()], bonus);
    }
}
