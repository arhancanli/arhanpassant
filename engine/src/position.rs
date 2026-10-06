//! Board state, FEN, making moves and attack queries.

use crate::bitboard::*;
use crate::types::*;
use std::fmt;

pub const START_FEN: &str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/// Castling rights that survive a move touching a square.
const CASTLE_KEEP: [u8; 64] = {
    let mut m = [0xFu8; 64];
    m[0] = !castle::WQ & 0xF; // a1
    m[7] = !castle::WK & 0xF; // h1
    m[4] = !(castle::WK | castle::WQ) & 0xF; // e1
    m[56] = !castle::BQ & 0xF; // a8
    m[63] = !castle::BK & 0xF; // h8
    m[60] = !(castle::BK | castle::BQ) & 0xF; // e8
    m
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Position {
    pieces: [Bitboard; 6],
    colors: [Bitboard; 2],
    board: [Piece; 64],
    stm: Color,
    castling: u8,
    ep: Square,
    halfmove: u16,
    fullmove: u16,
    hash: u64,
    checkers: Bitboard,
    pinned: Bitboard,
}

/// Squares attacked by the opponent, by attacker class (see [`Position::threats`]).
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct Threats {
    pub pawn: Bitboard,
    pub minor: Bitboard,
    pub rook: Bitboard,
    pub all: Bitboard,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FenError(pub String);

impl fmt::Display for FenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid FEN: {}", self.0)
    }
}

impl std::error::Error for FenError {}

impl Default for Position {
    fn default() -> Self {
        Position::startpos()
    }
}

impl Position {
    fn empty() -> Position {
        Position {
            pieces: [0; 6],
            colors: [0; 2],
            board: [Piece::NONE; 64],
            stm: Color::White,
            castling: 0,
            ep: NO_SQUARE,
            halfmove: 0,
            fullmove: 1,
            hash: 0,
            checkers: 0,
            pinned: 0,
        }
    }

    pub fn startpos() -> Position {
        Position::from_fen(START_FEN).expect("start FEN is valid")
    }

    // ----- accessors -------------------------------------------------------

    #[inline(always)]
    pub fn side_to_move(&self) -> Color {
        self.stm
    }

    #[inline(always)]
    pub fn hash(&self) -> u64 {
        self.hash
    }

    #[inline(always)]
    pub fn checkers(&self) -> Bitboard {
        self.checkers
    }

    #[inline(always)]
    pub fn in_check(&self) -> bool {
        self.checkers != 0
    }

    #[inline(always)]
    pub fn pinned(&self) -> Bitboard {
        self.pinned
    }

    #[inline(always)]
    pub fn castling_rights(&self) -> u8 {
        self.castling
    }

    #[inline(always)]
    pub fn ep_square(&self) -> Option<Square> {
        (self.ep != NO_SQUARE).then_some(self.ep)
    }

    #[inline(always)]
    pub fn halfmove_clock(&self) -> u16 {
        self.halfmove
    }

    #[inline(always)]
    pub fn fullmove_number(&self) -> u16 {
        self.fullmove
    }

    #[inline(always)]
    pub fn piece_on(&self, sq: Square) -> Piece {
        self.board[sq as usize]
    }

    #[inline(always)]
    pub fn occupied(&self) -> Bitboard {
        self.colors[0] | self.colors[1]
    }

    #[inline(always)]
    pub fn color_bb(&self, c: Color) -> Bitboard {
        self.colors[c.idx()]
    }

    #[inline(always)]
    pub fn type_bb(&self, pt: PieceType) -> Bitboard {
        self.pieces[pt.idx()]
    }

    #[inline(always)]
    pub fn pieces(&self, c: Color, pt: PieceType) -> Bitboard {
        self.pieces[pt.idx()] & self.colors[c.idx()]
    }

    #[inline(always)]
    pub fn king_sq(&self, c: Color) -> Square {
        lsb(self.pieces(c, PieceType::King))
    }

    /// Knights, bishops, rooks and queens of one side.
    #[inline(always)]
    pub fn non_pawn_material(&self, c: Color) -> Bitboard {
        self.colors[c.idx()] & !(self.pieces[0] | self.pieces[5])
    }

    // ----- piece placement -------------------------------------------------

    #[inline(always)]
    fn put(&mut self, p: Piece, sq: Square) {
        let b = bb(sq);
        self.pieces[p.piece_type().idx()] |= b;
        self.colors[p.color().idx()] |= b;
        self.board[sq as usize] = p;
        self.hash ^= ZOBRIST_PIECES[p.idx()][sq as usize];
    }

    #[inline(always)]
    fn remove(&mut self, sq: Square) -> Piece {
        let p = self.board[sq as usize];
        debug_assert!(!p.is_none());
        let b = bb(sq);
        self.pieces[p.piece_type().idx()] ^= b;
        self.colors[p.color().idx()] ^= b;
        self.board[sq as usize] = Piece::NONE;
        self.hash ^= ZOBRIST_PIECES[p.idx()][sq as usize];
        p
    }

    // ----- attacks ---------------------------------------------------------

    /// Pieces of both colours attacking `sq` given occupancy `occ`.
    #[inline]
    pub fn attackers_to(&self, sq: Square, occ: Bitboard) -> Bitboard {
        let p = &self.pieces;
        (pawn_attacks(Color::White, sq) & p[0] & self.colors[1])
            | (pawn_attacks(Color::Black, sq) & p[0] & self.colors[0])
            | (knight_attacks(sq) & p[1])
            | (king_attacks(sq) & p[5])
            | (rook_attacks(sq, occ) & (p[3] | p[4]))
            | (bishop_attacks(sq, occ) & (p[2] | p[4]))
    }

    /// Is `sq` attacked by side `by`, with occupancy `occ`?
    #[inline]
    pub fn is_attacked_by(&self, sq: Square, by: Color, occ: Bitboard) -> bool {
        let them = self.colors[by.idx()];
        let p = &self.pieces;
        (pawn_attacks(by.flip(), sq) & p[0] & them) != 0
            || (knight_attacks(sq) & p[1] & them) != 0
            || (king_attacks(sq) & p[5] & them) != 0
            || (rook_attacks(sq, occ) & (p[3] | p[4]) & them) != 0
            || (bishop_attacks(sq, occ) & (p[2] | p[4]) & them) != 0
    }

    /// Squares the side not to move attacks, by class of attacker, each set
    /// including the cheaper classes: pawns; pawns and minor pieces; pawns,
    /// minors and rooks; and every piece.
    pub fn threats(&self) -> Threats {
        let them = self.stm.flip();
        let occ = self.occupied();
        let pawn = pawn_attacks_bb(them, self.pieces(them, PieceType::Pawn));
        let mut minor = pawn;
        for sq in squares(self.pieces(them, PieceType::Knight)) {
            minor |= knight_attacks(sq);
        }
        for sq in squares(self.pieces(them, PieceType::Bishop)) {
            minor |= bishop_attacks(sq, occ);
        }
        let mut rook = minor;
        for sq in squares(self.pieces(them, PieceType::Rook)) {
            rook |= rook_attacks(sq, occ);
        }
        let mut all = rook | king_attacks(self.king_sq(them));
        for sq in squares(self.pieces(them, PieceType::Queen)) {
            all |= queen_attacks(sq, occ);
        }
        Threats { pawn, minor, rook, all }
    }

    /// Squares from which a piece of each type would check the opponent's king directly.
    pub fn check_squares(&self) -> [Bitboard; 6] {
        let them = self.stm.flip();
        let ksq = self.king_sq(them);
        let occ = self.occupied();
        let (b, r) = (bishop_attacks(ksq, occ), rook_attacks(ksq, occ));
        [pawn_attacks(them, ksq), knight_attacks(ksq), b, r, b | r, 0]
    }

    fn update_check_info(&mut self) {
        let us = self.stm;
        let them = us.flip();
        let ksq = self.king_sq(us);
        let occ = self.occupied();
        self.checkers = self.attackers_to(ksq, occ) & self.colors[them.idx()];
        let snipers = ((rook_attacks(ksq, 0) & (self.pieces[3] | self.pieces[4]))
            | (bishop_attacks(ksq, 0) & (self.pieces[2] | self.pieces[4])))
            & self.colors[them.idx()];
        let mut pinned = 0;
        for s in squares(snipers) {
            let blockers = between(ksq, s) & occ;
            if blockers != 0 && !more_than_one(blockers) && blockers & self.colors[us.idx()] != 0 {
                pinned |= blockers;
            }
        }
        self.pinned = pinned;
    }

    // ----- making moves ----------------------------------------------------

    /// Play a legal move in place. Behaviour is undefined (debug-asserted) for illegal moves.
    pub fn play(&mut self, m: Move) {
        let us = self.stm;
        let them = us.flip();
        let from = m.from();
        let to = m.to();
        let flags = m.flags();

        self.hash ^= ZOBRIST_CASTLING[self.castling as usize];
        if self.ep != NO_SQUARE {
            self.hash ^= ZOBRIST_EP[file_of(self.ep) as usize];
            self.ep = NO_SQUARE;
        }
        self.halfmove += 1;

        if m.is_capture() {
            let cap_sq = if flags == flag::EP_CAPTURE { to ^ 8 } else { to };
            self.remove(cap_sq);
            self.halfmove = 0;
        }

        let piece = self.remove(from);
        debug_assert_eq!(piece.color(), us);
        let placed = match m.promotion() {
            Some(pt) => Piece::new(us, pt),
            None => piece,
        };
        self.put(placed, to);

        match piece.piece_type() {
            PieceType::Pawn => {
                self.halfmove = 0;
                if flags == flag::DOUBLE_PUSH {
                    let ep_sq = (from + to) / 2;
                    if pawn_attacks(us, ep_sq) & self.pieces(them, PieceType::Pawn) != 0 {
                        self.ep = ep_sq;
                        self.hash ^= ZOBRIST_EP[file_of(ep_sq) as usize];
                    }
                }
            }
            PieceType::King => {
                if flags == flag::KING_CASTLE {
                    let rook = self.remove(from + 3);
                    self.put(rook, from + 1);
                } else if flags == flag::QUEEN_CASTLE {
                    let rook = self.remove(from - 4);
                    self.put(rook, from - 1);
                }
            }
            _ => {}
        }

        self.castling &= CASTLE_KEEP[from as usize] & CASTLE_KEEP[to as usize];
        self.hash ^= ZOBRIST_CASTLING[self.castling as usize];
        if us == Color::Black {
            self.fullmove += 1;
        }
        self.stm = them;
        self.hash ^= ZOBRIST_SIDE;
        self.update_check_info();
        debug_assert_eq!(self.hash, self.compute_hash());
    }

    /// Return the position after `m` (copy-make).
    #[inline]
    pub fn after(&self, m: Move) -> Position {
        let mut p = *self;
        p.play(m);
        p
    }

    /// Pass the turn (for null-move pruning). Must not be called in check.
    pub fn play_null(&mut self) {
        debug_assert!(!self.in_check());
        if self.ep != NO_SQUARE {
            self.hash ^= ZOBRIST_EP[file_of(self.ep) as usize];
            self.ep = NO_SQUARE;
        }
        self.halfmove += 1;
        self.stm = self.stm.flip();
        self.hash ^= ZOBRIST_SIDE;
        self.update_check_info();
    }

    /// Hash of the position after `m`, without making it (for TT prefetch / speculation).
    pub fn hash_after(&self, m: Move) -> u64 {
        self.after(m).hash
    }

    // ----- rules -----------------------------------------------------------

    /// Dead position by material: no sequence of legal moves can mate
    /// (K v K, K+minor v K, or only same-coloured bishops besides kings).
    pub fn is_insufficient_material(&self) -> bool {
        let p = &self.pieces;
        if p[0] | p[3] | p[4] != 0 {
            return false;
        }
        let minors = p[1] | p[2];
        if minors.count_ones() <= 1 {
            return true;
        }
        if p[1] == 0 {
            let bishops = p[2];
            return bishops & LIGHT_SQUARES == 0 || bishops & DARK_SQUARES == 0;
        }
        false
    }

    pub fn compute_hash(&self) -> u64 {
        let mut h = 0u64;
        for sq in 0..64u8 {
            let p = self.board[sq as usize];
            if !p.is_none() {
                h ^= ZOBRIST_PIECES[p.idx()][sq as usize];
            }
        }
        h ^= ZOBRIST_CASTLING[self.castling as usize];
        if self.ep != NO_SQUARE {
            h ^= ZOBRIST_EP[file_of(self.ep) as usize];
        }
        if self.stm == Color::Black {
            h ^= ZOBRIST_SIDE;
        }
        h
    }

    // ----- FEN -------------------------------------------------------------

    pub fn from_fen(fen: &str) -> Result<Position, FenError> {
        let err = |s: &str| Err(FenError(s.to_string()));
        let mut parts = fen.split_whitespace();
        let placement = match parts.next() {
            Some(p) => p,
            None => return err("empty string"),
        };
        let mut pos = Position::empty();
        let ranks: Vec<&str> = placement.split('/').collect();
        if ranks.len() != 8 {
            return err("placement must have 8 ranks");
        }
        for (i, rank_str) in ranks.iter().enumerate() {
            let rank = 7 - i as u8;
            let mut file = 0u8;
            for c in rank_str.chars() {
                if let Some(d) = c.to_digit(10) {
                    if !(1..=8).contains(&d) {
                        return err("bad empty-square count");
                    }
                    file += d as u8;
                } else {
                    let pt = match PieceType::from_char(c) {
                        Some(pt) => pt,
                        None => return err("unknown piece letter"),
                    };
                    if file >= 8 {
                        return err("rank too long");
                    }
                    let color = if c.is_ascii_uppercase() { Color::White } else { Color::Black };
                    pos.put(Piece::new(color, pt), make_square(file, rank));
                    file += 1;
                }
                if file > 8 {
                    return err("rank too long");
                }
            }
            if file != 8 {
                return err("rank does not cover 8 files");
            }
        }

        pos.stm = match parts.next().unwrap_or("w") {
            "w" => Color::White,
            "b" => Color::Black,
            _ => return err("side to move must be w or b"),
        };

        let castling = parts.next().unwrap_or("-");
        if castling != "-" {
            for c in castling.chars() {
                pos.castling |= match c {
                    'K' => castle::WK,
                    'Q' => castle::WQ,
                    'k' => castle::BK,
                    'q' => castle::BQ,
                    _ => return err("bad castling field"),
                };
            }
        }

        let ep = parts.next().unwrap_or("-");
        if ep != "-" {
            match parse_square(ep) {
                Some(sq) => pos.ep = sq,
                None => return err("bad en-passant square"),
            }
        }

        pos.halfmove = match parts.next() {
            Some(s) => s.parse().map_err(|_| FenError("bad halfmove clock".into()))?,
            None => 0,
        };
        pos.fullmove = match parts.next() {
            Some(s) => s.parse::<u16>().map_err(|_| FenError("bad fullmove number".into()))?.max(1),
            None => 1,
        };

        // Validation.
        for c in [Color::White, Color::Black] {
            if pos.pieces(c, PieceType::King).count_ones() != 1 {
                return err("each side needs exactly one king");
            }
        }
        if pos.pieces[0] & (RANK_1 | RANK_8) != 0 {
            return err("pawns on the first or last rank");
        }
        // Drop castling rights that the placement cannot support.
        let has = |c: Color, pt: PieceType, sq: Square| pos.board[sq as usize] == Piece::new(c, pt);
        let mut rights = pos.castling;
        if !has(Color::White, PieceType::King, 4) {
            rights &= !(castle::WK | castle::WQ);
        }
        if !has(Color::White, PieceType::Rook, 7) {
            rights &= !castle::WK;
        }
        if !has(Color::White, PieceType::Rook, 0) {
            rights &= !castle::WQ;
        }
        if !has(Color::Black, PieceType::King, 60) {
            rights &= !(castle::BK | castle::BQ);
        }
        if !has(Color::Black, PieceType::Rook, 63) {
            rights &= !castle::BK;
        }
        if !has(Color::Black, PieceType::Rook, 56) {
            rights &= !castle::BQ;
        }
        pos.castling = rights;
        // Keep an en-passant square only when a capture onto it is possible.
        if pos.ep != NO_SQUARE {
            let us = pos.stm;
            let them = us.flip();
            let ok_rank = if us == Color::White { 5 } else { 2 };
            let pushed = if us == Color::White { pos.ep - 8 } else { pos.ep + 8 };
            let valid = rank_of(pos.ep) == ok_rank
                && pos.board[pushed as usize] == Piece::new(them, PieceType::Pawn)
                && pos.board[pos.ep as usize].is_none()
                && pawn_attacks(them, pos.ep) & pos.pieces(us, PieceType::Pawn) != 0;
            if !valid {
                pos.ep = NO_SQUARE;
            }
        }
        let them = pos.stm.flip();
        if pos.is_attacked_by(pos.king_sq(them), pos.stm, pos.occupied()) {
            return err("the side not to move is in check");
        }
        pos.hash = pos.compute_hash();
        pos.update_check_info();
        Ok(pos)
    }

    pub fn fen(&self) -> String {
        let mut s = String::with_capacity(90);
        for rank in (0..8).rev() {
            let mut empty = 0;
            for file in 0..8 {
                let p = self.board[make_square(file, rank) as usize];
                if p.is_none() {
                    empty += 1;
                } else {
                    if empty > 0 {
                        s.push((b'0' + empty) as char);
                        empty = 0;
                    }
                    s.push(p.to_char());
                }
            }
            if empty > 0 {
                s.push((b'0' + empty) as char);
            }
            if rank > 0 {
                s.push('/');
            }
        }
        s.push(' ');
        s.push(if self.stm == Color::White { 'w' } else { 'b' });
        s.push(' ');
        if self.castling == 0 {
            s.push('-');
        } else {
            for (bit, c) in [(castle::WK, 'K'), (castle::WQ, 'Q'), (castle::BK, 'k'), (castle::BQ, 'q')] {
                if self.castling & bit != 0 {
                    s.push(c);
                }
            }
        }
        s.push(' ');
        if self.ep == NO_SQUARE {
            s.push('-');
        } else {
            s.push_str(&square_name(self.ep));
        }
        s.push_str(&format!(" {} {}", self.halfmove, self.fullmove));
        s
    }

    /// The piece type that `m` captures, if any (pawn for en passant).
    #[inline]
    pub fn captured(&self, m: Move) -> Option<PieceType> {
        if m.is_ep() {
            Some(PieceType::Pawn)
        } else if m.is_capture() {
            Some(self.board[m.to() as usize].piece_type())
        } else {
            None
        }
    }

    /// The moving piece of `m`.
    #[inline(always)]
    pub fn moved_piece(&self, m: Move) -> Piece {
        self.board[m.from() as usize]
    }

    /// Does `m` give check? (Exact, via making the move.)
    pub fn gives_check(&self, m: Move) -> bool {
        self.after(m).in_check()
    }
}

impl fmt::Debug for Position {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f)?;
        for rank in (0..8).rev() {
            write!(f, " {} ", rank + 1)?;
            for file in 0..8 {
                let p = self.board[make_square(file, rank) as usize];
                write!(f, " {}", if p.is_none() { '.' } else { p.to_char() })?;
            }
            writeln!(f)?;
        }
        writeln!(f, "    a b c d e f g h")?;
        writeln!(f, " fen: {}", self.fen())?;
        write!(f, " key: {:016x}", self.hash)
    }
}

impl fmt::Display for Position {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.fen())
    }
}
