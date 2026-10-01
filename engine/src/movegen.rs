//! Legal move generation using check and pin masks.

use crate::bitboard::*;
use crate::position::Position;
use crate::types::*;

pub const MAX_MOVES: usize = 256;

/// Which moves to generate.
pub mod kind {
    /// Captures (including en passant and capture-promotions) and queen promotions.
    pub const NOISY: u8 = 1;
    /// Everything else: quiet moves, castling and non-capture under-promotions.
    pub const QUIET: u8 = 2;
    pub const ALL: u8 = 3;
}

#[derive(Clone)]
pub struct MoveList {
    moves: [Move; MAX_MOVES],
    len: usize,
}

impl Default for MoveList {
    fn default() -> Self {
        MoveList::new()
    }
}

impl MoveList {
    #[inline(always)]
    pub fn new() -> MoveList {
        MoveList { moves: [Move::NULL; MAX_MOVES], len: 0 }
    }

    #[inline(always)]
    pub fn push(&mut self, m: Move) {
        debug_assert!(self.len < MAX_MOVES);
        self.moves[self.len] = m;
        self.len += 1;
    }

    #[inline(always)]
    pub fn len(&self) -> usize {
        self.len
    }

    #[inline(always)]
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    #[inline(always)]
    pub fn as_slice(&self) -> &[Move] {
        &self.moves[..self.len]
    }

    pub fn iter(&self) -> impl Iterator<Item = Move> + '_ {
        self.as_slice().iter().copied()
    }

    pub fn contains(&self, m: Move) -> bool {
        self.as_slice().contains(&m)
    }
}

impl std::ops::Index<usize> for MoveList {
    type Output = Move;
    fn index(&self, i: usize) -> &Move {
        &self.as_slice()[i]
    }
}

#[inline(always)]
fn push_promotions(list: &mut MoveList, from: Square, to: Square, capture: bool, kind_mask: u8) {
    let base = if capture { flag::PROMO_CAPTURE_N } else { flag::PROMO_N };
    // Queen promotions are noisy; non-capture under-promotions are quiet; capture
    // under-promotions are noisy (they capture).
    if kind_mask & kind::NOISY != 0 {
        list.push(Move::new(from, to, base + 3));
        if capture {
            for f in 0..3 {
                list.push(Move::new(from, to, base + f));
            }
        }
    }
    if kind_mask & kind::QUIET != 0 && !capture {
        for f in 0..3 {
            list.push(Move::new(from, to, base + f));
        }
    }
}

/// Generate legal moves of the requested kind (see [`kind`]).
pub fn generate(pos: &Position, kind_mask: u8, list: &mut MoveList) {
    let us = pos.side_to_move();
    let them = us.flip();
    let own = pos.color_bb(us);
    let enemy = pos.color_bb(them);
    let occ = own | enemy;
    let ksq = pos.king_sq(us);
    let checkers = pos.checkers();

    let noisy = kind_mask & kind::NOISY != 0;
    let quiet = kind_mask & kind::QUIET != 0;
    let mut dest = 0u64;
    if noisy {
        dest |= enemy;
    }
    if quiet {
        dest |= !occ;
    }

    // King moves.
    let occ_no_king = occ ^ bb(ksq);
    for to in squares(king_attacks(ksq) & dest) {
        if !pos.is_attacked_by(to, them, occ_no_king) {
            let f = if enemy & bb(to) != 0 { flag::CAPTURE } else { flag::QUIET };
            list.push(Move::new(ksq, to, f));
        }
    }
    if more_than_one(checkers) {
        return;
    }

    let check_mask = if checkers != 0 { checkers | between(ksq, lsb(checkers)) } else { !0u64 };
    let pinned = pos.pinned();
    let target = dest & check_mask;

    // Knights (a pinned knight can never move).
    for from in squares(pos.pieces(us, PieceType::Knight) & !pinned) {
        for to in squares(knight_attacks(from) & target) {
            let f = if enemy & bb(to) != 0 { flag::CAPTURE } else { flag::QUIET };
            list.push(Move::new(from, to, f));
        }
    }

    let queens = pos.pieces(us, PieceType::Queen);
    for from in squares(pos.pieces(us, PieceType::Bishop) | queens) {
        let mut att = bishop_attacks(from, occ) & target;
        if pinned & bb(from) != 0 {
            att &= line(ksq, from);
        }
        for to in squares(att) {
            let f = if enemy & bb(to) != 0 { flag::CAPTURE } else { flag::QUIET };
            list.push(Move::new(from, to, f));
        }
    }
    for from in squares(pos.pieces(us, PieceType::Rook) | queens) {
        let mut att = rook_attacks(from, occ) & target;
        if pinned & bb(from) != 0 {
            att &= line(ksq, from);
        }
        for to in squares(att) {
            let f = if enemy & bb(to) != 0 { flag::CAPTURE } else { flag::QUIET };
            list.push(Move::new(from, to, f));
        }
    }

    // Pawns.
    let (up, start_rank, promo_rank): (i8, Bitboard, Bitboard) = match us {
        Color::White => (8, RANK_2, RANK_8),
        Color::Black => (-8, RANK_7, RANK_1),
    };
    for from in squares(pos.pieces(us, PieceType::Pawn)) {
        let pin_line = if pinned & bb(from) != 0 { line(ksq, from) } else { !0u64 };
        let one = (from as i8 + up) as Square;
        // Pushes (promotions count as noisy when to a queen).
        if occ & bb(one) == 0 {
            let allowed = check_mask & pin_line;
            if bb(one) & promo_rank != 0 {
                if allowed & bb(one) != 0 {
                    push_promotions(list, from, one, false, kind_mask);
                }
            } else if quiet {
                if allowed & bb(one) != 0 {
                    list.push(Move::new(from, one, flag::QUIET));
                }
                if bb(from) & start_rank != 0 {
                    let two = (one as i8 + up) as Square;
                    if occ & bb(two) == 0 && allowed & bb(two) != 0 {
                        list.push(Move::new(from, two, flag::DOUBLE_PUSH));
                    }
                }
            }
        }
        // Captures.
        let caps = pawn_attacks(us, from) & enemy & check_mask & pin_line;
        for to in squares(caps) {
            if bb(to) & promo_rank != 0 {
                push_promotions(list, from, to, true, kind_mask);
            } else if noisy {
                list.push(Move::new(from, to, flag::CAPTURE));
            }
        }
    }

    // En passant: rare, so check legality by simulating the capture.
    if noisy {
        if let Some(ep) = pos.ep_square() {
            let captured_sq = ep ^ 8;
            for from in squares(pawn_attacks(them, ep) & pos.pieces(us, PieceType::Pawn)) {
                let occ_after = (occ ^ bb(from) ^ bb(captured_sq)) | bb(ep);
                let their = enemy ^ bb(captured_sq);
                let rq = (pos.type_bb(PieceType::Rook) | pos.type_bb(PieceType::Queen)) & their;
                let bq = (pos.type_bb(PieceType::Bishop) | pos.type_bb(PieceType::Queen)) & their;
                let attacked = rook_attacks(ksq, occ_after) & rq != 0
                    || bishop_attacks(ksq, occ_after) & bq != 0
                    || knight_attacks(ksq) & pos.pieces(them, PieceType::Knight) != 0
                    || pawn_attacks(us, ksq) & pos.pieces(them, PieceType::Pawn) & !bb(captured_sq) != 0;
                if !attacked {
                    list.push(Move::new(from, ep, flag::EP_CAPTURE));
                }
            }
        }
    }

    // Castling.
    if quiet && checkers == 0 {
        let rights = pos.castling_rights();
        let (k_right, q_right, base) = match us {
            Color::White => (castle::WK, castle::WQ, 0u8),
            Color::Black => (castle::BK, castle::BQ, 56u8),
        };
        if rights & k_right != 0
            && occ & (bb(base + 5) | bb(base + 6)) == 0
            && !pos.is_attacked_by(base + 5, them, occ)
            && !pos.is_attacked_by(base + 6, them, occ)
        {
            list.push(Move::new(base + 4, base + 6, flag::KING_CASTLE));
        }
        if rights & q_right != 0
            && occ & (bb(base + 1) | bb(base + 2) | bb(base + 3)) == 0
            && !pos.is_attacked_by(base + 3, them, occ)
            && !pos.is_attacked_by(base + 2, them, occ)
        {
            list.push(Move::new(base + 4, base + 2, flag::QUEEN_CASTLE));
        }
    }
}

impl Position {
    /// All legal moves.
    pub fn legal_moves(&self) -> MoveList {
        let mut list = MoveList::new();
        generate(self, kind::ALL, &mut list);
        list
    }

    /// Is this specific move legal here? (Used to validate hash/killer moves.)
    pub fn is_legal(&self, m: Move) -> bool {
        if m.is_null() {
            return false;
        }
        let p = self.piece_on(m.from());
        if p.is_none() || p.color() != self.side_to_move() {
            return false;
        }
        let mut list = MoveList::new();
        generate(self, if m.is_noisy() { kind::NOISY } else { kind::QUIET }, &mut list);
        list.contains(m)
    }

    pub fn is_checkmate(&self) -> bool {
        self.in_check() && self.legal_moves().is_empty()
    }

    pub fn is_stalemate(&self) -> bool {
        !self.in_check() && self.legal_moves().is_empty()
    }

    /// Parse a move in UCI notation (`e2e4`, `e7e8q`; castling as `e1g1` or `e1h1`).
    pub fn parse_uci_move(&self, s: &str) -> Option<Move> {
        let s = s.trim();
        let legal = self.legal_moves();
        if let Some(m) = legal.iter().find(|m| m.to_uci() == s) {
            return Some(m);
        }
        // King-takes-rook castling notation.
        let castle = legal.iter().find(|m| {
            m.is_castle() && {
                let rook_from = if m.flags() == flag::KING_CASTLE { m.from() + 3 } else { m.from() - 4 };
                format!("{}{}", square_name(m.from()), square_name(rook_from)) == s
            }
        });
        castle
    }
}

/// Count leaf nodes to `depth` (move-generator correctness test).
pub fn perft(pos: &Position, depth: u32) -> u64 {
    if depth == 0 {
        return 1;
    }
    let mut list = MoveList::new();
    generate(pos, kind::ALL, &mut list);
    if depth == 1 {
        return list.len() as u64;
    }
    list.iter().map(|m| perft(&pos.after(m), depth - 1)).sum()
}

/// Per-move perft breakdown, as printed by `go perft` in most engines.
pub fn perft_divide(pos: &Position, depth: u32) -> Vec<(Move, u64)> {
    let list = pos.legal_moves();
    list.iter()
        .map(|m| (m, if depth <= 1 { 1 } else { perft(&pos.after(m), depth - 1) }))
        .collect()
}
