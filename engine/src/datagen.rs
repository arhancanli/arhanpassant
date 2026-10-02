//! Self-play data generation for training evaluation networks.
//!
//! Each record is 32 bytes:
//! `occupancy u64 | 32 x 4-bit pieces (colour << 3 | type, in square order) |
//! score i16 (White's view, centipawns) | result u8 (0 Black win, 1 draw,
//! 2 White win) | side to move u8 | halfmove u8 | fullmove u16 | reserved u8`.

use crate::bitboard::squares;
use crate::nnue::Network;
use crate::position::Position;
use crate::search::{Limits, Searcher, Shared, MATE_IN_MAX};
use crate::types::*;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use crate::time::Instant;
use std::time::Duration;

pub const RECORD_SIZE: usize = 32;

#[derive(Clone)]
pub struct Config {
    pub threads: usize,
    /// Total games to play; 0 means no limit.
    pub games: u64,
    pub soft_nodes: u64,
    pub hard_nodes: u64,
    pub random_plies: usize,
    pub out_dir: PathBuf,
    pub seed: u64,
    pub hash_mb: usize,
    pub network: Option<Arc<Network>>,
    /// Stop after this long; None means no limit.
    pub duration: Option<Duration>,
}

pub fn encode(pos: &Position, score_white: i16, result: u8) -> [u8; RECORD_SIZE] {
    let mut r = [0u8; RECORD_SIZE];
    let occ = pos.occupied();
    r[0..8].copy_from_slice(&occ.to_le_bytes());
    for (i, sq) in squares(occ).enumerate() {
        let p = pos.piece_on(sq);
        let code = ((p.color() as u8) << 3) | p.piece_type() as u8;
        r[8 + i / 2] |= code << (4 * (i % 2));
    }
    r[24..26].copy_from_slice(&score_white.to_le_bytes());
    r[26] = result;
    r[27] = pos.side_to_move() as u8;
    r[28] = pos.halfmove_clock().min(255) as u8;
    r[29..31].copy_from_slice(&pos.fullmove_number().to_le_bytes());
    r
}

/// Piece placement (FEN first field), score, result and side to move of a record.
pub fn decode(r: &[u8; RECORD_SIZE]) -> (String, i16, u8, Color) {
    let occ = u64::from_le_bytes(r[0..8].try_into().unwrap());
    let mut board = [None; 64];
    for (i, sq) in squares(occ).enumerate() {
        let code = (r[8 + i / 2] >> (4 * (i % 2))) & 0xF;
        let color = if code & 8 != 0 { Color::Black } else { Color::White };
        board[sq as usize] = Some(Piece::new(color, PieceType::from_idx((code & 7) as usize)));
    }
    let mut fen = String::new();
    for rank in (0..8).rev() {
        let mut empty = 0;
        for file in 0..8 {
            match board[rank * 8 + file] {
                Some(p) => {
                    if empty > 0 {
                        fen.push_str(&empty.to_string());
                        empty = 0;
                    }
                    fen.push(p.to_char());
                }
                None => empty += 1,
            }
        }
        if empty > 0 {
            fen.push_str(&empty.to_string());
        }
        if rank > 0 {
            fen.push('/');
        }
    }
    let score = i16::from_le_bytes([r[24], r[25]]);
    (fen, score, r[26], Color::from_idx(r[27] as usize))
}

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

fn is_threefold(hashes: &[u64], halfmove: usize) -> bool {
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

/// Play one self-play game; returns the recorded positions or None when the
/// random opening was unusable.
fn play_game(s: &mut Searcher, rng: &mut Rng, cfg: &Config) -> Option<Vec<[u8; RECORD_SIZE]>> {
    let mut pos = Position::startpos();
    let mut hashes = vec![pos.hash()];
    let plies = cfg.random_plies + (rng.next() % 2) as usize;
    for _ in 0..plies {
        let moves = pos.legal_moves();
        if moves.is_empty() {
            return None;
        }
        pos.play(moves[(rng.next() % moves.len() as u64) as usize]);
        hashes.push(pos.hash());
    }
    if pos.legal_moves().is_empty() {
        return None;
    }
    s.clear();
    s.shared.tt.clear();
    let limits = Limits { soft_nodes: Some(cfg.soft_nodes), nodes: Some(cfg.hard_nodes), ..Default::default() };

    let mut samples: Vec<(Position, i16)> = Vec::with_capacity(256);
    let mut win_streak = 0;
    let mut draw_streak = 0;
    let mut opening_checked = false;
    let result: u8 = loop {
        let legal = pos.legal_moves();
        if legal.is_empty() {
            break if !pos.in_check() {
                1
            } else if pos.side_to_move() == Color::White {
                0
            } else {
                2
            };
        }
        if pos.halfmove_clock() >= 100 || pos.is_insufficient_material() || is_threefold(&hashes, pos.halfmove_clock() as usize) {
            break 1;
        }
        if hashes.len() > 800 {
            break 1;
        }
        s.shared.tt.new_search();
        s.shared.stop.store(false, Ordering::Relaxed);
        let r = s.go(&pos, &hashes, &limits, true, 0, &mut |_| {});
        if r.best_move.is_null() {
            return None;
        }
        let score = r.score;
        let white = if pos.side_to_move() == Color::White { score } else { -score };
        if !opening_checked {
            opening_checked = true;
            if score.abs() > 1000 {
                return None;
            }
        }
        if white.abs() >= 2500 {
            win_streak += 1;
            if win_streak >= 4 {
                break if white > 0 { 2 } else { 0 };
            }
        } else {
            win_streak = 0;
        }
        if pos.fullmove_number() >= 40 && white.abs() <= 8 {
            draw_streak += 1;
            if draw_streak >= 12 {
                break 1;
            }
        } else {
            draw_streak = 0;
        }
        if !pos.in_check() && r.best_move.is_quiet() && score.abs() < MATE_IN_MAX {
            samples.push((pos, white.clamp(-32000, 32000) as i16));
        }
        pos.play(r.best_move);
        hashes.push(pos.hash());
    };
    Some(samples.iter().map(|(p, sc)| encode(p, *sc, result)).collect())
}

/// Turn finished games played elsewhere (Lichess, gauntlets) into training
/// records. Each line is one game from the standard position:
/// `<result> <moves...>` with result 1-0, 0-1 or 1/2-1/2 and moves in SAN or
/// UCI. Every position is scored by a search of `soft_nodes`, and kept on the
/// same terms as self-play: past the first `skip_plies`, not in check, a quiet
/// best move, and no mate score. Returns the records, the games used and the
/// lines that could not be read.
pub fn rescore(lines: &[String], soft_nodes: u64, threads: usize, hash_mb: usize, network: Option<Arc<Network>>,
               skip_plies: usize) -> (Vec<[u8; RECORD_SIZE]>, usize, usize) {
    let threads = threads.max(1);
    let chunks: Vec<Vec<String>> = (0..threads).map(|t| lines.iter().skip(t).step_by(threads).cloned().collect()).collect();
    let handles: Vec<_> = chunks
        .into_iter()
        .map(|chunk| {
            let network = network.clone();
            std::thread::Builder::new()
                .stack_size(64 << 20)
                .spawn(move || {
                    let mut s = Searcher::new(Shared::new(hash_mb, network));
                    s.verbose = false;
                    let limits = Limits { soft_nodes: Some(soft_nodes), nodes: Some(soft_nodes * 20), ..Default::default() };
                    let (mut out, mut used, mut bad) = (Vec::new(), 0usize, 0usize);
                    for line in &chunk {
                        let mut words = line.split_whitespace();
                        let result = match words.next() {
                            Some("1-0") => 2u8,
                            Some("0-1") => 0,
                            Some("1/2-1/2") => 1,
                            _ => {
                                bad += 1;
                                continue;
                            }
                        };
                        let mut pos = Position::startpos();
                        let mut positions = vec![(pos, vec![pos.hash()])];
                        let mut ok = true;
                        for w in words {
                            let Some(m) = pos.parse_uci_move(w).or_else(|| pos.parse_san(w)) else {
                                ok = false;
                                break;
                            };
                            pos.play(m);
                            let mut h = positions.last().unwrap().1.clone();
                            h.push(pos.hash());
                            positions.push((pos, h));
                        }
                        if !ok {
                            bad += 1;
                            continue;
                        }
                        used += 1;
                        s.clear();
                        s.shared.tt.clear();
                        for (p, hashes) in positions.iter().skip(skip_plies) {
                            if p.in_check() || p.legal_moves().is_empty() {
                                continue;
                            }
                            s.shared.tt.new_search();
                            s.shared.stop.store(false, Ordering::Relaxed);
                            let r = s.go(p, hashes, &limits, true, 0, &mut |_| {});
                            if r.best_move.is_null() || !r.best_move.is_quiet() || r.score.abs() >= MATE_IN_MAX {
                                continue;
                            }
                            let white = if p.side_to_move() == Color::White { r.score } else { -r.score };
                            out.push(encode(p, white.clamp(-32000, 32000) as i16, result));
                        }
                    }
                    (out, used, bad)
                })
                .expect("spawn rescore thread")
        })
        .collect();
    let (mut records, mut used, mut bad) = (Vec::new(), 0, 0);
    for h in handles {
        let (r, u, b) = h.join().expect("rescore thread");
        records.extend(r);
        used += u;
        bad += b;
    }
    (records, used, bad)
}

/// Generate self-play data until the game or time budget is spent.
pub fn run(cfg: Config) -> std::io::Result<()> {
    std::fs::create_dir_all(&cfg.out_dir)?;
    let games = Arc::new(AtomicU64::new(0));
    let positions = Arc::new(AtomicU64::new(0));
    let done = Arc::new(AtomicBool::new(false));
    let start = Instant::now();
    let mut handles = Vec::new();
    for tid in 0..cfg.threads {
        let cfg = cfg.clone();
        let (games, positions, done) = (games.clone(), positions.clone(), done.clone());
        handles.push(std::thread::Builder::new().stack_size(64 << 20).spawn(move || -> std::io::Result<()> {
            let path = cfg.out_dir.join(format!("{:016x}-t{tid:02}.bin", cfg.seed));
            let mut file = OpenOptions::new().create(true).append(true).open(path)?;
            let mut rng = Rng(cfg.seed ^ (tid as u64 + 1).wrapping_mul(0xA24B_AED4_963E_E407));
            let shared = Shared::new(cfg.hash_mb, cfg.network.clone());
            let mut s = Searcher::new(shared);
            s.verbose = false;
            while !done.load(Ordering::Relaxed) {
                if let Some(records) = play_game(&mut s, &mut rng, &cfg) {
                    let mut buf = Vec::with_capacity(records.len() * RECORD_SIZE);
                    for r in &records {
                        buf.extend_from_slice(r);
                    }
                    file.write_all(&buf)?;
                    positions.fetch_add(records.len() as u64, Ordering::Relaxed);
                    let g = games.fetch_add(1, Ordering::Relaxed) + 1;
                    if cfg.games > 0 && g >= cfg.games {
                        done.store(true, Ordering::Relaxed);
                    }
                }
                if let Some(d) = cfg.duration {
                    if start.elapsed() >= d {
                        done.store(true, Ordering::Relaxed);
                    }
                }
            }
            file.flush()
        })?);
    }
    let mut last = Instant::now();
    while !done.load(Ordering::Relaxed) {
        std::thread::sleep(Duration::from_millis(200));
        if last.elapsed() >= Duration::from_secs(30) {
            last = Instant::now();
            let secs = start.elapsed().as_secs_f64();
            let p = positions.load(Ordering::Relaxed);
            println!(
                "datagen games {} positions {} elapsed {:.0}s rate {:.0} pos/s",
                games.load(Ordering::Relaxed),
                p,
                secs,
                p as f64 / secs
            );
        }
    }
    for h in handles {
        h.join().expect("datagen thread panicked")?;
    }
    let secs = start.elapsed().as_secs_f64();
    let p = positions.load(Ordering::Relaxed);
    println!(
        "datagen finished games {} positions {} elapsed {:.0}s rate {:.0} pos/s",
        games.load(Ordering::Relaxed),
        p,
        secs,
        p as f64 / secs
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_roundtrip() {
        let pos = Position::from_fen("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R b KQkq - 3 17").unwrap();
        let r = encode(&pos, -123, 2);
        let (placement, score, result, stm) = decode(&r);
        assert_eq!(placement, pos.fen().split(' ').next().unwrap());
        assert_eq!((score, result, stm), (-123, 2, Color::Black));
        assert_eq!(r[28], 3);
        assert_eq!(u16::from_le_bytes([r[29], r[30]]), 17);
    }
}
