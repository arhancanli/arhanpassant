//! Staged move ordering: hash move, good captures, quiets (history-ordered,
//! killers and counter-move first), then losing captures.

use crate::history::{ContKey, History};
use crate::movegen::{generate, kind, MAX_MOVES};
use crate::position::Position;
use crate::see::{see_ge, see_value};
use crate::types::*;

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
    c1: ContKey,
    c2: ContKey,
    /// 4-ply continuation key (`ContKey::NONE` when the `cont4` setting is off).
    pub c4: ContKey,
    moves: [Move; MAX_MOVES],
    scores: [i32; MAX_MOVES],
    len: usize,
    idx: usize,
    bad: [Move; MAX_MOVES],
    bad_len: usize,
    bad_idx: usize,
    skip_quiets: bool,
    /// Quiescence: only noisy moves (unless in check), no good/bad split.
    qsearch: bool,
}

impl MovePicker {
    pub fn new(pos: &Position, tt_move: Move, killers: [Move; 2], counter: Move, c1: ContKey, c2: ContKey) -> MovePicker {
        let tt_ok = !tt_move.is_null() && pos.is_legal(tt_move);
        MovePicker {
            stage: if tt_ok { Stage::TtMove } else { Stage::GenNoisy },
            tt_move: if tt_ok { tt_move } else { Move::NULL },
            killers,
            counter,
            c1,
            c2,
            c4: ContKey::NONE,
            moves: [Move::NULL; MAX_MOVES],
            scores: [0; MAX_MOVES],
            len: 0,
            idx: 0,
            bad: [Move::NULL; MAX_MOVES],
            bad_len: 0,
            bad_idx: 0,
            skip_quiets: false,
            qsearch: false,
        }
    }

    pub fn qsearch(pos: &Position, tt_move: Move, c1: ContKey, c2: ContKey) -> MovePicker {
        let in_check = pos.in_check();
        let usable = !tt_move.is_null() && (in_check || tt_move.is_noisy());
        let mut mp = MovePicker::new(pos, if usable { tt_move } else { Move::NULL }, [Move::NULL; 2], Move::NULL, c1, c2);
        mp.qsearch = true;
        mp.skip_quiets = !in_check;
        mp
    }

    /// Stop handing out quiet moves (late-move / futility pruning decided they are hopeless).
    pub fn skip_quiets(&mut self) {
        self.skip_quiets = true;
    }

    fn pick_best(&mut self) -> Option<(Move, i32)> {
        if self.idx >= self.len {
            return None;
        }
        let mut best = self.idx;
        for i in self.idx + 1..self.len {
            if self.scores[i] > self.scores[best] {
                best = i;
            }
        }
        self.moves.swap(self.idx, best);
        self.scores.swap(self.idx, best);
        let r = (self.moves[self.idx], self.scores[self.idx]);
        self.idx += 1;
        Some(r)
    }

    pub fn next(&mut self, pos: &Position, hist: &History) -> Option<Move> {
        loop {
            match self.stage {
                Stage::TtMove => {
                    self.stage = Stage::GenNoisy;
                    return Some(self.tt_move);
                }
                Stage::GenNoisy => {
                    let mut list = crate::movegen::MoveList::new();
                    generate(pos, kind::NOISY, &mut list);
                    self.len = 0;
                    self.idx = 0;
                    for m in list.iter() {
                        if m == self.tt_move {
                            continue;
                        }
                        let victim = pos.captured(m).unwrap_or(PieceType::Pawn);
                        let mut score = 16 * see_value(victim)
                            + hist.capture_score(pos.moved_piece(m), m.to(), victim);
                        if m.promotion() == Some(PieceType::Queen) {
                            score += 16 * see_value(PieceType::Queen);
                        }
                        self.moves[self.len] = m;
                        self.scores[self.len] = score;
                        self.len += 1;
                    }
                    self.stage = Stage::GoodNoisy;
                }
                Stage::GoodNoisy => {
                    while let Some((m, score)) = self.pick_best() {
                        if self.qsearch || see_ge(pos, m, -score / 64) {
                            return Some(m);
                        }
                        self.bad[self.bad_len] = m;
                        self.bad_len += 1;
                    }
                    self.stage = Stage::GenQuiet;
                }
                Stage::GenQuiet => {
                    if self.skip_quiets {
                        self.stage = Stage::BadNoisy;
                        continue;
                    }
                    let mut list = crate::movegen::MoveList::new();
                    generate(pos, kind::QUIET, &mut list);
                    self.len = 0;
                    self.idx = 0;
                    let stm = pos.side_to_move();
                    for m in list.iter() {
                        if m == self.tt_move {
                            continue;
                        }
                        let piece = pos.moved_piece(m);
                        let mut score = hist.quiet_score(stm, piece, m, self.c1, self.c2, self.c4);
                        if m == self.killers[0] {
                            score += 1 << 22;
                        } else if m == self.killers[1] {
                            score += 1 << 21;
                        } else if m == self.counter {
                            score += 1 << 20;
                        }
                        self.moves[self.len] = m;
                        self.scores[self.len] = score;
                        self.len += 1;
                    }
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
                    if self.bad_idx < self.bad_len {
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
