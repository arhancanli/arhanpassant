//! NNUE evaluation: a (768 -> N)x2 -> 1 perspective network with squared
//! clipped ReLU, quantised to i16, updated incrementally as moves are made.
//!
//! File format (little-endian): b"APNN", u32 version (1), u32 hidden size N,
//! then i16 feature weights [768][N], i16 feature biases [N],
//! i16 output weights [2N], i32 output bias.

use crate::bitboard::squares;
use crate::position::Position;
use crate::types::*;
use std::sync::Arc;

pub const QA: i32 = 255;
pub const QB: i32 = 64;
pub const SCALE: i32 = 400;
const MAGIC: &[u8; 4] = b"APNN";

pub struct Network {
    pub hidden: usize,
    pub ft_weights: Vec<i16>,
    pub ft_bias: Vec<i16>,
    pub out_weights: Vec<i16>,
    pub out_bias: i32,
}

impl Network {
    pub fn from_bytes(b: &[u8]) -> Result<Network, String> {
        if b.len() < 12 || &b[..4] != MAGIC {
            return Err("not an ArhanPassant network (bad magic)".into());
        }
        let u32_at = |o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]);
        if u32_at(4) != 1 {
            return Err(format!("unsupported network version {}", u32_at(4)));
        }
        let hidden = u32_at(8) as usize;
        if hidden == 0 || hidden > 8192 || hidden % 8 != 0 {
            return Err(format!("bad hidden size {hidden}"));
        }
        let need = 12 + 2 * (768 * hidden + hidden + 2 * hidden) + 4;
        if b.len() != need {
            return Err(format!("network size {} does not match expected {need}", b.len()));
        }
        let mut off = 12;
        let mut read_i16 = |n: usize| -> Vec<i16> {
            let v = b[off..off + 2 * n].chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
            off += 2 * n;
            v
        };
        let ft_weights = read_i16(768 * hidden);
        let ft_bias = read_i16(hidden);
        let out_weights = read_i16(2 * hidden);
        let out_bias = i32::from_le_bytes([b[need - 4], b[need - 3], b[need - 2], b[need - 1]]);
        Ok(Network { hidden, ft_weights, ft_bias, out_weights, out_bias })
    }

    pub fn load(path: &str) -> Result<Network, String> {
        let b = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Network::from_bytes(&b)
    }

    /// The network compiled into the binary, if one was present at build time.
    pub fn embedded() -> Option<Network> {
        #[cfg(has_net)]
        {
            static BYTES: &[u8] = include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/nets/default.nnue"));
            return Network::from_bytes(BYTES).ok();
        }
        #[allow(unreachable_code)]
        None
    }

    /// Full evaluation from scratch (reference implementation, used by tests).
    pub fn evaluate_full(&self, pos: &Position) -> i32 {
        let mut accs = [self.ft_bias.clone(), self.ft_bias.clone()];
        for sq in squares(pos.occupied()) {
            let p = pos.piece_on(sq);
            for (c, acc) in accs.iter_mut().enumerate() {
                let f = feature(Color::from_idx(c), p, sq);
                let w = &self.ft_weights[f * self.hidden..(f + 1) * self.hidden];
                for (a, &x) in acc.iter_mut().zip(w) {
                    *a += x;
                }
            }
        }
        let us = pos.side_to_move().idx();
        self.output(&accs[us], &accs[1 - us])
    }

    #[inline]
    fn output(&self, us: &[i16], them: &[i16]) -> i32 {
        let h = self.hidden;
        let mut sum: i64 = 0;
        for (half, w) in [(us, &self.out_weights[..h]), (them, &self.out_weights[h..])] {
            for (&x, &wi) in half.iter().zip(w.iter()) {
                let c = (x as i32).clamp(0, QA);
                // c * c * w fits in i32 for any i16 weight; the sum may not, so widen.
                sum += (c * c * wi as i32) as i64;
            }
        }
        let out = (sum / QA as i64 + self.out_bias as i64) * SCALE as i64 / (QA as i64 * QB as i64);
        out as i32
    }
}

/// Input feature index for `piece` on `sq` seen from `perspective`.
#[inline(always)]
pub fn feature(perspective: Color, piece: Piece, sq: Square) -> usize {
    let (sq, own) = match perspective {
        Color::White => (sq, piece.color() == Color::White),
        Color::Black => (sq ^ 56, piece.color() == Color::Black),
    };
    (if own { 0 } else { 384 }) + piece.piece_type().idx() * 64 + sq as usize
}

/// Per-ply accumulator stack for incremental updates during search.
pub struct Accumulators {
    net: Arc<Network>,
    /// [ply][colour][hidden]
    data: Vec<i16>,
}

impl Accumulators {
    pub fn new(net: Arc<Network>, plies: usize) -> Accumulators {
        let h = net.hidden;
        Accumulators { net, data: vec![0; plies * 2 * h] }
    }

    #[inline(always)]
    fn slot(&mut self, ply: usize, c: usize) -> &mut [i16] {
        let h = self.net.hidden;
        let o = (ply * 2 + c) * h;
        &mut self.data[o..o + h]
    }

    pub fn refresh(&mut self, pos: &Position, ply: usize) {
        let net = self.net.clone();
        let h = net.hidden;
        for c in 0..2 {
            let acc = self.slot(ply, c);
            acc.copy_from_slice(&net.ft_bias);
            for sq in squares(pos.occupied()) {
                let f = feature(Color::from_idx(c), pos.piece_on(sq), sq);
                for (a, &x) in acc.iter_mut().zip(&net.ft_weights[f * h..(f + 1) * h]) {
                    *a += x;
                }
            }
        }
    }

    /// Same position at the next ply (null move).
    pub fn copy_parent(&mut self, ply: usize) {
        let h = self.net.hidden;
        let (a, b) = self.data.split_at_mut(ply * 2 * h);
        b[..2 * h].copy_from_slice(&a[(ply - 1) * 2 * h..]);
    }

    /// Derive the accumulator at `ply` from `ply - 1` after `m`.
    pub fn push(&mut self, parent: &Position, m: Move, child: &Position, ply: usize) {
        let us = parent.side_to_move();
        let moved = parent.moved_piece(m);
        let placed = child.piece_on(m.to());
        let mut add: [(Piece, Square); 2] = [(placed, m.to()), (Piece::NONE, 0)];
        let mut sub: [(Piece, Square); 2] = [(moved, m.from()), (Piece::NONE, 0)];
        let (mut n_add, mut n_sub) = (1, 1);
        if m.is_capture() {
            let cap_sq = if m.is_ep() { m.to() ^ 8 } else { m.to() };
            sub[1] = (parent.piece_on(cap_sq), cap_sq);
            n_sub = 2;
        } else if m.is_castle() {
            let (rf, rt) = if m.flags() == flag::KING_CASTLE { (m.from() + 3, m.from() + 1) } else { (m.from() - 4, m.from() - 1) };
            let rook = Piece::new(us, PieceType::Rook);
            sub[1] = (rook, rf);
            add[1] = (rook, rt);
            n_sub = 2;
            n_add = 2;
        }
        let net = self.net.clone();
        let h = net.hidden;
        let (prev_all, cur_all) = self.data.split_at_mut(ply * 2 * h);
        let prev_all = &prev_all[(ply - 1) * 2 * h..];
        for c in 0..2 {
            let persp = Color::from_idx(c);
            let prev = &prev_all[c * h..(c + 1) * h];
            let cur = &mut cur_all[c * h..(c + 1) * h];
            cur.copy_from_slice(prev);
            for &(p, s) in &add[..n_add] {
                let f = feature(persp, p, s);
                for (a, &x) in cur.iter_mut().zip(&net.ft_weights[f * h..(f + 1) * h]) {
                    *a += x;
                }
            }
            for &(p, s) in &sub[..n_sub] {
                let f = feature(persp, p, s);
                for (a, &x) in cur.iter_mut().zip(&net.ft_weights[f * h..(f + 1) * h]) {
                    *a -= x;
                }
            }
        }
    }

    pub fn evaluate(&mut self, pos: &Position, ply: usize) -> i32 {
        let h = self.net.hidden;
        let us = pos.side_to_move().idx();
        let o = ply * 2 * h;
        let (a, b) = (&self.data[o..o + h], &self.data[o + h..o + 2 * h]);
        if us == 0 {
            self.net.output(a, b)
        } else {
            self.net.output(b, a)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A random network with small weights.
    pub fn random_net(hidden: usize, seed: u64) -> Network {
        let mut s = seed;
        let mut r = || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            ((s >> 40) % 61) as i16 - 30
        };
        Network {
            hidden,
            ft_weights: (0..768 * hidden).map(|_| r()).collect(),
            ft_bias: (0..hidden).map(|_| r()).collect(),
            out_weights: (0..2 * hidden).map(|_| r()).collect(),
            out_bias: 17,
        }
    }

    #[test]
    fn incremental_matches_full() {
        let net = Arc::new(random_net(64, 0x1234_5678));
        let mut acc = Accumulators::new(net.clone(), 300);
        // A game with castling both ways, en passant, captures and promotion.
        let mut pos = Position::from_fen("r3k2r/pPppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1").unwrap();
        acc.refresh(&pos, 0);
        let mut ply = 0;
        let mut rng = 7u64;
        for _ in 0..200 {
            let moves = pos.legal_moves();
            if moves.is_empty() {
                break;
            }
            rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            let m = moves[(rng >> 33) as usize % moves.len()];
            let child = pos.after(m);
            acc.push(&pos, m, &child, ply + 1);
            ply += 1;
            pos = child;
            assert_eq!(acc.evaluate(&pos, ply), net.evaluate_full(&pos), "after {m} in {}", pos.fen());
            if ply >= 290 {
                break;
            }
        }
    }

    #[test]
    fn mirrored_positions_agree() {
        let net = random_net(32, 99);
        let a = Position::from_fen("r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4").unwrap();
        let b = Position::from_fen("rnbqk2r/pppp1ppp/5n2/2b1p3/4P3/2N2N2/PPPP1PPP/R1BQKB1R b KQkq - 4 4").unwrap();
        assert_eq!(net.evaluate_full(&a), net.evaluate_full(&b));
    }

    #[test]
    fn bytes_roundtrip() {
        let net = random_net(16, 5);
        let mut b = Vec::new();
        b.extend_from_slice(MAGIC);
        b.extend_from_slice(&1u32.to_le_bytes());
        b.extend_from_slice(&16u32.to_le_bytes());
        for v in net.ft_weights.iter().chain(&net.ft_bias).chain(&net.out_weights) {
            b.extend_from_slice(&v.to_le_bytes());
        }
        b.extend_from_slice(&net.out_bias.to_le_bytes());
        let back = Network::from_bytes(&b).unwrap();
        let pos = Position::startpos();
        assert_eq!(back.evaluate_full(&pos), net.evaluate_full(&pos));
        assert!(Network::from_bytes(&b[..b.len() - 1]).is_err());
    }
}
