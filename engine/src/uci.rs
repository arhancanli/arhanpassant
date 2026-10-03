//! Universal Chess Interface front end.

use crate::bench;
use crate::movegen::perft_divide;
use crate::nnue::Network;
use crate::params;
use crate::position::{Position, START_FEN};
use crate::search::{search_threads, Limits, SearchInfo, Searcher, Shared};
use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;

pub const NAME: &str = "ArhanPassant";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub fn info_line(info: &SearchInfo) -> String {
    let nps = info.nodes * 1000 / info.time_ms.max(1);
    let pv: Vec<String> = info.pv.iter().map(|m| m.to_uci()).collect();
    format!(
        "info depth {} seldepth {} score {} nodes {} nps {} hashfull {} time {} pv {}",
        info.depth,
        info.seldepth,
        info.score_string(),
        info.nodes,
        nps,
        info.hashfull,
        info.time_ms,
        pv.join(" ")
    )
}

struct Running {
    handle: JoinHandle<Vec<Searcher>>,
    gui_stop: Arc<AtomicBool>,
}

pub struct Uci {
    pos: Position,
    history: Vec<u64>,
    searchers: Vec<Searcher>,
    running: Option<Running>,
    hash_mb: usize,
    threads: usize,
    move_overhead: u64,
    eval_file: String,
    network: Option<Arc<Network>>,
    shared_handle: Option<Arc<Shared>>,
}

impl Default for Uci {
    fn default() -> Self {
        Uci::new()
    }
}

impl Uci {
    pub fn new() -> Uci {
        let network = Network::embedded().map(Arc::new);
        let mut u = Uci {
            pos: Position::startpos(),
            history: Vec::new(),
            searchers: Vec::new(),
            running: None,
            hash_mb: 16,
            threads: 1,
            move_overhead: 30,
            eval_file: if network.is_some() { "<embedded>".into() } else { "<none>".into() },
            network,
            shared_handle: None,
        };
        u.history.push(u.pos.hash());
        u.rebuild();
        u
    }

    fn rebuild(&mut self) {
        let shared = Shared::new(self.hash_mb, self.network.clone());
        self.searchers = (0..self.threads).map(|_| Searcher::new(shared.clone())).collect();
    }

    fn wait(&mut self) {
        if let Some(r) = self.running.take() {
            self.searchers = r.handle.join().expect("search thread panicked");
        }
    }

    fn stop(&mut self) {
        if let Some(r) = &self.running {
            r.gui_stop.store(true, Ordering::Relaxed);
            if let Some(s) = self.searchers_shared() {
                s.stop.store(true, Ordering::Relaxed);
            }
        }
        self.wait();
    }

    fn searchers_shared(&self) -> Option<Arc<Shared>> {
        self.searchers.first().map(|s| s.shared.clone()).or_else(|| self.shared_handle.clone())
    }

    pub fn run(&mut self) {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else { break };
            if !self.command(line.trim()) {
                break;
            }
        }
        self.stop();
    }

    /// Handle one command; returns false on `quit`.
    pub fn command(&mut self, line: &str) -> bool {
        let tokens: Vec<&str> = line.split_whitespace().collect();
        let Some(&cmd) = tokens.first() else { return true };
        match cmd {
            "uci" => {
                println!("id name {NAME} {VERSION}");
                println!("id author Arhan Canli");
                println!("option name Hash type spin default 16 min 1 max 65536");
                println!("option name Threads type spin default 1 min 1 max 512");
                println!("option name Move Overhead type spin default 30 min 0 max 5000");
                println!("option name EvalFile type string default {}", self.eval_file);
                println!("option name Clear Hash type button");
                println!("option name Ponder type check default false");
                println!("option name SyzygyPath type string default <empty>");
                println!("option name SyzygyProbeLimit type spin default 7 min 0 max 7");
                println!("uciok");
            }
            "isready" => println!("readyok"),
            "ucinewgame" => {
                self.stop();
                for s in &mut self.searchers {
                    s.clear();
                }
                if let Some(s) = self.searchers.first() {
                    s.shared.tt.clear();
                }
            }
            "setoption" => {
                self.stop();
                self.set_option(line);
            }
            "position" => {
                self.stop();
                if let Err(e) = self.set_position(&tokens[1..]) {
                    println!("info string error: {e}");
                }
            }
            "go" => {
                self.stop();
                self.go(&tokens[1..]);
            }
            "stop" => self.stop(),
            // The opponent played the move we were pondering on: the search continues under its time limits.
            "ponderhit" => {
                if let Some(s) = self.searchers_shared() {
                    s.pondering.store(false, Ordering::Relaxed);
                }
            }
            "quit" => return false,
            "d" => println!("{:?}", self.pos),
            "eval" => {
                let hce = crate::eval::evaluate(&self.pos);
                println!("hce {hce}");
                if let Some(n) = &self.network {
                    println!("nnue {}", n.evaluate_full(&self.pos));
                }
            }
            "bench" => {
                let depth = tokens.get(1).and_then(|d| d.parse().ok()).unwrap_or(bench::DEFAULT_DEPTH);
                bench::run(depth, self.network.clone());
            }
            "tb" => {
                // Tablebase verdict for the current position, and the root moves kept.
                let wdl = crate::syzygy::probe_wdl(&self.pos);
                let root = crate::syzygy::root_moves(&self.pos);
                let moves = root.as_ref().map(|(_, m)| m.iter().map(|m| m.to_string()).collect::<Vec<_>>().join(" "));
                println!("tb wdl {wdl:?} root {:?} moves {}", root.map(|(w, _)| w), moves.unwrap_or_default());
            }
            "tunables" => {
                for (name, def, min, max) in params::ALL {
                    let step = ((max - min) as f64 / 20.0).max(0.5);
                    println!("{name}, int, {def}, {min}, {max}, {step:.1}, 0.002");
                }
            }
            _ => println!("info string unknown command: {cmd}"),
        }
        io::stdout().flush().ok();
        true
    }

    fn set_option(&mut self, line: &str) {
        // setoption name <name with spaces> [value <value>]
        let rest = line.trim_start_matches("setoption").trim();
        let rest = rest.strip_prefix("name").unwrap_or(rest).trim();
        let (name, value) = match rest.find(" value ") {
            Some(i) => (rest[..i].trim(), rest[i + 7..].trim()),
            None => (rest, ""),
        };
        match name.to_ascii_lowercase().as_str() {
            "hash" => {
                if let Ok(v) = value.parse::<usize>() {
                    self.hash_mb = v.clamp(1, 65536);
                    self.rebuild();
                }
            }
            "threads" => {
                if let Ok(v) = value.parse::<usize>() {
                    self.threads = v.clamp(1, 512);
                    self.rebuild();
                }
            }
            "move overhead" => {
                if let Ok(v) = value.parse() {
                    self.move_overhead = v;
                }
            }
            "evalfile" => {
                if value == "<embedded>" || value.is_empty() {
                    self.network = Network::embedded().map(Arc::new);
                } else if value == "<none>" {
                    self.network = None;
                } else {
                    match Network::load(value) {
                        Ok(n) => self.network = Some(Arc::new(n)),
                        Err(e) => {
                            println!("info string error: {e}");
                            return;
                        }
                    }
                }
                self.eval_file = value.to_string();
                self.rebuild();
            }
            "syzygypath" => match crate::syzygy::init(value) {
                Ok(0) => println!("info string tablebases unloaded"),
                Ok(n) => println!("info string tablebases loaded, up to {n} pieces"),
                Err(e) => println!("info string error: tablebases not loaded ({e})"),
            },
            "syzygyprobelimit" => {
                if let Ok(v) = value.parse::<u32>() {
                    crate::syzygy::set_probe_limit(v);
                }
            }
            // Pondering needs no setting: the GUI decides with `go ponder`.
            "ponder" => {}
            "clear hash" => {
                if let Some(s) = self.searchers.first() {
                    s.shared.tt.clear();
                }
            }
            _ => {
                let ok = value.parse::<i32>().map(|v| params::set(name, v)).unwrap_or(false);
                if !ok {
                    println!("info string unknown option or bad value: {name}");
                }
            }
        }
    }

    fn set_position(&mut self, t: &[&str]) -> Result<(), String> {
        let moves_at = t.iter().position(|&x| x == "moves");
        let spec = &t[..moves_at.unwrap_or(t.len())];
        let mut pos = match spec.first() {
            Some(&"startpos") => Position::from_fen(START_FEN).unwrap(),
            Some(&"fen") => Position::from_fen(&spec[1..].join(" ")).map_err(|e| e.to_string())?,
            _ => return Err("expected startpos or fen".into()),
        };
        let mut history = vec![pos.hash()];
        if let Some(i) = moves_at {
            for s in &t[i + 1..] {
                let m = pos.parse_uci_move(s).ok_or_else(|| format!("illegal move {s}"))?;
                pos.play(m);
                history.push(pos.hash());
            }
        }
        self.pos = pos;
        self.history = history;
        Ok(())
    }

    fn go(&mut self, t: &[&str]) {
        let mut limits = Limits::default();
        let mut i = 0;
        let num = |i: usize| t.get(i + 1).and_then(|v| v.parse::<i64>().ok()).map(|v| v.max(0) as u64);
        while i < t.len() {
            match t[i] {
                "infinite" => limits.infinite = true,
                "ponder" => limits.ponder = true,
                "depth" => limits.depth = num(i).map(|v| v as i32),
                "nodes" => limits.nodes = num(i),
                "softnodes" => limits.soft_nodes = num(i),
                "movetime" => limits.movetime = num(i),
                "wtime" => limits.wtime = num(i),
                "btime" => limits.btime = num(i),
                "winc" => limits.winc = num(i),
                "binc" => limits.binc = num(i),
                "movestogo" => limits.movestogo = num(i),
                "perft" => {
                    let depth = num(i).unwrap_or(1) as u32;
                    let start = crate::time::Instant::now();
                    let mut total = 0;
                    for (m, n) in perft_divide(&self.pos, depth) {
                        println!("{m}: {n}");
                        total += n;
                    }
                    let ms = start.elapsed().as_millis().max(1) as u64;
                    println!("\nNodes searched: {total}\ntime {ms} ms, {} nps", total * 1000 / ms);
                    return;
                }
                _ => {}
            }
            i += 1;
        }
        let pos = self.pos;
        let history = self.history.clone();
        let overhead = self.move_overhead;
        let mut searchers = std::mem::take(&mut self.searchers);
        self.shared_handle = searchers.first().map(|s| s.shared.clone());
        let gui_stop = Arc::new(AtomicBool::new(false));
        let gs = gui_stop.clone();
        let handle = std::thread::Builder::new()
            .stack_size(64 << 20)
            .spawn(move || {
                let mut report = |info: &SearchInfo| {
                    println!("{}", info_line(info));
                };
                let result = search_threads(&mut searchers, &pos, &history, &limits, overhead, &mut report);
                // For `go infinite`, hold the answer until the GUI says stop; for `go ponder`,
                // until `ponderhit` or `stop` (a ponder search can finish early, e.g. on a mate).
                let shared = searchers[0].shared.clone();
                while (limits.infinite || shared.pondering.load(Ordering::Relaxed)) && !gs.load(Ordering::Relaxed) {
                    std::thread::sleep(std::time::Duration::from_millis(2));
                }
                // Name the expected reply so the GUI can ponder on it.
                match result.pv.get(1) {
                    Some(reply) if result.pv[0] == result.best_move => {
                        println!("bestmove {} ponder {}", result.best_move.to_uci(), reply.to_uci())
                    }
                    _ => println!("bestmove {}", result.best_move.to_uci()),
                }
                io::stdout().flush().ok();
                searchers
            })
            .expect("spawn search thread");
        self.running = Some(Running { handle, gui_stop });
    }
}
