//! NNUE evaluation: a perspective network with king-bucketed inputs and
//! piece-count output buckets, squared clipped ReLU, quantised to i16 and
//! updated incrementally as moves are made.
//!
//! Inputs, per perspective: (king bucket, own/their, piece type, square), with
//! the board flipped for Black and mirrored left-right when that side's king
//! is on files e-h. Output: one of `output_buckets` heads chosen by piece count.
//!
//! File formats (little-endian):
//! - v1: b"APNN", u32 1, u32 hidden, i16 ft[768][hidden], i16 ft_bias[hidden],
//!   i16 out[2*hidden], i32 out_bias. (One bucket, no mirroring.)
//! - v2: b"APNN", u32 2, u32 hidden, u32 input_buckets, u32 output_buckets,
//!   u32 flags (bit 0 = mirror), u8 bucket_map[64], i16 ft[input_buckets*768][hidden],
//!   i16 ft_bias[hidden], i16 out[output_buckets][2*hidden], i32 out_bias[output_buckets].

use crate::bitboard::squares;
use crate::position::Position;
use crate::types::*;
use std::sync::Arc;

pub const QA: i32 = 255;
pub const QB: i32 = 64;
pub const SCALE: i32 = 400;
const MAGIC: &[u8; 4] = b"APNN";

/// Each squared activation times an i16 weight fits in i32, but their
/// reduction needs i64 even for small networks with extreme weights.
#[inline]
fn squared_dot_scalar(acc: &[i16], weights: &[i16]) -> i64 {
    acc.iter().zip(weights).map(|(&x, &w)| {
        let c = i32::from(x).clamp(0, QA);
        i64::from(c * c * i32::from(w))
    }).sum()
}

#[inline]
fn squared_dot(acc: &[i16], weights: &[i16]) -> i64 {
    #[cfg(all(target_arch = "aarch64", target_feature = "neon"))]
    {
        // SAFETY: this build requires NEON. The kernel loads only complete
        // eight-element arrays obtained from these valid slices.
        unsafe { squared_dot_neon(acc, weights) }
    }
    #[cfg(not(all(target_arch = "aarch64", target_feature = "neon")))]
    {
        squared_dot_scalar(acc, weights)
    }
}

/// Largest output weight magnitude for which `clamp(x) * w` fits in i16
/// (255 * 128 = 32,640). Training clips output weights to +-1.98 (+-127 quantised).
pub const SMALL_WEIGHT: i32 = 128;

/// The same sum for weights within +-SMALL_WEIGHT: the activation-weight product
/// stays in i16 and the square accumulates in i32 lanes, widened every 128
/// chunks (128 * 255 * 128 * 255 < 2^31), so the result is exact.
#[inline]
fn squared_dot_small(acc: &[i16], weights: &[i16]) -> i64 {
    #[cfg(all(target_arch = "aarch64", target_feature = "neon"))]
    {
        // SAFETY: as squared_dot; the caller guarantees the weight bound.
        unsafe { squared_dot_small_neon(acc, weights) }
    }
    #[cfg(not(all(target_arch = "aarch64", target_feature = "neon")))]
    {
        squared_dot_scalar(acc, weights)
    }
}

#[cfg(all(target_arch = "aarch64", target_feature = "neon"))]
#[target_feature(enable = "neon")]
unsafe fn squared_dot_small_neon(acc: &[i16], weights: &[i16]) -> i64 {
    use std::arch::aarch64::*;
    let n = acc.len().min(weights.len());
    let (values, tail) = acc[..n].as_chunks::<16>();
    let (weights, weight_tail) = weights[..n].as_chunks::<16>();
    let zero = vdupq_n_s16(0);
    let cap = vdupq_n_s16(QA as i16);
    let mut total = 0i64;
    // Four independent i32 accumulators; each lane takes one product per
    // 16-element chunk, so 128 chunks per block keep every lane exact.
    for (vb, wb) in values.chunks(128).zip(weights.chunks(128)) {
        let (mut s0, mut s1, mut s2, mut s3) = (vdupq_n_s32(0), vdupq_n_s32(0), vdupq_n_s32(0), vdupq_n_s32(0));
        for (a, w) in vb.iter().zip(wb) {
            let c0 = vminq_s16(vmaxq_s16(vld1q_s16(a.as_ptr()), zero), cap);
            let c1 = vminq_s16(vmaxq_s16(vld1q_s16(a.as_ptr().add(8)), zero), cap);
            let p0 = vmulq_s16(c0, vld1q_s16(w.as_ptr()));
            let p1 = vmulq_s16(c1, vld1q_s16(w.as_ptr().add(8)));
            s0 = vmlal_s16(s0, vget_low_s16(p0), vget_low_s16(c0));
            s1 = vmlal_high_s16(s1, p0, c0);
            s2 = vmlal_s16(s2, vget_low_s16(p1), vget_low_s16(c1));
            s3 = vmlal_high_s16(s3, p1, c1);
        }
        total += vaddlvq_s32(s0) + vaddlvq_s32(s1) + vaddlvq_s32(s2) + vaddlvq_s32(s3);
    }
    total + squared_dot_scalar(tail, weight_tail)
}

#[cfg(all(target_arch = "aarch64", target_feature = "neon"))]
#[target_feature(enable = "neon")]
unsafe fn squared_dot_neon(acc: &[i16], weights: &[i16]) -> i64 {
    use std::arch::aarch64::*;
    let n = acc.len().min(weights.len());
    let (values, tail) = acc[..n].as_chunks::<8>();
    let (weights, weight_tail) = weights[..n].as_chunks::<8>();
    let zero = vdupq_n_s16(0);
    let cap = vdupq_n_s16(QA as i16);
    let mut low_sum = vdupq_n_s64(0);
    let mut high_sum = vdupq_n_s64(0);
    for (a, w) in values.iter().zip(weights) {
        let c = vminq_s16(vmaxq_s16(vld1q_s16(a.as_ptr()), zero), cap);
        let w = vld1q_s16(w.as_ptr());
        // Multiply c*w first, widen c, then multiply again. Squaring c in
        // i16 would overflow above 181. Widen before every pairwise sum.
        let low = vmulq_s32(vmull_s16(vget_low_s16(c), vget_low_s16(w)), vmovl_s16(vget_low_s16(c)));
        let high = vmulq_s32(vmull_high_s16(c, w), vmovl_high_s16(c));
        low_sum = vpadalq_s32(low_sum, low);
        high_sum = vpadalq_s32(high_sum, high);
    }
    vaddvq_s64(vaddq_s64(low_sum, high_sum)) + squared_dot_scalar(tail, weight_tail)
}

pub struct Network {
    pub hidden: usize,
    pub input_buckets: usize,
    pub output_buckets: usize,
    pub mirror: bool,
    /// King square (from the perspective, after mirroring) -> input bucket.
    pub bucket_map: [u8; 64],
    pub ft_weights: Vec<i16>,
    pub ft_bias: Vec<i16>,
    pub out_weights: Vec<i16>,
    pub out_bias: Vec<i32>,
    /// Every output weight is within +-SMALL_WEIGHT: the faster exact kernel applies.
    pub small_output: bool,
}

struct Reader<'a> {
    b: &'a [u8],
    off: usize,
}

impl Reader<'_> {
    fn u32(&mut self) -> Result<u32, String> {
        let s = self.b.get(self.off..self.off + 4).ok_or("network file is truncated")?;
        self.off += 4;
        Ok(u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
    }
    fn bytes(&mut self, n: usize) -> Result<&[u8], String> {
        let s = self.b.get(self.off..self.off + n).ok_or("network file is truncated")?;
        self.off += n;
        Ok(s)
    }
    fn i16s(&mut self, n: usize) -> Result<Vec<i16>, String> {
        Ok(self.bytes(2 * n)?.as_chunks::<2>().0.iter().map(|c| i16::from_le_bytes(*c)).collect())
    }
    fn i32s(&mut self, n: usize) -> Result<Vec<i32>, String> {
        Ok(self.bytes(4 * n)?.as_chunks::<4>().0.iter().map(|c| i32::from_le_bytes(*c)).collect())
    }
}

impl Network {
    pub fn from_bytes(b: &[u8]) -> Result<Network, String> {
        if b.len() < 12 || &b[..4] != MAGIC {
            return Err("not an ArhanPassant network (bad magic)".into());
        }
        let mut r = Reader { b, off: 4 };
        let version = r.u32()?;
        let hidden = r.u32()? as usize;
        if hidden == 0 || hidden > 8192 || !hidden.is_multiple_of(8) {
            return Err(format!("bad hidden size {hidden}"));
        }
        let (input_buckets, output_buckets, mirror, bucket_map) = match version {
            1 => (1, 1, false, [0u8; 64]),
            2 => {
                let ib = r.u32()? as usize;
                let ob = r.u32()? as usize;
                let flags = r.u32()?;
                let mut map = [0u8; 64];
                map.copy_from_slice(r.bytes(64)?);
                if ib == 0 || ib > 64 || ob == 0 || ob > 32 || map.iter().any(|&x| x as usize >= ib) {
                    return Err("bad bucket layout".into());
                }
                (ib, ob, flags & 1 != 0, map)
            }
            v => return Err(format!("unsupported network version {v}")),
        };
        let ft_weights = r.i16s(input_buckets * 768 * hidden)?;
        let ft_bias = r.i16s(hidden)?;
        let out_weights = r.i16s(output_buckets * 2 * hidden)?;
        let out_bias = r.i32s(output_buckets)?;
        if r.off != b.len() {
            return Err(format!("network size {} does not match its header ({} expected)", b.len(), r.off));
        }
        let small_output = out_weights.iter().all(|&w| i32::from(w).abs() <= SMALL_WEIGHT);
        Ok(Network { hidden, input_buckets, output_buckets, mirror, bucket_map, ft_weights, ft_bias, out_weights, out_bias, small_output })
    }

    pub fn load(path: &str) -> Result<Network, String> {
        let b = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Network::from_bytes(&b)
    }

    /// The network compiled into the binary, if one was present at build time.
    pub fn embedded() -> Option<Network> {
        #[cfg(has_net)]
        {
            static BYTES: &[u8] = include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/nets/default.nnue"));
            return Network::from_bytes(BYTES).ok();
        }
        #[allow(unreachable_code)]
        None
    }

    /// (bucket, mirrored) for a perspective whose king stands on `king`.
    #[inline(always)]
    fn king_key(&self, perspective: Color, king: Square) -> (usize, bool) {
        let k = if perspective == Color::White { king } else { king ^ 56 };
        let mirrored = self.mirror && file_of(k) >= 4;
        let k = if mirrored { k ^ 7 } else { k };
        (self.bucket_map[k as usize] as usize, mirrored)
    }

    /// Input feature for `piece` on `sq`, seen from `perspective` with that side's king key.
    #[inline(always)]
    fn feature(&self, perspective: Color, key: (usize, bool), piece: Piece, sq: Square) -> usize {
        let mut s = if perspective == Color::White { sq } else { sq ^ 56 };
        if key.1 {
            s ^= 7;
        }
        let own = piece.color() == perspective;
        key.0 * 768 + (if own { 0 } else { 384 }) + piece.piece_type().idx() * 64 + s as usize
    }

    #[inline(always)]
    fn output_bucket(&self, pos: &Position) -> usize {
        let n = pos.occupied().count_ones() as usize;
        let div = 32usize.div_ceil(self.output_buckets);
        (n.saturating_sub(2) / div).min(self.output_buckets - 1)
    }

    fn accumulate(&self, pos: &Position, perspective: Color, acc: &mut [i16]) {
        let h = self.hidden;
        acc.copy_from_slice(&self.ft_bias);
        let key = self.king_key(perspective, pos.king_sq(perspective));
        for sq in squares(pos.occupied()) {
            let f = self.feature(perspective, key, pos.piece_on(sq), sq);
            for (a, &x) in acc.iter_mut().zip(&self.ft_weights[f * h..(f + 1) * h]) {
                *a += x;
            }
        }
    }

    /// Full evaluation from scratch (reference implementation, used by tests).
    pub fn evaluate_full(&self, pos: &Position) -> i32 {
        let mut white = vec![0i16; self.hidden];
        let mut black = vec![0i16; self.hidden];
        self.accumulate(pos, Color::White, &mut white);
        self.accumulate(pos, Color::Black, &mut black);
        let (us, them) = if pos.side_to_move() == Color::White { (&white, &black) } else { (&black, &white) };
        self.output_with::<true>(us, them, self.output_bucket(pos))
    }

    #[inline]
    fn output(&self, us: &[i16], them: &[i16], bucket: usize) -> i32 {
        self.output_with::<false>(us, them, bucket)
    }

    #[inline]
    fn output_with<const SCALAR: bool>(&self, us: &[i16], them: &[i16], bucket: usize) -> i32 {
        let h = self.hidden;
        let w = &self.out_weights[bucket * 2 * h..(bucket + 1) * 2 * h];
        let mut sum: i64 = 0;
        for (half, w) in [(us, &w[..h]), (them, &w[h..])] {
            sum += if SCALAR {
                squared_dot_scalar(half, w)
            } else if self.small_output {
                squared_dot_small(half, w)
            } else {
                squared_dot(half, w)
            };
        }
        let out = (sum / QA as i64 + self.out_bias[bucket] as i64) * SCALE as i64 / (QA as i64 * QB as i64);
        out as i32
    }
}

/// Per-ply accumulator stack for incremental updates during search.
pub struct Accumulators {
    net: Arc<Network>,
    /// [ply][colour][hidden]
    data: Vec<i16>,
    positions: Vec<Position>,
    moves: Vec<Move>,
    ready: Vec<bool>,
    /// Each perspective/bucket/mirror remembers its last board and features.
    /// Revisiting a king bucket then updates only the pieces that changed.
    cache_boards: Vec<[Bitboard; 12]>,
    cache_data: Vec<i16>,
}

impl Accumulators {
    pub fn new(net: Arc<Network>, plies: usize) -> Accumulators {
        let h = net.hidden;
        let banks = 2 * net.input_buckets * if net.mirror { 2 } else { 1 };
        let cache_data = net.ft_bias.repeat(banks);
        Accumulators {
            net,
            data: vec![0; plies * 2 * h],
            positions: vec![Position::startpos(); plies],
            moves: vec![Move::NULL; plies],
            ready: vec![false; plies],
            cache_boards: vec![[0; 12]; banks],
            cache_data,
        }
    }

    pub fn refresh(&mut self, pos: &Position, ply: usize) {
        for c in 0..2 {
            self.refresh_side(pos, Color::from_idx(c), ply);
        }
        self.positions[ply] = *pos;
        self.ready[ply] = true;
        self.ready[ply + 1..].fill(false);
    }

    fn refresh_side(&mut self, pos: &Position, perspective: Color, ply: usize) {
        let net = &self.net;
        let h = net.hidden;
        let key = net.king_key(perspective, pos.king_sq(perspective));
        let mirrors = if net.mirror { 2 } else { 1 };
        let bank = (perspective.idx() * net.input_buckets + key.0) * mirrors + usize::from(key.1);
        let cached = &mut self.cache_data[bank * h..(bank + 1) * h];
        let board = &mut self.cache_boards[bank];
        for (idx, old) in board.iter_mut().enumerate() {
            let piece = Piece(idx as u8);
            let current = pos.pieces(piece.color(), piece.piece_type());
            for sq in squares(*old & !current) {
                let f = net.feature(perspective, key, piece, sq);
                for (a, &x) in cached.iter_mut().zip(&net.ft_weights[f * h..(f + 1) * h]) {
                    *a = a.wrapping_sub(x);
                }
            }
            for sq in squares(current & !*old) {
                let f = net.feature(perspective, key, piece, sq);
                for (a, &x) in cached.iter_mut().zip(&net.ft_weights[f * h..(f + 1) * h]) {
                    *a = a.wrapping_add(x);
                }
            }
            *old = current;
        }
        let o = (ply * 2 + perspective.idx()) * h;
        self.data[o..o + h].copy_from_slice(cached);
    }

    /// Same position at the next ply (null move).
    pub fn copy_parent(&mut self, ply: usize) {
        self.positions[ply] = self.positions[ply - 1];
        self.positions[ply].play_null();
        self.moves[ply] = Move::NULL;
        self.ready[ply] = false;
    }

    /// Record the next position. Many nodes return a table hit or a draw
    /// without evaluating: defer feature work until an evaluation needs it.
    pub fn push(&mut self, parent: &Position, m: Move, child: &Position, ply: usize) {
        debug_assert!(self.positions[ply - 1] == *parent);
        self.positions[ply] = *child;
        self.moves[ply] = m;
        self.ready[ply] = false;
    }

    fn ensure(&mut self, ply: usize) {
        if self.ready[ply] {
            return;
        }
        let mut base = ply;
        while base > 0 && !self.ready[base] {
            base -= 1;
        }
        assert!(self.ready[base], "refresh the root before evaluating");
        for next in base + 1..=ply {
            let mv = self.moves[next];
            if mv.is_null() {
                let h = self.net.hidden;
                let (a, b) = self.data.split_at_mut(next * 2 * h);
                b[..2 * h].copy_from_slice(&a[(next - 1) * 2 * h..]);
            } else {
                let (parent, child) = (self.positions[next - 1], self.positions[next]);
                self.update(&parent, mv, &child, next);
            }
            self.ready[next] = true;
        }
    }

    /// Derive an accumulator from its computed parent. Rebuild a perspective
    /// from scratch when its king changed bucket or mirror side.
    fn update(&mut self, parent: &Position, m: Move, child: &Position, ply: usize) {
        let us = parent.side_to_move();
        let moved = parent.moved_piece(m);
        let placed = child.piece_on(m.to());
        let mut add: [(Piece, Square); 2] = [(placed, m.to()), (Piece::NONE, 0)];
        let mut sub: [(Piece, Square); 2] = [(moved, m.from()), (Piece::NONE, 0)];
        let (mut n_add, mut n_sub) = (1, 1);
        if m.is_capture() {
            let cap_sq = if m.is_ep() { m.to() ^ 8 } else { m.to() };
            sub[1] = (parent.piece_on(cap_sq), cap_sq);
            n_sub = 2;
        } else if m.is_castle() {
            let (rf, rt) = if m.flags() == flag::KING_CASTLE { (m.from() + 3, m.from() + 1) } else { (m.from() - 4, m.from() - 1) };
            let rook = Piece::new(us, PieceType::Rook);
            sub[1] = (rook, rf);
            add[1] = (rook, rt);
            n_sub = 2;
            n_add = 2;
        }
        for c in 0..2 {
            let persp = Color::from_idx(c);
            let key = self.net.king_key(persp, child.king_sq(persp));
            if moved.piece_type() == PieceType::King && persp == us && self.net.king_key(persp, parent.king_sq(persp)) != key {
                self.refresh_side(child, persp, ply);
                continue;
            }
            let net = &self.net;
            let h = net.hidden;
            let (prev_all, cur_all) = self.data.split_at_mut(ply * 2 * h);
            let prev = &prev_all[(ply - 1) * 2 * h + c * h..(ply - 1) * 2 * h + (c + 1) * h];
            let cur = &mut cur_all[c * h..(c + 1) * h];
            let weights = |(p, s)| {
                let f = net.feature(persp, key, p, s);
                &net.ft_weights[f * h..(f + 1) * h]
            };
            let (a, b) = (weights(add[0]), weights(sub[0]));
            // Fuse copying and all feature changes into one pass. Keeping the
            // move kind outside the loop lets LLVM vectorise each case.
            match (n_add, n_sub) {
                (1, 1) => {
                    for i in 0..h {
                        cur[i] = prev[i].wrapping_add(a[i]).wrapping_sub(b[i]);
                    }
                }
                (1, 2) => {
                    let d = weights(sub[1]);
                    for i in 0..h {
                        cur[i] = prev[i].wrapping_add(a[i]).wrapping_sub(b[i]).wrapping_sub(d[i]);
                    }
                }
                (2, 2) => {
                    let (c, d) = (weights(add[1]), weights(sub[1]));
                    for i in 0..h {
                        cur[i] = prev[i].wrapping_add(a[i]).wrapping_add(c[i]).wrapping_sub(b[i]).wrapping_sub(d[i]);
                    }
                }
                _ => unreachable!("feature changes for a legal move"),
            }
        }
    }

    pub fn evaluate(&mut self, pos: &Position, ply: usize) -> i32 {
        self.ensure(ply);
        let h = self.net.hidden;
        let us = pos.side_to_move().idx();
        let o = ply * 2 * h;
        let (a, b) = (&self.data[o..o + h], &self.data[o + h..o + 2 * h]);
        let bucket = self.net.output_bucket(pos);
        if us == 0 {
            self.net.output(a, b, bucket)
        } else {
            self.net.output(b, a, bucket)
        }
    }
}

/// The king-bucket layout used by version-2 networks: four buckets on the back
/// rank by file pair, two on the second rank, one for ranks 3-4, one beyond.
pub const DEFAULT_BUCKETS: [u8; 64] = {
    let mut m = [0u8; 64];
    let mut sq = 0;
    while sq < 64 {
        let (file, rank) = (sq % 8, sq / 8);
        let f = if file < 4 { file } else { 7 - file };
        m[sq] = match rank {
            0 => f as u8,
            1 => 4 + (f >= 2) as u8,
            2 | 3 => 6,
            _ => 7,
        };
        sq += 1;
    }
    m
};

#[cfg(test)]
mod tests {
    use super::*;

    fn rng(seed: u64) -> impl FnMut() -> i16 {
        let mut s = seed;
        move || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            ((s >> 40) % 61) as i16 - 30
        }
    }

    /// A random network with small weights; `v2` uses king and output buckets.
    pub fn random_net(hidden: usize, seed: u64, v2: bool) -> Network {
        let mut r = rng(seed);
        let (ib, ob) = if v2 { (8, 8) } else { (1, 1) };
        Network {
            hidden,
            input_buckets: ib,
            output_buckets: ob,
            mirror: v2,
            bucket_map: if v2 { DEFAULT_BUCKETS } else { [0; 64] },
            ft_weights: (0..ib * 768 * hidden).map(|_| r()).collect(),
            ft_bias: (0..hidden).map(|_| r()).collect(),
            out_weights: (0..ob * 2 * hidden).map(|_| r()).collect(),
            out_bias: (0..ob).map(|i| 17 + i as i32).collect(),
            small_output: true,
        }
    }

    fn walk(net: Arc<Network>, fen: &str, seed: u64) {
        let mut acc = Accumulators::new(net.clone(), 300);
        let mut pos = Position::from_fen(fen).unwrap();
        acc.refresh(&pos, 0);
        let mut ply = 0;
        let mut rng = seed;
        while ply < 290 {
            let moves = pos.legal_moves();
            if moves.is_empty() {
                break;
            }
            rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            let m = moves[(rng >> 33) as usize % moves.len()];
            let child = pos.after(m);
            acc.push(&pos, m, &child, ply + 1);
            ply += 1;
            pos = child;
            assert_eq!(acc.evaluate(&pos, ply), net.evaluate_full(&pos), "after {m} in {}", pos.fen());
        }
    }

    #[test]
    fn incremental_matches_full() {
        let fen = "r3k2r/pPppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1";
        walk(Arc::new(random_net(64, 0x1234_5678, false)), fen, 7);
        // Version 2: kings wander across buckets and mirror sides, forcing refreshes.
        for seed in 1..6 {
            walk(Arc::new(random_net(32, 0x9E37 + seed, true)), fen, seed);
            walk(Arc::new(random_net(32, 0x7F4A + seed, true)), "4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1", seed);
        }
    }

    #[test]
    fn feature_updates_cover_castling_en_passant_and_promotions() {
        let cases = [
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
            ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"),
            ("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8n"),
            ("4k3/8/8/8/8/8/8/3K4 w - - 0 1", "d1e1"),
        ];
        for v2 in [false, true] {
            let net = Arc::new(random_net(64, 17, v2));
            let mut acc = Accumulators::new(net.clone(), 3);
            for (fen, uci) in cases {
                let pos = Position::from_fen(fen).unwrap();
                let mv = pos.parse_uci_move(uci).unwrap();
                let child = pos.after(mv);
                acc.refresh(&pos, 0);
                acc.push(&pos, mv, &child, 1);
                assert_eq!(acc.evaluate(&child, 1), net.evaluate_full(&child), "{uci} in {fen}");
            }
        }
    }

    #[test]
    fn deferred_chains_null_moves_and_backtracking_match_full() {
        for v2 in [false, true] {
            let net = Arc::new(random_net(64, 31, v2));
            let mut acc = Accumulators::new(net.clone(), 130);
            let root = Position::from_fen("r3k2r/pPppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1").unwrap();
            acc.refresh(&root, 0);
            let mut positions = vec![root];
            let mut rng = 31u64;
            for ply in 1..120 {
                let pos = *positions.last().unwrap();
                let moves = pos.legal_moves();
                if moves.is_empty() {
                    break;
                }
                let mut child = pos;
                if ply % 9 == 0 && !pos.in_check() {
                    child.play_null();
                    acc.copy_parent(ply);
                } else {
                    rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1);
                    let mv = moves[(rng >> 33) as usize % moves.len()];
                    child.play(mv);
                    acc.push(&pos, mv, &child, ply);
                }
                positions.push(child);
                if ply % 7 == 0 {
                    assert_eq!(acc.evaluate(&child, ply), net.evaluate_full(&child));
                }
            }
            let ply = positions.len() - 1;
            assert_eq!(acc.evaluate(&positions[ply], ply), net.evaluate_full(&positions[ply]));
            for ply in (0..positions.len() - 1).rev() {
                let parent = positions[ply];
                for mv in parent.legal_moves().iter().take(3) {
                    let child = parent.after(mv);
                    acc.push(&parent, mv, &child, ply + 1);
                    assert_eq!(acc.evaluate(&child, ply + 1), net.evaluate_full(&child));
                }
            }
        }
    }

    #[test]
    fn mirrored_positions_agree() {
        let a = Position::from_fen("r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4").unwrap();
        let b = Position::from_fen("rnbqk2r/pppp1ppp/5n2/2b1p3/4P3/2N2N2/PPPP1PPP/R1BQKB1R b KQkq - 4 4").unwrap();
        for v2 in [false, true] {
            let net = random_net(32, 99, v2);
            assert_eq!(net.evaluate_full(&a), net.evaluate_full(&b));
        }
        // With mirroring, a left-right reflection of the whole board evaluates the same.
        let net = random_net(32, 5, true);
        let left = Position::from_fen("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1").unwrap();
        let right = Position::from_fen("1k6/ppp5/8/8/8/8/PPP5/1K2R3 w - - 0 1").unwrap();
        assert_eq!(net.evaluate_full(&left), net.evaluate_full(&right));
    }

    fn to_bytes(net: &Network, version: u32) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(MAGIC);
        b.extend_from_slice(&version.to_le_bytes());
        b.extend_from_slice(&(net.hidden as u32).to_le_bytes());
        if version == 2 {
            b.extend_from_slice(&(net.input_buckets as u32).to_le_bytes());
            b.extend_from_slice(&(net.output_buckets as u32).to_le_bytes());
            b.extend_from_slice(&(net.mirror as u32).to_le_bytes());
            b.extend_from_slice(&net.bucket_map);
        }
        for v in net.ft_weights.iter().chain(&net.ft_bias).chain(&net.out_weights) {
            b.extend_from_slice(&v.to_le_bytes());
        }
        for v in &net.out_bias {
            b.extend_from_slice(&v.to_le_bytes());
        }
        b
    }

    #[test]
    fn bytes_roundtrip() {
        let pos = Position::from_fen("r1bq1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N1PN2/PP2BPPP/R2Q1RK1 w - - 0 10").unwrap();
        for (v2, version) in [(false, 1), (true, 2)] {
            let net = random_net(16, 5, v2);
            let b = to_bytes(&net, version);
            let back = Network::from_bytes(&b).unwrap();
            assert_eq!(back.evaluate_full(&pos), net.evaluate_full(&pos));
            assert!(Network::from_bytes(&b[..b.len() - 1]).is_err());
        }
    }

    #[test]
    fn output_kernel_matches_scalar_at_extremes_and_unaligned_lengths() {
        let mut seed = 0x1234_5678u64;
        for n in [0, 1, 7, 8, 9, 15, 16, 24, 64, 512, 1024, 8192] {
            let mut values = vec![0i16; n + 1];
            let mut weights = values.clone();
            for i in 0..n + 1 {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                values[i] = (seed >> 32) as i16;
                weights[i] = (seed >> 48) as i16;
            }
            // Starting one i16 into a vector exercises unaligned loads.
            assert_eq!(squared_dot(&values[1..], &weights[1..]), squared_dot_scalar(&values[1..], &weights[1..]));
            for x in [i16::MIN, -1, 0, 1, 181, 254, 255, 256, i16::MAX] {
                for w in [i16::MIN, -1, 0, 1, i16::MAX] {
                    values.fill(x);
                    weights.fill(w);
                    let c = i64::from(x).clamp(0, i64::from(QA));
                    assert_eq!(squared_dot(&values[1..], &weights[1..]), c * c * i64::from(w) * n as i64);
                }
            }
        }
        let values = [255; 8];
        let weights = [i16::MAX; 8];
        assert!(squared_dot(&values, &weights) > i64::from(i32::MAX));
    }

    #[test]
    fn small_weight_kernel_is_exact_up_to_the_largest_networks() {
        let mut seed = 0x9E37_79B9u64;
        for n in [0, 1, 7, 8, 15, 16, 17, 31, 32, 512, 1024, 2048, 2056, 4096, 8192] {
            let mut values = vec![0i16; n + 1];
            let mut weights = values.clone();
            for i in 0..n + 1 {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                values[i] = (seed >> 32) as i16;
                weights[i] = ((seed >> 48) as i16 as i32 % (SMALL_WEIGHT + 1)) as i16;
            }
            assert_eq!(squared_dot_small(&values[1..], &weights[1..]), squared_dot_scalar(&values[1..], &weights[1..]), "n {n}");
            // Extremes: every activation saturated, every weight at the bound, both signs.
            for w in [-SMALL_WEIGHT as i16, SMALL_WEIGHT as i16] {
                values.fill(i16::MAX);
                weights.fill(w);
                assert_eq!(squared_dot_small(&values[1..], &weights[1..]), 255 * 255 * i64::from(w) * n as i64, "n {n} w {w}");
            }
        }
    }
}
