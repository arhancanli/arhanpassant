//! WebAssembly interface: text commands in, JSON out, through linear memory.
//!
//! Host protocol: `ap_alloc(n)` returns a buffer for an n-byte UTF-8 command;
//! `ap_call(ptr, n)` runs it and returns the byte length of the JSON reply at
//! `ap_out_ptr()`; `ap_dealloc(ptr, n)` frees the command buffer. The host must
//! provide `env.ap_now_ms() -> f64` (for example `performance.now()`).
//!
//! Commands (tab-separated):
//! - `state <fen|startpos> <uci moves>`: position after the moves, legal moves with SAN, game status
//! - `search <fen|startpos> <uci moves> <nodes> <movetime ms> <depth>`: best move (0 = no limit)
//! - `rank <fen|startpos> <uci moves> <nodes per move>`: every legal move scored by a short search
//! - `sanline <fen|startpos> <san moves>`: convert SAN moves to UCI, stopping at the first illegal one
//! - `newgame`: clear the hash table and history
//! - `version`

use arhanpassant::game::{json_error as error, json_list as list, json_str as esc, replay, state_json as state};
use arhanpassant::nnue::Network;
use arhanpassant::search::{Limits, Searcher, Shared, MATE_IN_MAX};
use arhanpassant::{Move, Position};
use std::cell::RefCell;
use std::sync::atomic::Ordering;
use std::sync::Arc;

thread_local! {
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    static ENGINE: RefCell<Option<Searcher>> = const { RefCell::new(None) };
}

#[no_mangle]
pub extern "C" fn ap_alloc(len: usize) -> *mut u8 {
    let mut v = Vec::<u8>::with_capacity(len.max(1));
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `ptr`/`cap` must come from `ap_alloc`.
#[no_mangle]
pub unsafe extern "C" fn ap_dealloc(ptr: *mut u8, cap: usize) {
    drop(Vec::from_raw_parts(ptr, 0, cap.max(1)));
}

/// # Safety
/// `ptr` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn ap_call(ptr: *const u8, len: usize) -> usize {
    let bytes = std::slice::from_raw_parts(ptr, len);
    let reply = match std::str::from_utf8(bytes) {
        Ok(cmd) => dispatch(cmd),
        Err(_) => error("command is not UTF-8"),
    };
    OUT.with(|o| {
        let mut o = o.borrow_mut();
        o.clear();
        o.extend_from_slice(reply.as_bytes());
        o.len()
    })
}

#[no_mangle]
pub extern "C" fn ap_out_ptr() -> *const u8 {
    OUT.with(|o| o.borrow().as_ptr())
}

fn with_engine<R>(f: impl FnOnce(&mut Searcher) -> R) -> R {
    ENGINE.with(|e| {
        let mut e = e.borrow_mut();
        let s = e.get_or_insert_with(|| {
            let mut s = Searcher::new(Shared::new(16, Network::embedded().map(Arc::new)));
            s.verbose = false;
            s
        });
        f(s)
    })
}

fn score_json(score: i32) -> String {
    if score >= MATE_IN_MAX {
        format!("{{\"mate\":{}}}", (arhanpassant::search::MATE - score + 1) / 2)
    } else if score <= -MATE_IN_MAX {
        format!("{{\"mate\":-{}}}", (arhanpassant::search::MATE + score) / 2)
    } else {
        format!("{{\"cp\":{score}}}")
    }
}

fn run_search(s: &mut Searcher, pos: &Position, hashes: &[u64], limits: &Limits) -> arhanpassant::search::SearchResult {
    s.shared.stop.store(false, Ordering::Relaxed);
    s.shared.tt.new_search();
    s.go(pos, hashes, limits, true, 0, &mut |_| {})
}

fn search(fen: &str, moves: &str, nodes: u64, movetime: u64, depth: i32) -> String {
    let (pos, hashes, _) = match replay(fen, moves) {
        Ok(r) => r,
        Err(e) => return error(&e),
    };
    if pos.legal_moves().is_empty() {
        return error("no legal moves");
    }
    let limits = Limits {
        nodes: (nodes > 0).then_some(nodes),
        movetime: (movetime > 0).then_some(movetime),
        depth: (depth > 0).then_some(depth),
        ..Default::default()
    };
    let r = with_engine(|s| run_search(s, &pos, &hashes, &limits));
    let mut p = pos;
    let mut pv_san = Vec::new();
    for &m in &r.pv {
        if !p.is_legal(m) {
            break;
        }
        pv_san.push(p.san(m));
        p.play(m);
    }
    format!(
        "{{\"bestmove\":{},\"san\":{},\"score\":{},\"depth\":{},\"nodes\":{},\"pv\":{},\"pvSan\":{}}}",
        esc(&r.best_move.to_uci()),
        esc(&pos.san(r.best_move)),
        score_json(r.score),
        r.depth,
        r.nodes,
        list(r.pv.iter().map(|m| esc(&m.to_uci()))),
        list(pv_san.iter().map(|s| esc(s)))
    )
}

fn rank(fen: &str, moves: &str, nodes: u64) -> String {
    let (pos, hashes, _) = match replay(fen, moves) {
        Ok(r) => r,
        Err(e) => return error(&e),
    };
    let legal: Vec<Move> = pos.legal_moves().iter().collect();
    let mut scored: Vec<(Move, i32)> = with_engine(|s| {
        legal
            .iter()
            .map(|&m| {
                let child = pos.after(m);
                let mut h = hashes.clone();
                h.push(child.hash());
                let score = if child.legal_moves().is_empty() {
                    if child.in_check() { arhanpassant::search::MATE - 1 } else { 0 }
                } else {
                    let limits = Limits { nodes: Some(nodes.max(1)), ..Default::default() };
                    -run_search(s, &child, &h, &limits).score
                };
                (m, score)
            })
            .collect()
    });
    scored.sort_by_key(|&(_, s)| -s);
    list(scored.iter().map(|(m, s)| format!("[{},{},{}]", esc(&m.to_uci()), esc(&pos.san(*m)), s)))
}

fn sanline(fen: &str, sans: &str) -> String {
    let mut pos = if fen == "startpos" || fen.is_empty() {
        Position::startpos()
    } else {
        match Position::from_fen(fen) {
            Ok(p) => p,
            Err(e) => return error(&e.to_string()),
        }
    };
    let mut ucis = Vec::new();
    let mut bad = None;
    for (i, san) in sans.split_whitespace().enumerate() {
        match pos.parse_san(san) {
            Some(m) => {
                ucis.push(m.to_uci());
                pos.play(m);
            }
            None => {
                bad = Some(format!("move {} ({san}) is not legal", i + 1));
                break;
            }
        }
    }
    format!(
        "{{\"uci\":{},\"error\":{}}}",
        list(ucis.iter().map(|u| esc(u))),
        bad.map_or("null".to_string(), |b| esc(&b))
    )
}

fn dispatch(cmd: &str) -> String {
    let f: Vec<&str> = cmd.split('\t').collect();
    let num = |i: usize| f.get(i).and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0);
    match f[0] {
        "state" => state(f.get(1).copied().unwrap_or("startpos"), f.get(2).copied().unwrap_or("")),
        "search" => search(f.get(1).copied().unwrap_or("startpos"), f.get(2).copied().unwrap_or(""), num(3), num(4), num(5) as i32),
        "rank" => rank(f.get(1).copied().unwrap_or("startpos"), f.get(2).copied().unwrap_or(""), num(3)),
        "sanline" => sanline(f.get(1).copied().unwrap_or("startpos"), f.get(2).copied().unwrap_or("")),
        "newgame" => {
            with_engine(|s| {
                s.clear();
                s.shared.tt.clear();
            });
            "{\"ok\":true}".to_string()
        }
        "version" => format!(
            "{{\"name\":\"ArhanPassant\",\"version\":{},\"nnue\":{}}}",
            esc(arhanpassant::uci::VERSION),
            Network::embedded().is_some()
        ),
        other => error(&format!("unknown command {other}")),
    }
}
