//! A UCI engine running as a child process.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct EngineSpec {
    pub name: String,
    pub cmd: String,
    pub options: Vec<(String, String)>,
    /// Per-engine time control (for odds matches); None uses the match default.
    pub tc: Option<crate::game::TimeControl>,
}

impl EngineSpec {
    /// Parse `name=X cmd=PATH opt.Hash=16 opt.EvalFile=net.nnue`.
    pub fn parse(words: &[String]) -> Result<EngineSpec, String> {
        let mut spec = EngineSpec { name: String::new(), cmd: String::new(), options: Vec::new(), tc: None };
        for w in words {
            let (k, v) = w.split_once('=').ok_or_else(|| format!("expected key=value, got {w}"))?;
            match k {
                "name" => spec.name = v.to_string(),
                "cmd" => spec.cmd = v.to_string(),
                "tc" => spec.tc = Some(crate::game::TimeControl::parse(v)?),
                _ if k.starts_with("opt.") => spec.options.push((k[4..].to_string(), v.to_string())),
                _ => return Err(format!("unknown engine key {k}")),
            }
        }
        if spec.cmd.is_empty() {
            return Err("engine needs cmd=".into());
        }
        if spec.name.is_empty() {
            spec.name = spec.cmd.clone();
        }
        Ok(spec)
    }
}

pub struct Engine {
    pub spec: EngineSpec,
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<String>,
}

#[derive(Debug)]
pub struct MoveReply {
    pub bestmove: String,
    /// Score from the engine's point of view: centipawns, or +-(100000 - plies) for mates.
    pub score: Option<i32>,
    pub elapsed: Duration,
}

impl Engine {
    pub fn start(spec: &EngineSpec) -> Result<Engine, String> {
        let mut child = Command::new(&spec.cmd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("{}: {e}", spec.cmd))?;
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(l) => {
                        if tx.send(l).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        let mut e = Engine { spec: spec.clone(), child, stdin, rx };
        e.send("uci")?;
        e.wait_for(|l| l == "uciok", Duration::from_secs(10))?;
        for (k, v) in spec.options.clone() {
            e.send(&format!("setoption name {k} value {v}"))?;
        }
        e.sync()?;
        Ok(e)
    }

    pub fn send(&mut self, line: &str) -> Result<(), String> {
        writeln!(self.stdin, "{line}").and_then(|_| self.stdin.flush()).map_err(|e| format!("{}: write failed: {e}", self.spec.name))
    }

    /// Wait for a line matching `pred`; returns it with every line before it.
    fn wait_for(&mut self, pred: impl Fn(&str) -> bool, timeout: Duration) -> Result<(String, Vec<String>), String> {
        let deadline = Instant::now() + timeout;
        let mut seen = Vec::new();
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match self.rx.recv_timeout(left) {
                Ok(l) => {
                    if pred(&l) {
                        return Ok((l, seen));
                    }
                    seen.push(l);
                }
                Err(RecvTimeoutError::Timeout) => return Err(format!("{}: timed out", self.spec.name)),
                Err(RecvTimeoutError::Disconnected) => return Err(format!("{}: exited", self.spec.name)),
            }
        }
    }

    pub fn sync(&mut self) -> Result<(), String> {
        self.send("isready")?;
        self.wait_for(|l| l == "readyok", Duration::from_secs(30)).map(|_| ())
    }

    pub fn new_game(&mut self) -> Result<(), String> {
        self.send("ucinewgame")?;
        self.sync()
    }

    pub fn go(&mut self, position: &str, go: &str, timeout: Duration) -> Result<MoveReply, String> {
        self.send(position)?;
        let start = Instant::now();
        self.send(go)?;
        let (line, info) = self.wait_for(|l| l.starts_with("bestmove"), timeout)?;
        let elapsed = start.elapsed();
        let bestmove = line.split_whitespace().nth(1).unwrap_or("").to_string();
        let score = info.iter().rev().find_map(|l| parse_score(l));
        Ok(MoveReply { bestmove, score, elapsed })
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        let _ = writeln!(self.stdin, "quit");
        let _ = self.stdin.flush();
        let deadline = Instant::now() + Duration::from_millis(500);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn parse_score(line: &str) -> Option<i32> {
    if !line.starts_with("info") {
        return None;
    }
    let t: Vec<&str> = line.split_whitespace().collect();
    let i = t.iter().position(|&x| x == "score")?;
    if t.get(i + 3).is_some_and(|&b| b == "lowerbound" || b == "upperbound") {
        return None;
    }
    let v: i32 = t.get(i + 2)?.parse().ok()?;
    match *t.get(i + 1)? {
        "cp" => Some(v),
        "mate" => Some(if v > 0 { 100_000 - v } else { -100_000 - v }),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::parse_score;

    #[test]
    fn scores() {
        assert_eq!(parse_score("info depth 5 score cp -37 nodes 10 pv e2e4"), Some(-37));
        assert_eq!(parse_score("info depth 9 score mate 3 pv a1a8"), Some(99_997));
        assert_eq!(parse_score("info depth 9 score mate -2 pv a1a8"), Some(-99_998));
        assert_eq!(parse_score("info string hello"), None);
    }
}
