//! Legal move generation using check and pin masks.

use crate::bitboard::*;
use crate::position::{castle_right, castle_targets, Position};
use crate::types::*;
use std::mem::MaybeUninit;

pub const MAX_MOVES: usize = 256;

/// Which moves to generate.
pub mod kind {
    /// Captures (including en passant and capture-promotions) and queen promotions.
    pub const NOISY: u8 = 1;
    /// Everything else: quiet moves, castling and non-capture under-promotions.
    pub const QUIET: u8 = 2;
    pub const ALL: u8 = 3;
}

/// A list of moves. The storage is left uninitialised: search builds one at
/// nearly every node, and only the first `len` entries are ever read.
#[derive(Clone)]
pub struct MoveList {
    moves: [MaybeUninit<Move>; MAX_MOVES],
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
        MoveList { moves: [const { MaybeUninit::uninit() }; MAX_MOVES], len: 0 }
    }

    #[inline(always)]
    pub fn push(&mut self, m: Move) {
        debug_assert!(self.len < MAX_MOVES);
        self.moves[self.len] = MaybeUninit::new(m);
        self.len += 1;
    }

    #[inline(always)]
    pub fn clear(&mut self) {
        self.len = 0;
    }

    /// Overwrite entry `i` (< len).
    #[inline(always)]
    pub fn set(&mut self, i: usize, m: Move) {
        assert!(i < self.len);
        self.moves[i] = MaybeUninit::new(m);
    }

    /// Keep only the first `len` entries.
    #[inline(always)]
    pub fn truncate(&mut self, len: usize) {
        self.len = self.len.min(len);
    }

    #[inline(always)]
    pub fn swap(&mut self, a: usize, b: usize) {
        assert!(a < self.len && b < self.len);
        self.moves.swap(a, b);
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
        // SAFETY: entries below `len` were written by `push` or `set`, and
        // MaybeUninit<Move> has the layout of Move.
        unsafe { std::slice::from_raw_parts(self.moves.as_ptr().cast::<Move>(), self.len) }
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

    // Castling (standard chess and Chess960).
    if quiet && checkers == 0 && pos.castling_rights() != 0 {
        for king_side in [true, false] {
            if castle_ok(pos, us, them, occ, king_side) {
                let (kto, _) = castle_targets(us, king_side);
                let fl = if king_side { flag::KING_CASTLE } else { flag::QUEEN_CASTLE };
                list.push(Move::new(pos.king_sq(us), kto, fl));
            }
        }
    }
}

/// Squares from `a` to `b` on one rank, both included.
#[inline(always)]
fn rank_span(a: Square, b: Square) -> Bitboard {
    let (lo, hi) = (a.min(b), a.max(b));
    (u64::MAX >> (63 - hi)) & (u64::MAX << lo)
}

/// Can `us` castle on this side now (not in check is the caller's business)? The
/// squares the king and rook cross must be empty apart from those two pieces, the
/// squares the king crosses and lands on must not be attacked, and the rook must
/// not be shielding the king (possible in Chess960).
#[inline]
fn castle_ok(pos: &Position, us: Color, them: Color, occ: Bitboard, king_side: bool) -> bool {
    let right = castle_right(us, king_side);
    if pos.castling_rights() & right == 0 {
        return false;
    }
    let ksq = pos.king_sq(us);
    let rsq = pos.castle_rook(right);
    let (kto, rto) = castle_targets(us, king_side);
    let movers = bb(ksq) | bb(rsq);
    if occ & (rank_span(ksq, kto) | rank_span(rsq, rto)) & !movers != 0 {
        return false;
    }
    let mut path = rank_span(ksq, kto) & !bb(ksq);
    while path != 0 {
        let sq = lsb(path);
        path &= path - 1;
        if pos.is_attacked_by(sq, them, occ) {
            return false;
        }
    }
    pos.pinned() & bb(rsq) == 0
}

impl Position {
    /// All legal moves.
    pub fn legal_moves(&self) -> MoveList {
        let mut list = MoveList::new();
        generate(self, kind::ALL, &mut list);
        list
    }

    /// Is this specific move legal here? (Used to validate hash/killer moves.)
    /// Decides exactly what [`generate`] would produce, without generating:
    /// every flag, target and pin/check rule of the generator is mirrored.
    pub fn is_legal(&self, m: Move) -> bool {
        if m.is_null() {
            return false;
        }
        let us = self.side_to_move();
        let them = us.flip();
        let (from, to, fl) = (m.from(), m.to(), m.flags());
        let p = self.piece_on(from);
        if p.is_none() || p.color() != us {
            return false;
        }
        let own = self.color_bb(us);
        let enemy = self.color_bb(them);
        let occ = own | enemy;
        let to_bb = bb(to);
        if own & to_bb != 0 || fl == 6 || fl == 7 {
            return false;
        }
        // Capture flags need an enemy piece on the target (en passant: the ep square); others an empty one.
        if fl == flag::EP_CAPTURE {
            if self.ep_square() != Some(to) {
                return false;
            }
        } else if m.is_capture() != (enemy & to_bb != 0) {
            return false;
        }
        let pt = p.piece_type();
        let ksq = self.king_sq(us);
        let checkers = self.checkers();
        if pt == PieceType::King {
            if m.is_castle() {
                let king_side = fl == flag::KING_CASTLE;
                return checkers == 0 && from == ksq && to == castle_targets(us, king_side).0 && castle_ok(self, us, them, occ, king_side);
            }
            if fl != flag::QUIET && fl != flag::CAPTURE {
                return false;
            }
            return king_attacks(from) & to_bb != 0 && !self.is_attacked_by(to, them, occ ^ bb(ksq));
        }
        if m.is_castle() || more_than_one(checkers) {
            return false;
        }
        if pt != PieceType::Pawn {
            if fl != flag::QUIET && fl != flag::CAPTURE {
                return false;
            }
            let check_mask = if checkers != 0 { checkers | between(ksq, lsb(checkers)) } else { !0u64 };
            let pinned = self.pinned() & bb(from) != 0;
            let att = match pt {
                PieceType::Knight => {
                    if pinned {
                        return false;
                    }
                    knight_attacks(from)
                }
                PieceType::Bishop => bishop_attacks(from, occ),
                PieceType::Rook => rook_attacks(from, occ),
                _ => bishop_attacks(from, occ) | rook_attacks(from, occ),
            };
            let pin_line = if pinned { line(ksq, from) } else { !0u64 };
            return att & check_mask & pin_line & to_bb != 0;
        }
        // Pawns.
        let (up, start_rank, promo_rank): (i8, Bitboard, Bitboard) = match us {
            Color::White => (8, RANK_2, RANK_8),
            Color::Black => (-8, RANK_7, RANK_1),
        };
        if (to_bb & promo_rank != 0) != m.is_promotion() {
            return false;
        }
        if fl == flag::EP_CAPTURE {
            // As the generator: no check mask, simulate the capture instead.
            if pawn_attacks(them, to) & bb(from) == 0 {
                return false;
            }
            let captured_sq = to ^ 8;
            let occ_after = (occ ^ bb(from) ^ bb(captured_sq)) | to_bb;
            let their = enemy ^ bb(captured_sq);
            let rq = (self.type_bb(PieceType::Rook) | self.type_bb(PieceType::Queen)) & their;
            let bq = (self.type_bb(PieceType::Bishop) | self.type_bb(PieceType::Queen)) & their;
            let attacked = rook_attacks(ksq, occ_after) & rq != 0
                || bishop_attacks(ksq, occ_after) & bq != 0
                || knight_attacks(ksq) & self.pieces(them, PieceType::Knight) != 0
                || pawn_attacks(us, ksq) & self.pieces(them, PieceType::Pawn) & !bb(captured_sq) != 0;
            return !attacked;
        }
        let check_mask = if checkers != 0 { checkers | between(ksq, lsb(checkers)) } else { !0u64 };
        let pin_line = if self.pinned() & bb(from) != 0 { line(ksq, from) } else { !0u64 };
        let allowed = check_mask & pin_line;
        if m.is_capture() {
            return pawn_attacks(us, from) & to_bb & allowed != 0;
        }
        let one = (from as i8 + up) as Square;
        if occ & bb(one) != 0 {
            return false;
        }
        if fl == flag::DOUBLE_PUSH {
            let two = (one as i8 + up) as Square;
            return bb(from) & start_rank != 0 && to == two && occ & to_bb == 0 && allowed & to_bb != 0;
        }
        // Single push (plain, or a promotion on the last rank).
        to == one && allowed & to_bb != 0
    }

    /// Reference legality check: generate the move's kind and look it up.
    pub fn is_legal_by_generation(&self, m: Move) -> bool {
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
        self.parse_uci_move_mode(s, false)
    }

    /// Parse a move in UCI notation. In Chess960 mode castling is only king-takes-rook
    /// (`f1h1`), since `f1g1` may be a plain king move; otherwise both forms are accepted.
    pub fn parse_uci_move_mode(&self, s: &str, chess960: bool) -> Option<Move> {
        let s = s.trim();
        let legal = self.legal_moves();
        if let Some(m) = legal.iter().find(|m| !(chess960 && m.is_castle()) && m.to_uci() == s) {
            return Some(m);
        }
        // King-takes-rook castling notation.
        let castle = legal.iter().find(|m| m.is_castle() && self.uci_move(*m, true) == s);
        castle
    }

    /// UCI text of a move. In Chess960 mode castling is written king-takes-rook.
    pub fn uci_move(&self, m: Move, chess960: bool) -> String {
        if chess960 && m.is_castle() {
            let us = if m.from() < 8 { Color::White } else { Color::Black };
            let rook = self.castle_rook(castle_right(us, m.flags() == flag::KING_CASTLE));
            format!("{}{}", square_name(m.from()), square_name(rook))
        } else {
            m.to_uci()
        }
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
