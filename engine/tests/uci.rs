use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};
use arhanpassant::Position;

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

#[test]
fn smp_vote_returns_a_legal_move_and_matching_legal_pv() {
    let mut engine = Engine::new();
    engine.send("setoption name Threads value 3\nsetoption name smp_vote value 1");
    for fen in [
        arhanpassant::START_FEN,
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
    ] {
        engine.send(&format!("position fen {fen}\ngo nodes 60000"));
        let mut last_pv = Vec::new();
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            let line = engine.lines.recv_timeout(end.saturating_duration_since(Instant::now())).unwrap();
            if let Some((_, pv)) = line.split_once(" pv ") {
                last_pv = pv.split_whitespace().map(str::to_owned).collect();
            }
            if line.starts_with("bestmove ") {
                let best = line.split_whitespace().nth(1).unwrap();
                let mut pos = Position::from_fen(fen).unwrap();
                assert!(pos.legal_moves().iter().any(|m| m.to_uci() == best));
                assert_eq!(last_pv.first().map(String::as_str), Some(best));
                for uci in &last_pv {
                    let m = pos.legal_moves().iter().find(|m| m.to_uci() == *uci)
                        .unwrap_or_else(|| panic!("illegal PV move {uci} in {line}")).to_owned();
                    pos.play(m);
                }
                break;
            }
        }
    }
}

#[test]
fn quiescence_checks_obey_node_limits_and_stop_in_forcing_positions() {
    let mut engine = Engine::new();
    for (checks, sacrifices) in [(1, 0), (2, 1)] {
        engine.send(&format!("setoption name qs_checks value {checks}\nsetoption name qs_check_see value {sacrifices}"));
        for fen in [
            "6rk/2R2Qp1/pPNq1n1p/P3p3/7P/6P1/5P2/r4BK1 w - - 1 31",
            "r2qnrk1/pb2bppp/1p2p3/n2pP3/3p1B1P/P1NB1Q1N/1PP2PP1/2KR3R b - - 1 6",
            "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1",
        ] {
            engine.send(&format!("ucinewgame\nposition fen {fen}\ngo nodes 16000"));
            let end = Instant::now() + Duration::from_secs(5);
            let mut reported = false;
            loop {
                let line = engine.lines.recv_timeout(end.saturating_duration_since(Instant::now())).unwrap();
                assert!(!line.contains("unknown option") && !line.contains("error:"), "{line}");
                if let Some((_, pv)) = line.split_once(" pv ") {
                    reported = true;
                    let mut pos = Position::from_fen(fen).unwrap();
                    for uci in pv.split_whitespace() {
                        let m = pos.parse_uci_move(uci).unwrap_or_else(|| panic!("illegal PV move {uci}"));
                        pos.play(m);
                    }
                    let words: Vec<_> = line.split_whitespace().collect();
                    let i = words.iter().position(|w| *w == "nodes").unwrap();
                    assert!(words[i + 1].parse::<u64>().unwrap() <= 16000);
                }
                if let Some(best) = line.strip_prefix("bestmove ") {
                    assert!(reported);
                    assert!(Position::from_fen(fen).unwrap().parse_uci_move(best.split_whitespace().next().unwrap()).is_some());
                    break;
                }
            }
            engine.send("go infinite");
            engine.until("info depth ", Duration::from_secs(3));
            engine.send("stop");
            engine.until("bestmove ", Duration::from_secs(3));
        }
    }
}
