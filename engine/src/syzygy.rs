//! Syzygy endgame tablebases: win/draw/loss probes inside the search and
//! distance-to-zero probes at the root, through the pyrrhic-rs prober.
//!
//! The tables are a process-wide resource (the prober keeps global state), so
//! they live in one static handle that is never cloned; search threads probe
//! through a read lock. In the WebAssembly build there are no tables and every
//! probe returns `None`.

use crate::position::Position;
use crate::types::*;
use std::sync::atomic::{AtomicU32, Ordering};

/// Result of a tablebase probe, from the side to move's point of view.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Wdl {
    Loss,
    /// Lost, but the fifty-move rule saves it.
    BlessedLoss,
    Draw,
    /// Won, but the fifty-move rule spoils it.
    CursedWin,
    Win,
}

/// Largest piece count the loaded tables cover (0 when none are loaded).
static MAX_PIECES: AtomicU32 = AtomicU32::new(0);

/// Largest piece count the search probes (UCI `SyzygyProbeLimit`).
static PROBE_LIMIT: AtomicU32 = AtomicU32::new(7);

#[inline(always)]
pub fn probe_limit() -> u32 {
    PROBE_LIMIT.load(Ordering::Relaxed)
}

pub fn set_probe_limit(n: u32) {
    PROBE_LIMIT.store(n.min(7), Ordering::Relaxed);
}

#[inline(always)]
pub fn max_pieces() -> u32 {
    MAX_PIECES.load(Ordering::Relaxed)
}

/// Can this position be probed at all? (Tables never cover castling rights.)
#[inline(always)]
pub fn probeable(pos: &Position, limit: u32) -> bool {
    let n = pos.occupied().count_ones();
    n <= limit.min(max_pieces()) && pos.castling_rights() == 0
}

#[cfg(not(target_arch = "wasm32"))]
mod imp {
    use super::*;
    use crate::bitboard;
    use pyrrhic_rs::{DtzProbeValue, EngineAdapter, TableBases, WdlProbeResult};
    use std::sync::RwLock;

    #[derive(Clone)]
    pub struct Adapter;

    impl EngineAdapter for Adapter {
        fn pawn_attacks(color: pyrrhic_rs::Color, sq: u64) -> u64 {
            let c = if color == pyrrhic_rs::Color::White { Color::White } else { Color::Black };
            bitboard::pawn_attacks(c, sq as Square)
        }
        fn knight_attacks(sq: u64) -> u64 {
            bitboard::knight_attacks(sq as Square)
        }
        fn bishop_attacks(sq: u64, occ: u64) -> u64 {
            bitboard::bishop_attacks(sq as Square, occ)
        }
        fn rook_attacks(sq: u64, occ: u64) -> u64 {
            bitboard::rook_attacks(sq as Square, occ)
        }
        fn queen_attacks(sq: u64, occ: u64) -> u64 {
            bitboard::queen_attacks(sq as Square, occ)
        }
        fn king_attacks(sq: u64) -> u64 {
            bitboard::king_attacks(sq as Square)
        }
    }

    static TB: RwLock<Option<TableBases<Adapter>>> = RwLock::new(None);

    pub fn init(path: &str) -> Result<u32, String> {
        let mut guard = TB.write().map_err(|_| "tablebase lock poisoned".to_string())?;
        MAX_PIECES.store(0, Ordering::Relaxed);
        *guard = None; // frees the old tables before loading new ones
        let path = path.trim();
        if path.is_empty() || path == "<empty>" {
            return Ok(0);
        }
        // pyrrhic-rs 0.2 registers every table name whether or not its file
        // exists (its file test returns -1, not 0, for a missing file), so its
        // own largest size always reads 7. Count the files that are really there.
        let n = largest_on_disk(path);
        if n < 3 {
            return Err(format!("no .rtbw files in {path}"));
        }
        let tb = TableBases::<Adapter>::new(path).map_err(|e| format!("{e:?}"))?;
        *guard = Some(tb);
        MAX_PIECES.store(n, Ordering::Relaxed);
        Ok(n)
    }

    /// Largest piece count among the WDL files in the `:`-separated directories.
    fn largest_on_disk(path: &str) -> u32 {
        let mut largest = 0;
        for dir in path.split(':').filter(|d| !d.is_empty()) {
            let Ok(entries) = std::fs::read_dir(dir) else { continue };
            for e in entries.flatten() {
                let name = e.file_name();
                let Some(stem) = name.to_str().and_then(|n| n.strip_suffix(".rtbw")) else { continue };
                if stem.starts_with('K') && stem.contains("vK") {
                    largest = largest.max(stem.chars().filter(|c| *c != 'v').count() as u32);
                }
            }
        }
        largest
    }

    fn boards(pos: &Position) -> [u64; 8] {
        let t = |pt| pos.type_bb(pt);
        [
            pos.color_bb(Color::White),
            pos.color_bb(Color::Black),
            t(PieceType::King),
            t(PieceType::Queen),
            t(PieceType::Rook),
            t(PieceType::Bishop),
            t(PieceType::Knight),
            t(PieceType::Pawn),
        ]
    }

    fn wdl_of(r: WdlProbeResult) -> Wdl {
        match r {
            WdlProbeResult::Loss => Wdl::Loss,
            WdlProbeResult::BlessedLoss => Wdl::BlessedLoss,
            WdlProbeResult::Draw => Wdl::Draw,
            WdlProbeResult::CursedWin => Wdl::CursedWin,
            WdlProbeResult::Win => Wdl::Win,
        }
    }

    pub fn probe_wdl(pos: &Position) -> Option<Wdl> {
        let guard = TB.read().ok()?;
        let tb = guard.as_ref()?;
        let [w, b, k, q, r, bi, n, p] = boards(pos);
        let ep = pos.ep_square().map_or(0, |s| s as u32);
        tb.probe_wdl(w, b, k, q, r, bi, n, p, ep, pos.side_to_move() == Color::White).ok().map(wdl_of)
    }

    pub fn probe_root(pos: &Position) -> Option<(Wdl, Vec<(Move, Wdl, u16)>)> {
        let guard = TB.read().ok()?;
        let tb = guard.as_ref()?;
        let [w, b, k, q, r, bi, n, p] = boards(pos);
        let ep = pos.ep_square().map_or(0, |s| s as u32);
        let res = tb
            .probe_root(w, b, k, q, r, bi, n, p, pos.halfmove_clock() as u32, ep, pos.side_to_move() == Color::White)
            .ok()?;
        let root = match res.root {
            DtzProbeValue::DtzResult(d) => wdl_of(d.wdl),
            _ => return None,
        };
        let legal = pos.legal_moves();
        let mut out = Vec::with_capacity(res.num_moves);
        for v in &res.moves[..res.num_moves] {
            let DtzProbeValue::DtzResult(d) = *v else { continue };
            let promo = match d.promotion {
                pyrrhic_rs::Piece::Queen => Some(PieceType::Queen),
                pyrrhic_rs::Piece::Rook => Some(PieceType::Rook),
                pyrrhic_rs::Piece::Bishop => Some(PieceType::Bishop),
                pyrrhic_rs::Piece::Knight => Some(PieceType::Knight),
                _ => None,
            };
            let m = legal.iter().find(|m| m.from() == d.from_square && m.to() == d.to_square && m.promotion() == promo)?;
            out.push((m, wdl_of(d.wdl), d.dtz));
        }
        // Every legal move must be ranked, or the filter could drop the only good one.
        (out.len() == legal.len()).then_some((root, out))
    }
}

#[cfg(target_arch = "wasm32")]
mod imp {
    use super::*;
    pub fn init(_path: &str) -> Result<u32, String> {
        Err("tablebases are not available in this build".into())
    }
    pub fn probe_wdl(_pos: &Position) -> Option<Wdl> {
        None
    }
    pub fn probe_root(_pos: &Position) -> Option<(Wdl, Vec<(Move, Wdl, u16)>)> {
        None
    }
}

/// Load the tables on a path list (`:`-separated; `;` on Windows is not
/// supported by the prober, use relative paths there). An empty path unloads them.
/// Returns the largest piece count covered.
pub fn init(path: &str) -> Result<u32, String> {
    imp::init(path)
}

/// Win/draw/loss of the position for the side to move, ignoring the fifty-move
/// counter's current value (callers probe only right after a capture or pawn move).
#[inline]
pub fn probe_wdl(pos: &Position) -> Option<Wdl> {
    imp::probe_wdl(pos)
}

/// The root moves that keep the best tablebase result: the fastest wins by
/// distance to zero, every move that keeps a draw, or the slowest losses.
/// `None` when the position is not covered, so the search uses every move.
pub fn root_moves(pos: &Position) -> Option<(Wdl, Vec<Move>)> {
    if !probeable(pos, u32::MAX) {
        return None;
    }
    let (root, moves) = imp::probe_root(pos)?;
    let best = moves.iter().map(|&(_, w, _)| w).max()?;
    let same: Vec<_> = moves.iter().filter(|&&(_, w, _)| w == best).collect();
    let pick = match best {
        Wdl::Win | Wdl::CursedWin => same.iter().map(|&&(_, _, d)| d).min(),
        Wdl::Loss | Wdl::BlessedLoss => same.iter().map(|&&(_, _, d)| d).max(),
        Wdl::Draw => None,
    };
    let kept = same.iter().filter(|&&&(_, _, d)| pick.is_none_or(|p| d == p)).map(|&&(m, _, _)| m).collect();
    Some((root, kept))
}
