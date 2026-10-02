//! Alpha-beta search: iterative deepening, aspiration windows, principal
//! variation search, quiescence, transposition table and the usual pruning
//! and reduction heuristics. Several threads share one table (lazy SMP).

use crate::eval;
use crate::history::{pawn_key, ContKey, History};
use crate::movepick::MovePicker;
use crate::nnue::{Accumulators, Network};
use crate::params as p;
use crate::position::Position;
use crate::see::{see_ge, see_value};
use crate::tt::*;
use crate::types::*;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use crate::time::Instant;
use std::time::Duration;

pub const MAX_PLY: usize = 128;
pub const INF: i32 = 32001;
pub const MATE: i32 = 32000;
pub const MATE_IN_MAX: i32 = MATE - MAX_PLY as i32;
pub const DRAW: i32 = 0;

/// Soft time limit scale by how many iterations in a row the best move held.
const STABILITY: [f64; 5] = [2.5, 1.2, 0.9, 0.8, 0.75];

#[derive(Clone, Debug, Default)]
pub struct Limits {
    pub depth: Option<i32>,
    /// Hard node limit: abort as soon as it is reached.
    pub nodes: Option<u64>,
    /// Soft node limit: finish the current iteration, then stop.
    pub soft_nodes: Option<u64>,
    pub movetime: Option<u64>,
    pub wtime: Option<u64>,
    pub btime: Option<u64>,
    pub winc: Option<u64>,
    pub binc: Option<u64>,
    pub movestogo: Option<u64>,
    pub infinite: bool,
}

#[derive(Clone, Debug)]
pub struct SearchInfo {
    pub depth: i32,
    pub seldepth: usize,
    pub score: i32,
    pub nodes: u64,
    pub time_ms: u64,
    pub hashfull: usize,
    pub pv: Vec<Move>,
}

impl SearchInfo {
    pub fn score_string(&self) -> String {
        score_to_uci(self.score)
    }
}

pub fn score_to_uci(score: i32) -> String {
    if score >= MATE_IN_MAX {
        format!("mate {}", (MATE - score + 1) / 2)
    } else if score <= -MATE_IN_MAX {
        format!("mate -{}", (MATE + score) / 2)
    } else {
        format!("cp {score}")
    }
}

#[derive(Copy, Clone, Default)]
struct StackEntry {
    static_eval: i32,
    killers: [Move; 2],
    excluded: Move,
    cont: ContKey,
    current: Move,
}

pub struct SearchResult {
    pub best_move: Move,
    pub score: i32,
    pub depth: i32,
    pub nodes: u64,
    pub pv: Vec<Move>,
}

/// State shared by every search thread.
pub struct Shared {
    pub tt: TranspositionTable,
    pub stop: AtomicBool,
    pub nodes: AtomicU64,
    pub network: Option<Arc<Network>>,
}

impl Shared {
    pub fn new(hash_mb: usize, network: Option<Arc<Network>>) -> Arc<Shared> {
        Arc::new(Shared {
            tt: TranspositionTable::new(hash_mb),
            stop: AtomicBool::new(false),
            nodes: AtomicU64::new(0),
            network,
        })
    }
}

pub struct Searcher {
    pub shared: Arc<Shared>,
    pub history: Box<History>,
    stack: Vec<StackEntry>,
    pv: Vec<[Move; MAX_PLY + 1]>,
    pv_len: [usize; MAX_PLY + 1],
    hashes: Vec<u64>,
    pub nodes: u64,
    flushed_nodes: u64,
    seldepth: usize,
    start: Instant,
    hard_deadline: Option<Instant>,
    hard_nodes: Option<u64>,
    stopped: bool,
    main_thread: bool,
    root_best: (Move, i32),
    lmr: Box<[[i32; 64]; 64]>,
    /// Nodes spent below each root move [from][to] in this search.
    root_nodes: Box<[[u64; 64]; 64]>,
    acc: Option<Accumulators>,
    /// Print UCI info lines (main thread of an interactive search).
    pub verbose: bool,
}

impl Searcher {
    pub fn new(shared: Arc<Shared>) -> Searcher {
        let acc = shared.network.as_ref().map(|n| Accumulators::new(n.clone(), MAX_PLY + 2));
        Searcher {
            shared,
            history: History::new(),
            stack: vec![StackEntry::default(); MAX_PLY + 4],
            pv: vec![[Move::NULL; MAX_PLY + 1]; MAX_PLY + 1],
            pv_len: [0; MAX_PLY + 1],
            hashes: Vec::with_capacity(1024),
            nodes: 0,
            flushed_nodes: 0,
            seldepth: 0,
            start: Instant::now(),
            hard_deadline: None,
            hard_nodes: None,
            stopped: false,
            main_thread: true,
            root_best: (Move::NULL, -INF),
            lmr: Box::new([[0; 64]; 64]),
            root_nodes: Box::new([[0; 64]; 64]),
            acc,
            verbose: true,
        }
    }

    pub fn clear(&mut self) {
        self.history.clear();
    }

    fn init_lmr(&mut self) {
        let base = p::lmr_base() as f64 / 100.0;
        let div = p::lmr_div() as f64 / 100.0;
        for d in 1..64 {
            for m in 1..64 {
                self.lmr[d][m] = (base + (d as f64).ln() * (m as f64).ln() / div) as i32;
            }
        }
    }

    #[inline(always)]
    fn evaluate(&mut self, pos: &Position, ply: usize) -> i32 {
        let raw = match &mut self.acc {
            Some(acc) => acc.evaluate(pos, ply),
            None => eval::evaluate(pos),
        };
        // Damp evaluations as the fifty-move counter grows.
        let raw = raw * (200 - pos.halfmove_clock() as i32) / 200;
        raw.clamp(-MATE_IN_MAX + 1, MATE_IN_MAX - 1)
    }

    /// Static evaluation adjusted by what search has learned about this pawn structure.
    #[inline(always)]
    fn corrected(&self, pos: &Position, raw: i32) -> i32 {
        let w = p::corr_pawn();
        if w == 0 {
            return raw;
        }
        let key = pawn_key(pos.pieces(Color::White, PieceType::Pawn), pos.pieces(Color::Black, PieceType::Pawn));
        (raw + self.history.correction(pos.side_to_move(), key, w)).clamp(-MATE_IN_MAX + 1, MATE_IN_MAX - 1)
    }

    #[inline(always)]
    fn push_move(&mut self, parent: &Position, m: Move, child: &Position, ply: usize) {
        self.hashes.push(child.hash());
        if let Some(acc) = &mut self.acc {
            acc.push(parent, m, child, ply + 1);
        }
    }

    #[inline(always)]
    fn pop_move(&mut self) {
        self.hashes.pop();
    }

    #[inline(always)]
    fn check_stop(&mut self) -> bool {
        if self.stopped {
            return true;
        }
        if self.nodes & 1023 == 0 {
            let delta = self.nodes - self.flushed_nodes;
            self.shared.nodes.fetch_add(delta, Ordering::Relaxed);
            self.flushed_nodes = self.nodes;
            if self.shared.stop.load(Ordering::Relaxed) {
                self.stopped = true;
            } else if self.main_thread {
                if let Some(d) = self.hard_deadline {
                    if Instant::now() >= d {
                        self.stopped = true;
                    }
                }
            }
        }
        if self.main_thread {
            if let Some(n) = self.hard_nodes {
                if self.nodes >= n {
                    self.stopped = true;
                }
            }
        }
        if self.stopped && self.main_thread {
            self.shared.stop.store(true, Ordering::Relaxed);
        }
        self.stopped
    }

    fn is_repetition(&self, pos: &Position, ply: usize) -> bool {
        let n = self.hashes.len();
        let max_back = (pos.halfmove_clock() as usize).min(n - 1);
        let h = pos.hash();
        let mut count = 0;
        let mut i = 4;
        while i <= max_back {
            if self.hashes[n - 1 - i] == h {
                if i <= ply {
                    return true;
                }
                count += 1;
                if count >= 2 {
                    return true;
                }
            }
            i += 2;
        }
        false
    }

    fn is_draw(&self, pos: &Position, ply: usize) -> bool {
        if pos.halfmove_clock() >= 100 && (!pos.in_check() || !pos.legal_moves().is_empty()) {
            return true;
        }
        pos.is_insufficient_material() || self.is_repetition(pos, ply)
    }

    fn update_pv(&mut self, ply: usize, m: Move) {
        self.pv[ply][ply] = m;
        let child_len = self.pv_len[ply + 1].max(ply + 1);
        for i in ply + 1..child_len {
            self.pv[ply][i] = self.pv[ply + 1][i];
        }
        self.pv_len[ply] = child_len;
    }

    fn cont_keys(&self, ply: usize) -> (ContKey, ContKey) {
        let c1 = if ply >= 1 { self.stack[ply - 1].cont } else { ContKey::NONE };
        let c2 = if ply >= 2 { self.stack[ply - 2].cont } else { ContKey::NONE };
        (c1, c2)
    }

    fn hist_bonus(depth: i32) -> i32 {
        (p::hist_mult() * depth).min(p::hist_max())
    }

    // ------------------------------------------------------------------ main search

    #[allow(clippy::too_many_arguments)]
    fn search<const PV: bool>(&mut self, pos: &Position, mut alpha: i32, mut beta: i32, mut depth: i32, ply: usize, cut_node: bool) -> i32 {
        let root = ply == 0;
        let in_check = pos.in_check();
        if PV {
            self.pv_len[ply] = ply;
        }
        if depth <= 0 {
            return self.qsearch::<PV>(pos, alpha, beta, ply);
        }
        self.nodes += 1;
        if self.check_stop() {
            return 0;
        }
        if PV && ply > self.seldepth {
            self.seldepth = ply;
        }
        if !root {
            if self.is_draw(pos, ply) {
                return DRAW;
            }
            if ply >= MAX_PLY - 1 {
                return if in_check { DRAW } else { self.evaluate(pos, ply) };
            }
            alpha = alpha.max(-MATE + ply as i32);
            beta = beta.min(MATE - ply as i32 - 1);
            if alpha >= beta {
                return alpha;
            }
        }

        let excluded = self.stack[ply].excluded;
        let tt_hit = if excluded.is_null() { self.shared.tt.probe(pos.hash()) } else { None };
        let tt_move = tt_hit.map_or(Move::NULL, |e| e.mv);
        let tt_score = tt_hit.map_or(-INF, |e| score_from_tt(e.score, ply));
        if let Some(e) = tt_hit {
            if !PV
                && e.depth >= depth
                && (e.bound == BOUND_EXACT
                    || (e.bound == BOUND_LOWER && tt_score >= beta)
                    || (e.bound == BOUND_UPPER && tt_score <= alpha))
            {
                return tt_score;
            }
        }
        let tt_pv = PV || tt_hit.is_some_and(|e| e.pv);

        // Static evaluation: the raw value is what the table keeps, the corrected
        // value is what pruning decisions use.
        let raw_eval;
        let static_eval;
        let mut eval;
        if in_check {
            raw_eval = -INF;
            static_eval = -INF;
            eval = -INF;
        } else if let Some(e) = tt_hit {
            raw_eval = if e.eval != -INF && e.eval.abs() < MATE_IN_MAX { e.eval } else { self.evaluate(pos, ply) };
            static_eval = self.corrected(pos, raw_eval);
            eval = static_eval;
            let tt_better = (e.bound == BOUND_LOWER && tt_score > eval) || (e.bound == BOUND_UPPER && tt_score < eval) || e.bound == BOUND_EXACT;
            if tt_better && tt_score.abs() < MATE_IN_MAX {
                eval = tt_score;
            }
        } else {
            raw_eval = self.evaluate(pos, ply);
            static_eval = self.corrected(pos, raw_eval);
            eval = static_eval;
            if excluded.is_null() {
                self.shared.tt.store(pos.hash(), Move::NULL, -INF, raw_eval, 0, BOUND_NONE, tt_pv);
            }
        }
        self.stack[ply].static_eval = static_eval;
        let improving = !in_check && ply >= 2 && static_eval > self.stack[ply - 2].static_eval;
        self.stack[ply + 1].killers = [Move::NULL; 2];

        let us = pos.side_to_move();
        if !PV && !in_check && excluded.is_null() {
            // Razoring: far below alpha at low depth, only captures can save it.
            if p::razor_margin() > 0 && depth <= 4 && alpha.abs() < MATE_IN_MAX && eval + p::razor_margin() * depth <= alpha {
                let score = self.qsearch::<false>(pos, alpha, alpha + 1, ply);
                if self.stopped {
                    return 0;
                }
                if score <= alpha {
                    return score;
                }
            }
            // Reverse futility pruning.
            if depth <= p::rfp_depth() && eval.abs() < MATE_IN_MAX && eval - p::rfp_margin() * (depth - improving as i32) >= beta {
                return (eval + beta) / 2;
            }
            // Null-move pruning.
            if depth >= 3
                && eval >= beta
                && static_eval >= beta
                && ply >= 1
                && !self.stack[ply - 1].current.is_null()
                && pos.non_pawn_material(us) != 0
                && beta > -MATE_IN_MAX
            {
                let r = p::nmp_base() + depth / p::nmp_depth_div() + ((eval - beta) / p::nmp_eval_div()).min(3);
                let mut child = *pos;
                child.play_null();
                self.stack[ply].current = Move::NULL;
                self.stack[ply].cont = ContKey::NONE;
                self.hashes.push(child.hash());
                if let Some(acc) = &mut self.acc {
                    acc.copy_parent(ply + 1);
                }
                let score = -self.search::<false>(&child, -beta, -beta + 1, depth - r, ply + 1, !cut_node);
                self.hashes.pop();
                if self.stopped {
                    return 0;
                }
                if score >= beta {
                    return if score >= MATE_IN_MAX { beta } else { score };
                }
            }
            // ProbCut: a capture that clears beta by a margin at reduced depth
            // almost always clears beta at full depth.
            let pc_beta = beta + p::probcut_margin();
            if p::probcut_margin() > 0
                && depth >= 5
                && beta.abs() < MATE_IN_MAX
                && !tt_hit.is_some_and(|e| e.depth >= depth - 3 && tt_score < pc_beta)
            {
                let (c1, c2) = self.cont_keys(ply);
                let mut picker = MovePicker::qsearch(pos, tt_move, c1, c2);
                while let Some(m) = picker.next(pos, &self.history) {
                    if !see_ge(pos, m, pc_beta - static_eval) {
                        continue;
                    }
                    let child = pos.after(m);
                    self.stack[ply].current = m;
                    self.stack[ply].cont = ContKey { piece: pos.moved_piece(m).0, to: m.to() };
                    self.push_move(pos, m, &child, ply);
                    let mut s = -self.qsearch::<false>(&child, -pc_beta, -pc_beta + 1, ply + 1);
                    if s >= pc_beta {
                        s = -self.search::<false>(&child, -pc_beta, -pc_beta + 1, depth - 4, ply + 1, !cut_node);
                    }
                    self.pop_move();
                    if self.stopped {
                        return 0;
                    }
                    if s >= pc_beta {
                        self.shared.tt.store(pos.hash(), m, score_to_tt(s, ply), raw_eval, depth - 3, BOUND_LOWER, tt_pv);
                        return s;
                    }
                }
            }
        }

        // Internal iterative reduction.
        if depth >= 4 && tt_move.is_null() && excluded.is_null() && (PV || cut_node) {
            depth -= 1;
        }

        let (c1, c2) = self.cont_keys(ply);
        let counter = if c1.piece != 12 { self.history.counter[c1.piece as usize][c1.to as usize] } else { Move::NULL };
        let mut picker = MovePicker::new(pos, tt_move, self.stack[ply].killers, counter, c1, c2);
        let mut best_score = -INF;
        let mut best_move = Move::NULL;
        let mut moves_searched = 0usize;
        let mut quiets: [Move; 64] = [Move::NULL; 64];
        let mut n_quiets = 0;
        let mut noisies: [Move; 32] = [Move::NULL; 32];
        let mut n_noisies = 0;

        while let Some(m) = picker.next(pos, &self.history) {
            if m == excluded {
                continue;
            }
            let is_quiet = m.is_quiet();
            let piece = pos.moved_piece(m);
            let hist = if is_quiet { self.history.quiet_score(us, piece, m, c1, c2) } else { 0 };
            let lmr_base = self.lmr[(depth as usize).min(63)][moves_searched.min(63)];

            if !root && best_score > -MATE_IN_MAX {
                let lmr_depth = (depth - lmr_base).max(0);
                if is_quiet {
                    // Late-move pruning.
                    let lmp_limit = (p::lmp_base() + depth * depth) / (2 - improving as i32);
                    if !in_check && moves_searched as i32 >= lmp_limit {
                        picker.skip_quiets();
                        continue;
                    }
                    // Futility pruning.
                    if !in_check && lmr_depth <= 8 && static_eval + p::fut_base() + p::fut_mult() * lmr_depth <= alpha {
                        picker.skip_quiets();
                        continue;
                    }
                    if !see_ge(pos, m, -p::see_quiet() * lmr_depth) {
                        continue;
                    }
                } else if depth <= 8 && !see_ge(pos, m, -p::see_noisy() * depth * depth) {
                    continue;
                }
            }

            // Singular extension.
            let mut ext = 0;
            if !root && m == tt_move && excluded.is_null() && depth >= p::se_depth() {
                if let Some(e) = tt_hit {
                    if e.depth >= depth - 3 && e.bound & BOUND_LOWER != 0 && tt_score.abs() < MATE_IN_MAX {
                        let s_beta = tt_score - depth;
                        let s_depth = (depth - 1) / 2;
                        self.stack[ply].excluded = m;
                        let s = self.search::<false>(pos, s_beta - 1, s_beta, s_depth, ply, cut_node);
                        self.stack[ply].excluded = Move::NULL;
                        if self.stopped {
                            return 0;
                        }
                        if s < s_beta {
                            ext = if !PV && s < s_beta - p::se_double_margin() { 2 } else { 1 };
                        } else if s_beta >= beta {
                            return s_beta;
                        } else if tt_score >= beta {
                            ext = -1;
                        }
                    }
                }
            }

            let child = pos.after(m);
            self.stack[ply].current = m;
            self.stack[ply].cont = ContKey { piece: piece.0, to: m.to() };
            self.push_move(pos, m, &child, ply);
            if PV {
                self.pv_len[ply + 1] = ply + 1;
            }
            let mut new_depth = depth - 1 + ext;
            let nodes_before = self.nodes;
            let score = if moves_searched == 0 {
                -self.search::<PV>(&child, -beta, -alpha, new_depth, ply + 1, false)
            } else {
                let mut r = 0;
                if depth >= 3 && moves_searched > 2 * PV as usize {
                    r = lmr_base;
                    if is_quiet {
                        r -= hist / p::lmr_hist_div();
                    } else {
                        r -= 1;
                    }
                    if !tt_pv {
                        r += 1;
                    }
                    if cut_node {
                        r += 1;
                    }
                    if !improving {
                        r += 1;
                    }
                    if child.in_check() {
                        r -= 1;
                    }
                    if m == self.stack[ply].killers[0] || m == self.stack[ply].killers[1] {
                        r -= 1;
                    }
                    r = r.clamp(0, (new_depth - 1).max(0));
                }
                let reduced = new_depth - r;
                let mut s = -self.search::<false>(&child, -alpha - 1, -alpha, reduced, ply + 1, true);
                if s > alpha && r > 0 {
                    if p::lmr_deeper() != 0 {
                        new_depth += (s > best_score + 40 + 2 * new_depth) as i32 - (s < best_score + new_depth) as i32;
                    }
                    if new_depth > reduced {
                        s = -self.search::<false>(&child, -alpha - 1, -alpha, new_depth, ply + 1, !cut_node);
                    }
                }
                if PV && s > alpha && s < beta {
                    s = -self.search::<true>(&child, -beta, -alpha, new_depth, ply + 1, false);
                }
                s
            };
            self.pop_move();
            moves_searched += 1;
            if root {
                self.root_nodes[m.from() as usize][m.to() as usize] += self.nodes - nodes_before;
            }
            if self.stopped {
                return 0;
            }

            if score > best_score {
                best_score = score;
                if score > alpha {
                    best_move = m;
                    if PV {
                        self.update_pv(ply, m);
                    }
                    if root {
                        self.root_best = (m, score);
                    }
                    if score >= beta {
                        break;
                    }
                    alpha = score;
                }
            }
            if m != best_move {
                if is_quiet {
                    if n_quiets < 64 {
                        quiets[n_quiets] = m;
                        n_quiets += 1;
                    }
                } else if n_noisies < 32 {
                    noisies[n_noisies] = m;
                    n_noisies += 1;
                }
            }
        }

        if moves_searched == 0 {
            if !excluded.is_null() {
                return alpha;
            }
            return if in_check { -MATE + ply as i32 } else { DRAW };
        }

        if best_score >= beta {
            let bonus = Self::hist_bonus(depth);
            let bp = pos.moved_piece(best_move);
            if best_move.is_quiet() {
                self.history.update_quiet(us, bp, best_move, c1, c2, bonus);
                for &q in &quiets[..n_quiets] {
                    let qp = pos.moved_piece(q);
                    self.history.update_quiet(us, qp, q, c1, c2, -bonus);
                }
                let k = &mut self.stack[ply].killers;
                if k[0] != best_move {
                    k[1] = k[0];
                    k[0] = best_move;
                }
                if c1.piece != 12 {
                    self.history.counter[c1.piece as usize][c1.to as usize] = best_move;
                }
            } else {
                let victim = pos.captured(best_move).unwrap_or(PieceType::Pawn);
                self.history.update_capture(bp, best_move.to(), victim, bonus);
            }
            for &n in &noisies[..n_noisies] {
                let victim = pos.captured(n).unwrap_or(PieceType::Pawn);
                self.history.update_capture(pos.moved_piece(n), n.to(), victim, -bonus);
            }
        }

        if excluded.is_null() {
            let bound = if best_score >= beta {
                BOUND_LOWER
            } else if PV && !best_move.is_null() {
                BOUND_EXACT
            } else {
                BOUND_UPPER
            };
            // Learn how far search landed from the static evaluation, when the
            // result says something about it (not a capture, bound on the right side).
            if p::corr_pawn() != 0
                && !in_check
                && !best_move.is_noisy()
                && best_score.abs() < MATE_IN_MAX
                && !(bound == BOUND_LOWER && best_score <= static_eval)
                && !(bound == BOUND_UPPER && best_score >= static_eval)
            {
                let key = pawn_key(pos.pieces(Color::White, PieceType::Pawn), pos.pieces(Color::Black, PieceType::Pawn));
                self.history.update_correction(us, key, depth, best_score - raw_eval);
            }
            self.shared.tt.store(pos.hash(), best_move, score_to_tt(best_score, ply), raw_eval, depth, bound, tt_pv);
        }
        best_score
    }

    // ------------------------------------------------------------------ quiescence

    fn qsearch<const PV: bool>(&mut self, pos: &Position, mut alpha: i32, beta: i32, ply: usize) -> i32 {
        self.nodes += 1;
        if PV {
            self.pv_len[ply] = ply;
            if ply > self.seldepth {
                self.seldepth = ply;
            }
        }
        if self.check_stop() {
            return 0;
        }
        if pos.is_insufficient_material() || (ply > 0 && self.is_repetition(pos, ply)) {
            return DRAW;
        }
        let in_check = pos.in_check();
        if ply >= MAX_PLY - 1 {
            return if in_check { DRAW } else { self.evaluate(pos, ply) };
        }

        let tt_hit = self.shared.tt.probe(pos.hash());
        let tt_score = tt_hit.map_or(-INF, |e| score_from_tt(e.score, ply));
        if let Some(e) = tt_hit {
            if !PV
                && (e.bound == BOUND_EXACT
                    || (e.bound == BOUND_LOWER && tt_score >= beta)
                    || (e.bound == BOUND_UPPER && tt_score <= alpha))
            {
                return tt_score;
            }
        }

        let mut best_score;
        let raw_eval;
        let static_eval;
        if in_check {
            best_score = -INF;
            raw_eval = -INF;
            static_eval = -INF;
        } else {
            raw_eval = match tt_hit {
                Some(e) if e.eval != -INF && e.eval.abs() < MATE_IN_MAX => e.eval,
                _ => self.evaluate(pos, ply),
            };
            static_eval = self.corrected(pos, raw_eval);
            best_score = static_eval;
            if let Some(e) = tt_hit {
                if tt_score.abs() < MATE_IN_MAX
                    && ((e.bound == BOUND_LOWER && tt_score > best_score) || (e.bound == BOUND_UPPER && tt_score < best_score))
                {
                    best_score = tt_score;
                }
            }
            if best_score >= beta {
                return best_score;
            }
            alpha = alpha.max(best_score);
        }

        let (c1, c2) = self.cont_keys(ply);
        let tt_move = tt_hit.map_or(Move::NULL, |e| e.mv);
        let mut picker = MovePicker::qsearch(pos, tt_move, c1, c2);
        let mut best_move = Move::NULL;
        let mut moves_searched = 0;
        let fut_base = if in_check || p::qs_fut_margin() == 0 { -INF } else { static_eval + p::qs_fut_margin() };
        while let Some(m) = picker.next(pos, &self.history) {
            // Futility: a capture that cannot lift the score to alpha even with a margin.
            if fut_base > -INF && m.promotion().is_none() && !pos.gives_check(m) {
                let gain = fut_base + see_value(pos.captured(m).unwrap_or(PieceType::Pawn));
                if gain <= alpha {
                    best_score = best_score.max(gain);
                    continue;
                }
                if fut_base <= alpha && !see_ge(pos, m, 1) {
                    best_score = best_score.max(fut_base);
                    continue;
                }
            }
            if !in_check && best_score > -MATE_IN_MAX && !see_ge(pos, m, p::qs_see()) {
                continue;
            }
            let child = pos.after(m);
            self.stack[ply].current = m;
            self.stack[ply].cont = ContKey { piece: pos.moved_piece(m).0, to: m.to() };
            self.push_move(pos, m, &child, ply);
            if PV {
                self.pv_len[ply + 1] = ply + 1;
            }
            let score = -self.qsearch::<PV>(&child, -beta, -alpha, ply + 1);
            self.pop_move();
            moves_searched += 1;
            if self.stopped {
                return 0;
            }
            if score > best_score {
                best_score = score;
                if score > alpha {
                    best_move = m;
                    if PV {
                        self.update_pv(ply, m);
                    }
                    if score >= beta {
                        break;
                    }
                    alpha = score;
                }
            }
        }
        if in_check && moves_searched == 0 {
            return -MATE + ply as i32;
        }
        let bound = if best_score >= beta { BOUND_LOWER } else { BOUND_UPPER };
        self.shared.tt.store(pos.hash(), best_move, score_to_tt(best_score, ply), raw_eval, 0, bound, false);
        best_score
    }

    // ------------------------------------------------------------------ driver

    /// Set the hard deadline; return the soft limit in ms and whether it may be
    /// scaled by how the search is going (not for a fixed time per move).
    fn set_time_limits(&mut self, stm: Color, limits: &Limits, move_overhead: u64) -> Option<(u64, bool)> {
        self.hard_deadline = None;
        let (time, inc) = match stm {
            Color::White => (limits.wtime, limits.winc.unwrap_or(0)),
            Color::Black => (limits.btime, limits.binc.unwrap_or(0)),
        };
        if let Some(mt) = limits.movetime {
            let t = mt.saturating_sub(move_overhead).max(1);
            self.hard_deadline = Some(self.start + Duration::from_millis(t));
            return Some((t, false));
        }
        let time = time?;
        let avail = time.saturating_sub(move_overhead).max(1);
        let mtg = limits.movestogo.unwrap_or(25).clamp(1, 50);
        let base = avail / mtg + inc * 3 / 4;
        let soft = (base * p::tm_soft_pct() as u64 / 100).min(avail / 2).max(1);
        let hard = (base * p::tm_hard_mult() as u64).min(avail * 3 / 4).max(1);
        self.hard_deadline = Some(self.start + Duration::from_millis(hard));
        Some((soft, true))
    }

    /// Search `root` (whose game history hashes, oldest first and including
    /// `root`, are `history`) within `limits`. `report` receives one
    /// `SearchInfo` per completed iteration when this is the main thread.
    pub fn go(
        &mut self,
        root: &Position,
        history: &[u64],
        limits: &Limits,
        main_thread: bool,
        move_overhead: u64,
        report: &mut dyn FnMut(&SearchInfo),
    ) -> SearchResult {
        self.start = Instant::now();
        self.main_thread = main_thread;
        self.stopped = false;
        self.nodes = 0;
        self.flushed_nodes = 0;
        self.seldepth = 0;
        self.root_best = (Move::NULL, -INF);
        self.hard_nodes = limits.nodes;
        self.hashes.clear();
        self.hashes.extend_from_slice(history);
        if self.hashes.last() != Some(&root.hash()) {
            self.hashes.push(root.hash());
        }
        self.init_lmr();
        for e in self.stack.iter_mut() {
            *e = StackEntry::default();
        }
        for r in self.root_nodes.iter_mut() {
            *r = [0; 64];
        }
        if let Some(acc) = &mut self.acc {
            acc.refresh(root, 0);
        }
        let soft_limit = if main_thread { self.set_time_limits(root.side_to_move(), limits, move_overhead) } else { None };
        let mut stability = 0usize;

        let legal = root.legal_moves();
        let mut result = SearchResult { best_move: Move::NULL, score: 0, depth: 0, nodes: 0, pv: Vec::new() };
        if legal.is_empty() {
            return result;
        }
        result.best_move = legal[0];
        let max_depth = limits.depth.unwrap_or(MAX_PLY as i32 - 1).clamp(1, MAX_PLY as i32 - 1);
        let mut score = 0;
        for depth in 1..=max_depth {
            self.seldepth = 0;
            self.root_best = (Move::NULL, -INF);
            // Aspiration windows.
            let mut delta = p::asp_delta();
            let (mut alpha, mut beta) = if depth >= 4 { ((score - delta).max(-INF), (score + delta).min(INF)) } else { (-INF, INF) };
            let mut search_depth = depth;
            loop {
                let s = self.search::<true>(root, alpha, beta, search_depth.max(1), 0, false);
                if self.stopped {
                    break;
                }
                if s <= alpha {
                    beta = (alpha + beta) / 2;
                    alpha = (s - delta).max(-INF);
                    search_depth = depth;
                } else if s >= beta {
                    beta = (s + delta).min(INF);
                    search_depth = (search_depth - 1).max(depth - 3);
                } else {
                    score = s;
                    break;
                }
                delta += delta / 2;
            }
            if self.stopped {
                // Keep a verified better root move found in the unfinished iteration.
                if depth > 1 && !self.root_best.0.is_null() {
                    result.best_move = self.root_best.0;
                }
                break;
            }
            if self.pv_len[0] > 0 {
                if self.pv[0][0] == result.best_move {
                    stability += 1;
                } else {
                    stability = 0;
                }
                result.best_move = self.pv[0][0];
            }
            result.score = score;
            result.depth = depth;
            result.pv = self.pv[0][..self.pv_len[0]].to_vec();
            if main_thread && self.verbose {
                let total = self.shared.nodes.load(Ordering::Relaxed) + (self.nodes - self.flushed_nodes);
                report(&SearchInfo {
                    depth,
                    seldepth: self.seldepth,
                    score,
                    nodes: total,
                    time_ms: self.start.elapsed().as_millis() as u64,
                    hashfull: self.shared.tt.hashfull(),
                    pv: result.pv.clone(),
                });
            }
            if main_thread {
                if let Some(sn) = limits.soft_nodes {
                    if self.nodes >= sn {
                        break;
                    }
                }
                if let Some((soft, scalable)) = soft_limit {
                    let mut limit = soft as f64;
                    if scalable && p::tm_nodes() != 0 {
                        // Spend less when one move takes most of the effort and keeps winning
                        // iteration after iteration; more when the choice is contested.
                        let b = result.best_move;
                        let frac = self.root_nodes[b.from() as usize][b.to() as usize] as f64 / self.nodes.max(1) as f64;
                        limit *= (p::tm_node_base() as f64 / 100.0 - frac) * p::tm_node_mult() as f64 / 100.0;
                        limit *= STABILITY[stability.min(4)];
                    }
                    if self.start.elapsed().as_secs_f64() * 1000.0 >= limit {
                        break;
                    }
                }
            }
            if self.shared.stop.load(Ordering::Relaxed) {
                break;
            }
        }
        if main_thread {
            self.shared.stop.store(true, Ordering::Relaxed);
        }
        self.shared.nodes.fetch_add(self.nodes - self.flushed_nodes, Ordering::Relaxed);
        self.flushed_nodes = self.nodes;
        result.nodes = self.nodes;
        result
    }
}

#[inline(always)]
fn score_to_tt(score: i32, ply: usize) -> i32 {
    if score >= MATE_IN_MAX {
        score + ply as i32
    } else if score <= -MATE_IN_MAX {
        score - ply as i32
    } else {
        score
    }
}

#[inline(always)]
fn score_from_tt(score: i32, ply: usize) -> i32 {
    if score == -INF {
        -INF
    } else if score >= MATE_IN_MAX {
        score - ply as i32
    } else if score <= -MATE_IN_MAX {
        score + ply as i32
    } else {
        score
    }
}

/// Run a (possibly multi-threaded) search and return the main thread's result.
pub fn search_threads(
    searchers: &mut [Searcher],
    root: &Position,
    history: &[u64],
    limits: &Limits,
    move_overhead: u64,
    report: &mut (dyn FnMut(&SearchInfo) + Send),
) -> SearchResult {
    let shared = searchers[0].shared.clone();
    shared.stop.store(false, Ordering::Relaxed);
    shared.nodes.store(0, Ordering::Relaxed);
    shared.tt.new_search();
    let (main, helpers) = searchers.split_first_mut().expect("at least one searcher");
    std::thread::scope(|s| {
        for h in helpers.iter_mut() {
            let lim = Limits { infinite: true, depth: limits.depth, ..Default::default() };
            s.spawn(move || {
                h.verbose = false;
                h.go(root, history, &lim, false, move_overhead, &mut |_| {});
            });
        }
        let mut r = main.go(root, history, limits, true, move_overhead, report);
        shared.stop.store(true, Ordering::Relaxed);
        r.nodes = shared.nodes.load(Ordering::Relaxed);
        r
    })
}
