//! Staged move ordering: hash move, good captures, quiets (history-ordered,
//! killers and counter-move first), then losing captures.

use crate::bitboard::bb;
use crate::history::{History, QuietCtx};
use crate::movegen::{generate, kind, MoveList, MAX_MOVES};
use crate::params as p;
use crate::position::{Position, Threats};
use crate::see::{see_ge, see_value};
use crate::types::*;
use std::mem::MaybeUninit;

/// Board facts for ordering quiet moves beyond their history: which pieces
/// are attacked by cheaper ones, and which squares give check.
#[derive(Copy, Clone, Debug)]
pub struct OrderInfo {
    pub threats: Threats,
    pub check_sq: [Bitboard; 6],
}

/// Ordering value of rescuing (or, x0.95 against, endangering) a piece of each
/// type from an attack by a cheaper piece, before the `threat_order` scale.
const THREAT_VALUE: [i32; 6] = [0, 14000, 14000, 24000, 48000, 0];

#[derive(Copy, Clone, PartialEq, Eq, PartialOrd, Ord, Debug)]
enum Stage {
    TtMove,
    GenNoisy,
    GoodNoisy,
    GenQuiet,
    Quiet,
    BadNoisy,
    Done,
}

pub struct MovePicker {
    stage: Stage,
    tt_move: Move,
    killers: [Move; 2],
    counter: Move,
    ctx: QuietCtx,
    /// Threat and check information for quiet ordering (None when those settings are off).
    pub order: Option<OrderInfo>,
    /// Moves of the current stage; scores[i] belongs to moves[i] (uninitialised past len).
    moves: MoveList,
    scores: [MaybeUninit<i32>; MAX_MOVES],
    len: usize,
    idx: usize,
    bad: MoveList,
    bad_idx: usize,
    skip_quiets: bool,
    /// Quiescence: only noisy moves (unless in check), no good/bad split.
    qsearch: bool,
}

impl MovePicker {
    pub fn new(pos: &Position, tt_move: Move, killers: [Move; 2], counter: Move, ctx: QuietCtx) -> MovePicker {
        let tt_ok = !tt_move.is_null() && pos.is_legal(tt_move);
        MovePicker {
            stage: if tt_ok { Stage::TtMove } else { Stage::GenNoisy },
            tt_move: if tt_ok { tt_move } else { Move::NULL },
            killers,
            counter,
            ctx,
            order: None,
            moves: MoveList::new(),
            scores: [const { MaybeUninit::uninit() }; MAX_MOVES],
            len: 0,
            idx: 0,
            bad: MoveList::new(),
            bad_idx: 0,
            skip_quiets: false,
            qsearch: false,
        }
    }

    pub fn qsearch(pos: &Position, tt_move: Move, ctx: QuietCtx) -> MovePicker {
        let in_check = pos.in_check();
        let usable = !tt_move.is_null() && (in_check || tt_move.is_noisy());
        let mut mp = MovePicker::new(pos, if usable { tt_move } else { Move::NULL }, [Move::NULL; 2], Move::NULL, ctx);
        mp.qsearch = true;
        mp.skip_quiets = !in_check;
        mp
    }

    /// Stop handing out quiet moves (late-move / futility pruning decided they are hopeless).
    pub fn skip_quiets(&mut self) {
        self.skip_quiets = true;
    }

    #[inline(always)]
    fn score(&self, i: usize) -> i32 {
        debug_assert!(i < self.len);
        // SAFETY: every index below len was scored when the stage was generated.
        unsafe { self.scores[i].assume_init() }
    }

    fn pick_best(&mut self) -> Option<(Move, i32)> {
        if self.idx >= self.len {
            return None;
        }
        let mut best = self.idx;
        let mut best_score = self.score(best);
        for i in self.idx + 1..self.len {
            let sc = self.score(i);
            if sc > best_score {
                best = i;
                best_score = sc;
            }
        }
        self.moves.swap(self.idx, best);
        self.scores.swap(self.idx, best);
        let r = (self.moves[self.idx], best_score);
        self.idx += 1;
        Some(r)
    }

    /// Generate one stage's moves into `moves`, dropping the table move, and
    /// score them in generation order.
    #[inline(always)]
    fn fill(&mut self, pos: &Position, kind_mask: u8, mut score: impl FnMut(Move) -> i32) {
        self.moves.clear();
        generate(pos, kind_mask, &mut self.moves);
        let n = self.moves.len();
        let mut j = 0;
        for i in 0..n {
            let m = self.moves[i];
            if m == self.tt_move {
                continue;
            }
            self.moves.set(j, m);
            self.scores[j] = MaybeUninit::new(score(m));
            j += 1;
        }
        self.moves.truncate(j);
        self.len = j;
        self.idx = 0;
    }

    pub fn next(&mut self, pos: &Position, hist: &History) -> Option<Move> {
        loop {
            match self.stage {
                Stage::TtMove => {
                    self.stage = Stage::GenNoisy;
                    return Some(self.tt_move);
                }
                Stage::GenNoisy => {
                    self.fill(pos, kind::NOISY, |m| {
                        let victim = pos.captured(m).unwrap_or(PieceType::Pawn);
                        let mvv = p::mvv_mult();
                        let mut score = mvv * see_value(victim) + hist.capture_score(pos.moved_piece(m), m.to(), victim);
                        if m.promotion() == Some(PieceType::Queen) {
                            score += mvv * see_value(PieceType::Queen);
                        }
                        score
                    });
                    self.stage = Stage::GoodNoisy;
                }
                Stage::GoodNoisy => {
                    while let Some((m, score)) = self.pick_best() {
                        if self.qsearch || see_ge(pos, m, -score / 64) {
                            return Some(m);
                        }
                        self.bad.push(m);
                    }
                    self.stage = Stage::GenQuiet;
                }
                Stage::GenQuiet => {
                    if self.skip_quiets {
                        self.stage = Stage::BadNoisy;
                        continue;
                    }
                    let (ctx, killers, counter, order) = (self.ctx, self.killers, self.counter, self.order);
                    let (threat_scale, check_bonus) = (p::threat_order(), p::check_order());
                    self.fill(pos, kind::QUIET, |m| {
                        let piece = pos.moved_piece(m);
                        let mut score = hist.quiet_score(&ctx, piece, m);
                        if let Some(o) = &order {
                            let pt = piece.piece_type();
                            if threat_scale != 0 {
                                let lesser = match pt {
                                    PieceType::Knight | PieceType::Bishop => o.threats.pawn,
                                    PieceType::Rook => o.threats.minor,
                                    PieceType::Queen => o.threats.rook,
                                    _ => 0,
                                };
                                let v = THREAT_VALUE[pt.idx()] * threat_scale / 100;
                                if lesser & bb(m.to()) != 0 {
                                    score -= v * 95 / 100;
                                } else if lesser & bb(m.from()) != 0 {
                                    score += v;
                                }
                            }
                            if check_bonus != 0 && o.check_sq[pt.idx()] & bb(m.to()) != 0 && see_ge(pos, m, -75) {
                                score += check_bonus;
                            }
                        }
                        if m == killers[0] {
                            score += 1 << 22;
                        } else if m == killers[1] {
                            score += 1 << 21;
                        } else if m == counter {
                            score += 1 << 20;
                        }
                        score
                    });
                    self.stage = Stage::Quiet;
                }
                Stage::Quiet => {
                    if !self.skip_quiets {
                        if let Some((m, _)) = self.pick_best() {
                            return Some(m);
                        }
                    }
                    self.stage = Stage::BadNoisy;
                }
                Stage::BadNoisy => {
                    if self.bad_idx < self.bad.len() {
                        self.bad_idx += 1;
                        return Some(self.bad[self.bad_idx - 1]);
                    }
                    self.stage = Stage::Done;
                }
                Stage::Done => return None,
            }
        }
    }
}
