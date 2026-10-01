//! Lock-free shared transposition table (XOR-verified entries, 4-way clusters).

use crate::types::Move;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering::Relaxed};

pub const BOUND_NONE: u8 = 0;
pub const BOUND_UPPER: u8 = 1;
pub const BOUND_LOWER: u8 = 2;
pub const BOUND_EXACT: u8 = 3;

#[derive(Copy, Clone, Debug)]
pub struct TtEntry {
    pub mv: Move,
    pub score: i32,
    pub eval: i32,
    pub depth: i32,
    pub bound: u8,
    pub pv: bool,
}

#[derive(Default)]
struct Slot {
    key: AtomicU64,
    data: AtomicU64,
}

#[repr(C, align(64))]
#[derive(Default)]
struct Cluster {
    slots: [Slot; 4],
}

pub struct TranspositionTable {
    clusters: Vec<Cluster>,
    age: AtomicU8,
}

// data layout: move 16 | score 16 | eval 16 | depth 8 | bound 2 | pv 1 | age 5
#[inline(always)]
fn pack(mv: Move, score: i32, eval: i32, depth: i32, bound: u8, pv: bool, age: u8) -> u64 {
    (mv.0 as u64)
        | ((score as i16 as u16 as u64) << 16)
        | ((eval as i16 as u16 as u64) << 32)
        | ((depth.clamp(0, 255) as u64) << 48)
        | ((bound as u64 & 3) << 56)
        | ((pv as u64) << 58)
        | (((age & 31) as u64) << 59)
}

#[inline(always)]
fn unpack(d: u64) -> (TtEntry, u8) {
    (
        TtEntry {
            mv: Move(d as u16),
            score: (d >> 16) as u16 as i16 as i32,
            eval: (d >> 32) as u16 as i16 as i32,
            depth: ((d >> 48) & 0xFF) as i32,
            bound: ((d >> 56) & 3) as u8,
            pv: (d >> 58) & 1 != 0,
        },
        ((d >> 59) & 31) as u8,
    )
}

impl TranspositionTable {
    pub fn new(mb: usize) -> TranspositionTable {
        let n = ((mb.max(1) << 20) / std::mem::size_of::<Cluster>()).max(1);
        let mut clusters = Vec::with_capacity(n);
        clusters.resize_with(n, Cluster::default);
        TranspositionTable { clusters, age: AtomicU8::new(0) }
    }

    pub fn clear(&self) {
        for c in &self.clusters {
            for s in &c.slots {
                s.key.store(0, Relaxed);
                s.data.store(0, Relaxed);
            }
        }
        self.age.store(0, Relaxed);
    }

    /// Call once per new search so older entries become preferred victims.
    pub fn new_search(&self) {
        self.age.store((self.age.load(Relaxed) + 1) & 31, Relaxed);
    }

    #[inline(always)]
    fn cluster(&self, key: u64) -> &Cluster {
        let idx = ((key as u128 * self.clusters.len() as u128) >> 64) as usize;
        &self.clusters[idx]
    }

    pub fn probe(&self, key: u64) -> Option<TtEntry> {
        for s in &self.cluster(key).slots {
            let d = s.data.load(Relaxed);
            if s.key.load(Relaxed) ^ d == key && d != 0 {
                return Some(unpack(d).0);
            }
        }
        None
    }

    #[allow(clippy::too_many_arguments)]
    pub fn store(&self, key: u64, mv: Move, score: i32, eval: i32, depth: i32, bound: u8, pv: bool) {
        let age = self.age.load(Relaxed);
        let cluster = self.cluster(key);
        let mut victim = &cluster.slots[0];
        let mut victim_value = i32::MAX;
        for s in &cluster.slots {
            let d = s.data.load(Relaxed);
            let k = s.key.load(Relaxed) ^ d;
            if k == key || d == 0 {
                victim = s;
                break;
            }
            let (e, a) = unpack(d);
            let age_diff = (32 + age as i32 - a as i32) & 31;
            let value = e.depth - 8 * age_diff;
            if value < victim_value {
                victim_value = value;
                victim = s;
            }
        }
        let old = victim.data.load(Relaxed);
        let old_key = victim.key.load(Relaxed) ^ old;
        let mut mv = mv;
        if old_key == key && old != 0 {
            let (e, a) = unpack(old);
            // Keep a deeper same-age result unless this one is exact.
            if bound != BOUND_EXACT && a == age && depth + 4 < e.depth {
                return;
            }
            if mv.is_null() {
                mv = e.mv;
            }
        }
        let d = pack(mv, score, eval, depth, bound, pv, age);
        victim.data.store(d, Relaxed);
        victim.key.store(key ^ d, Relaxed);
    }

    /// Per-mille of sampled entries written during the current search.
    pub fn hashfull(&self) -> usize {
        let age = self.age.load(Relaxed);
        let sample = self.clusters.len().min(250);
        let mut used = 0;
        for c in &self.clusters[..sample] {
            for s in &c.slots {
                let d = s.data.load(Relaxed);
                if d != 0 && unpack(d).1 == age {
                    used += 1;
                }
            }
        }
        used * 1000 / (sample * 4)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let tt = TranspositionTable::new(1);
        let m = Move::new(12, 28, 1);
        tt.store(0xDEAD_BEEF_1234_5678, m, -31_000, 57, 17, BOUND_LOWER, true);
        let e = tt.probe(0xDEAD_BEEF_1234_5678).unwrap();
        assert_eq!((e.mv, e.score, e.eval, e.depth, e.bound, e.pv), (m, -31_000, 57, 17, BOUND_LOWER, true));
        assert!(tt.probe(0xDEAD_BEEF_1234_5679).is_none());
    }
}
