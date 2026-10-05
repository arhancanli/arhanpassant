use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

struct Engine {
    child: Child,
    input: ChildStdin,
    lines: Receiver<String>,
}

impl Engine {
    fn new() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_arhanpassant"))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
        let input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                if tx.send(line.unwrap()).is_err() { break; }
            }
        });
        let mut engine = Self { child, input, lines };
        engine.send("uci\nisready");
        engine.until("readyok", Duration::from_secs(5));
        engine
    }

    fn send(&mut self, commands: &str) {
        writeln!(self.input, "{commands}").unwrap();
        self.input.flush().unwrap();
    }

    fn until(&self, prefix: &str, timeout: Duration) -> String {
        let end = Instant::now() + timeout;
        loop {
            let line = self.lines.recv_timeout(end.saturating_duration_since(Instant::now()))
                .unwrap_or_else(|e| panic!("waiting for {prefix}: {e}"));
            if line.starts_with(prefix) { return line; }
        }
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn immediate_stop_does_not_get_lost_when_the_search_starts() {
    let mut engine = Engine::new();
    engine.send("setoption name Threads value 4");
    for _ in 0..20 {
        engine.send("position startpos\ngo infinite\nstop\nisready");
        let best = engine.until("bestmove ", Duration::from_secs(3));
        assert_ne!(best, "bestmove 0000");
        engine.until("readyok", Duration::from_secs(3));
    }
}

#[test]
fn pondering_uses_its_move_budget_after_ponderhit() {
    let mut engine = Engine::new();
    engine.send("setoption name Move Overhead value 0\nposition startpos\ngo ponder movetime 200");
    engine.until("info depth ", Duration::from_secs(3));
    std::thread::sleep(Duration::from_millis(350));
    while let Ok(line) = engine.lines.try_recv() {
        assert!(!line.starts_with("bestmove "), "answer before ponderhit");
    }
    let hit = Instant::now();
    engine.send("ponderhit");
    engine.until("bestmove ", Duration::from_secs(3));
    assert!(hit.elapsed() >= Duration::from_millis(120), "ponder time consumed the move budget");
}

#[test]
fn immediate_ponderhit_does_not_get_lost_when_the_search_starts() {
    let mut engine = Engine::new();
    for _ in 0..10 {
        engine.send("position startpos\ngo ponder depth 2\nponderhit");
        engine.until("bestmove ", Duration::from_secs(3));
    }
}
