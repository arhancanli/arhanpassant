//! Playing one game between two UCI engines, with the rules enforced by the
//! arhanpassant library and optional adjudication.

use crate::engine::Engine;
use arhanpassant::{Color, Position};
use std::time::Duration;

#[derive(Clone, Copy, Debug)]
pub enum TimeControl {
    /// Base time and increment, milliseconds.
    Fischer { base: u64, inc: u64 },
    Nodes(u64),
    MoveTime(u64),
}

impl TimeControl {
    /// `8+0.08` (seconds), `nodes=20000`, `movetime=100`.
    pub fn parse(s: &str) -> Result<TimeControl, String> {
        if let Some(n) = s.strip_prefix("nodes=") {
            return n.parse().map(TimeControl::Nodes).map_err(|_| format!("bad nodes {n}"));
        }
        if let Some(n) = s.strip_prefix("movetime=") {
            return n.parse().map(TimeControl::MoveTime).map_err(|_| format!("bad movetime {n}"));
        }
        let (b, i) = s.split_once('+').unwrap_or((s, "0"));
        let b: f64 = b.parse().map_err(|_| format!("bad base time {b}"))?;
        let i: f64 = i.parse().map_err(|_| format!("bad increment {i}"))?;
        Ok(TimeControl::Fischer { base: (b * 1000.0) as u64, inc: (i * 1000.0) as u64 })
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Adjudication {
    /// Resign when the loser reports <= -score and the winner >= score for `moves` consecutive moves each.
    pub resign_score: i32,
    pub resign_moves: u32,
    /// Draw when both sides report |score| <= draw_score for `draw_moves` consecutive moves each, after `draw_after` full moves.
    pub draw_score: i32,
    pub draw_moves: u32,
    pub draw_after: u32,
    pub max_plies: usize,
    /// Allowed clock overrun before a time forfeit, milliseconds.
    pub time_margin: u64,
}

impl Default for Adjudication {
    fn default() -> Self {
        Adjudication { resign_score: 1000, resign_moves: 3, draw_score: 10, draw_moves: 8, draw_after: 34, max_plies: 500, time_margin: 100 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    WhiteWin,
    BlackWin,
    Draw,
}

#[derive(Clone, Debug)]
pub struct GameRecord {
    pub start_fen: String,
    pub moves: Vec<String>,
    pub outcome: Outcome,
    pub reason: String,
}

fn threefold(hashes: &[u64], halfmove: usize) -> bool {
    let n = hashes.len();
    let h = hashes[n - 1];
    let mut count = 1;
    let mut i = 4;
    while i <= halfmove.min(n - 1) {
        if hashes[n - 1 - i] == h {
            count += 1;
            if count >= 3 {
                return true;
            }
        }
        i += 2;
    }
    false
}

fn loss_for(c: Color) -> Outcome {
    if c == Color::White {
        Outcome::BlackWin
    } else {
        Outcome::WhiteWin
    }
}

/// Play a game. `engines[0]` plays White.
pub fn play(engines: [&mut Engine; 2], start_fen: &str, default_tc: TimeControl, adj: &Adjudication) -> GameRecord {
    let [white, black] = engines;
    let mut pos = Position::from_fen(start_fen).expect("opening FEN");
    let mut hashes = vec![pos.hash()];
    let mut moves: Vec<String> = Vec::new();
    let tcs = [white.spec.tc.unwrap_or(default_tc), black.spec.tc.unwrap_or(default_tc)];
    let mut clocks = [0i64; 2];
    for (c, tc) in clocks.iter_mut().zip(tcs) {
        if let TimeControl::Fischer { base, .. } = tc {
            *c = base as i64;
        }
    }
    let incs = tcs.map(|tc| if let TimeControl::Fischer { inc, .. } = tc { inc } else { 0 });
    let mut resign_count = [0u32; 2];
    let mut draw_count = 0u32;
    let mut last_score = [0i32; 2];
    let finish = |moves: Vec<String>, outcome, reason: &str| GameRecord {
        start_fen: start_fen.to_string(),
        moves,
        outcome,
        reason: reason.to_string(),
    };

    if let Err(err) = white.new_game() {
        return finish(moves, loss_for(Color::White), &format!("engine failure: {err}"));
    }
    if let Err(err) = black.new_game() {
        return finish(moves, loss_for(Color::Black), &format!("engine failure: {err}"));
    }

    loop {
        let stm = pos.side_to_move();
        if pos.legal_moves().is_empty() {
            return if pos.in_check() {
                finish(moves, loss_for(stm), "checkmate")
            } else {
                finish(moves, Outcome::Draw, "stalemate")
            };
        }
        if pos.halfmove_clock() >= 100 {
            return finish(moves, Outcome::Draw, "fifty-move rule");
        }
        if pos.is_insufficient_material() {
            return finish(moves, Outcome::Draw, "insufficient material");
        }
        if threefold(&hashes, pos.halfmove_clock() as usize) {
            return finish(moves, Outcome::Draw, "threefold repetition");
        }
        if moves.len() >= adj.max_plies {
            return finish(moves, Outcome::Draw, "game too long");
        }

        let position = if moves.is_empty() {
            format!("position fen {start_fen}")
        } else {
            format!("position fen {start_fen} moves {}", moves.join(" "))
        };
        let tc = tcs[stm as usize];
        let (go, timeout) = match tc {
            TimeControl::Fischer { .. } => (
                format!("go wtime {} btime {} winc {} binc {}", clocks[0].max(1), clocks[1].max(1), incs[0], incs[1]),
                Duration::from_millis(clocks[stm as usize].max(0) as u64 + adj.time_margin + 5000),
            ),
            TimeControl::Nodes(n) => (format!("go nodes {n}"), Duration::from_secs(60)),
            TimeControl::MoveTime(ms) => (format!("go movetime {ms}"), Duration::from_millis(ms + 5000)),
        };
        let engine: &mut Engine = if stm == Color::White { &mut *white } else { &mut *black };
        let reply = match engine.go(&position, &go, timeout) {
            Ok(r) => r,
            Err(e) => return finish(moves, loss_for(stm), &format!("engine failure: {e}")),
        };
        if let TimeControl::Fischer { inc, .. } = tc {
            let c = &mut clocks[stm as usize];
            *c -= reply.elapsed.as_millis() as i64;
            if *c < -(adj.time_margin as i64) {
                return finish(moves, loss_for(stm), "loses on time");
            }
            *c = (*c).max(0) + inc as i64;
        }
        let Some(m) = pos.parse_uci_move(&reply.bestmove) else {
            return finish(moves, loss_for(stm), &format!("illegal move {}", reply.bestmove));
        };
        pos.play(m);
        hashes.push(pos.hash());
        moves.push(m.to_uci());

        // Adjudication on the engines' own evaluations.
        let me = stm as usize;
        if let Some(score) = reply.score {
            last_score[me] = score;
            if score <= -adj.resign_score && last_score[1 - me] >= adj.resign_score {
                resign_count[me] += 1;
            } else {
                resign_count[me] = 0;
            }
            if resign_count[me] >= adj.resign_moves {
                return finish(moves, loss_for(stm), "adjudicated: resignation");
            }
            let full_moves = moves.len() as u32 / 2;
            if full_moves >= adj.draw_after && score.abs() <= adj.draw_score && last_score[1 - me].abs() <= adj.draw_score {
                draw_count += 1;
            } else {
                draw_count = 0;
            }
            if draw_count >= 2 * adj.draw_moves {
                return finish(moves, Outcome::Draw, "adjudicated: draw");
            }
        }
    }
}
