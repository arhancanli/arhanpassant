//! Generates the static lookup tables (leaper attacks, magic bitboards, line
//! masks and Zobrist keys) at build time so the engine needs no runtime
//! initialisation and the same tables ship to native and WebAssembly builds.

use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::Path;

const ROOK_DIRS: [(i32, i32); 4] = [(1, 0), (-1, 0), (0, 1), (0, -1)];
const BISHOP_DIRS: [(i32, i32); 4] = [(1, 1), (1, -1), (-1, 1), (-1, -1)];

fn on_board(f: i32, r: i32) -> bool {
    (0..8).contains(&f) && (0..8).contains(&r)
}

fn bit(f: i32, r: i32) -> u64 {
    1u64 << (r * 8 + f)
}

fn slider_attacks(sq: usize, occ: u64, dirs: &[(i32, i32)]) -> u64 {
    let (f0, r0) = ((sq % 8) as i32, (sq / 8) as i32);
    let mut att = 0u64;
    for &(df, dr) in dirs {
        let (mut f, mut r) = (f0 + df, r0 + dr);
        while on_board(f, r) {
            att |= bit(f, r);
            if occ & bit(f, r) != 0 {
                break;
            }
            f += df;
            r += dr;
        }
    }
    att
}

/// Relevant-occupancy mask: every ray square except the last one on the edge.
fn slider_mask(sq: usize, dirs: &[(i32, i32)]) -> u64 {
    let (f0, r0) = ((sq % 8) as i32, (sq / 8) as i32);
    let mut mask = 0u64;
    for &(df, dr) in dirs {
        let (mut f, mut r) = (f0 + df, r0 + dr);
        while on_board(f + df, r + dr) {
            mask |= bit(f, r);
            f += df;
            r += dr;
        }
    }
    mask
}

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn sparse(&mut self) -> u64 {
        self.next() & self.next() & self.next()
    }
}

fn splitmix(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

struct Magics {
    masks: Vec<u64>,
    magics: Vec<u64>,
    shifts: Vec<u8>,
    offsets: Vec<u32>,
    table: Vec<u64>,
}

fn build_magics(dirs: &[(i32, i32)], rng: &mut Rng) -> Magics {
    let mut m = Magics {
        masks: Vec::new(),
        magics: Vec::new(),
        shifts: Vec::new(),
        offsets: Vec::new(),
        table: Vec::new(),
    };
    for sq in 0..64 {
        let mask = slider_mask(sq, dirs);
        let bits = mask.count_ones();
        let n = 1usize << bits;
        let mut occs = Vec::with_capacity(n);
        let mut atts = Vec::with_capacity(n);
        let mut sub = 0u64;
        loop {
            occs.push(sub);
            atts.push(slider_attacks(sq, sub, dirs));
            sub = sub.wrapping_sub(mask) & mask;
            if sub == 0 {
                break;
            }
        }
        assert_eq!(occs.len(), n);
        let shift = 64 - bits;
        let (magic, table) = loop {
            let magic = rng.sparse();
            if (mask.wrapping_mul(magic) >> 56).count_ones() < 6 {
                continue;
            }
            let mut table = vec![0u64; n];
            let mut used = vec![false; n];
            let mut ok = true;
            for i in 0..n {
                let idx = (occs[i].wrapping_mul(magic) >> shift) as usize;
                if !used[idx] {
                    used[idx] = true;
                    table[idx] = atts[i];
                } else if table[idx] != atts[i] {
                    ok = false;
                    break;
                }
            }
            if ok {
                break (magic, table);
            }
        };
        m.masks.push(mask);
        m.magics.push(magic);
        m.shifts.push(shift as u8);
        m.offsets.push(m.table.len() as u32);
        m.table.extend_from_slice(&table);
    }
    m
}

fn leaper(sq: usize, deltas: &[(i32, i32)]) -> u64 {
    let (f0, r0) = ((sq % 8) as i32, (sq / 8) as i32);
    deltas
        .iter()
        .filter(|&&(df, dr)| on_board(f0 + df, r0 + dr))
        .fold(0u64, |acc, &(df, dr)| acc | bit(f0 + df, r0 + dr))
}

fn write_u64s(out: &mut String, name: &str, data: &[u64]) {
    writeln!(out, "pub static {name}: [u64; {}] = [", data.len()).unwrap();
    for chunk in data.chunks(8) {
        out.push_str("    ");
        for v in chunk {
            write!(out, "0x{v:016x}, ").unwrap();
        }
        out.push('\n');
    }
    out.push_str("];\n");
}

fn write_u64_grid(out: &mut String, name: &str, rows: &[Vec<u64>]) {
    writeln!(out, "pub static {name}: [[u64; {}]; {}] = [", rows[0].len(), rows.len()).unwrap();
    for row in rows {
        out.push_str("    [");
        for v in row {
            write!(out, "0x{v:x},").unwrap();
        }
        out.push_str("],\n");
    }
    out.push_str("];\n");
}

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    // Embed the shipped network when one is present.
    println!("cargo:rerun-if-changed=nets/default.nnue");
    println!("cargo::rustc-check-cfg=cfg(has_net)");
    if Path::new("nets/default.nnue").exists() {
        println!("cargo:rustc-cfg=has_net");
    }
    let mut out = String::with_capacity(4 << 20);
    out.push_str("// @generated by build.rs. Do not edit.\n");

    let knight_d = [(1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2)];
    let king_d = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)];
    let knights: Vec<u64> = (0..64).map(|s| leaper(s, &knight_d)).collect();
    let kings: Vec<u64> = (0..64).map(|s| leaper(s, &king_d)).collect();
    let white_pawns: Vec<u64> = (0..64).map(|s| leaper(s, &[(-1, 1), (1, 1)])).collect();
    let black_pawns: Vec<u64> = (0..64).map(|s| leaper(s, &[(-1, -1), (1, -1)])).collect();
    write_u64s(&mut out, "KNIGHT_ATTACKS", &knights);
    write_u64s(&mut out, "KING_ATTACKS", &kings);
    write_u64_grid(&mut out, "PAWN_ATTACKS", &[white_pawns, black_pawns]);

    let mut rng = Rng(0x0A4D_A11C_E5EE_D001);
    for (prefix, dirs) in [("ROOK", &ROOK_DIRS), ("BISHOP", &BISHOP_DIRS)] {
        let m = build_magics(dirs, &mut rng);
        write_u64s(&mut out, &format!("{prefix}_MASKS"), &m.masks);
        write_u64s(&mut out, &format!("{prefix}_MAGICS"), &m.magics);
        writeln!(out, "pub static {prefix}_SHIFTS: [u8; 64] = {:?};", m.shifts).unwrap();
        writeln!(out, "pub static {prefix}_OFFSETS: [u32; 64] = {:?};", m.offsets).unwrap();
        write_u64s(&mut out, &format!("{prefix}_TABLE"), &m.table);
    }

    // BETWEEN[a][b]: squares strictly between two aligned squares.
    // LINE[a][b]: the whole line through two aligned squares (edge to edge).
    let all_dirs: Vec<(i32, i32)> = ROOK_DIRS.iter().chain(BISHOP_DIRS.iter()).copied().collect();
    let mut between = vec![vec![0u64; 64]; 64];
    let mut line = vec![vec![0u64; 64]; 64];
    for a in 0..64usize {
        let (fa, ra) = ((a % 8) as i32, (a / 8) as i32);
        for &(df, dr) in &all_dirs {
            let full = slider_attacks(a, 0, &[(df, dr), (-df, -dr)]) | (1u64 << a);
            let (mut f, mut r) = (fa + df, ra + dr);
            let mut path = 0u64;
            while on_board(f, r) {
                let b = (r * 8 + f) as usize;
                between[a][b] = path;
                line[a][b] = full;
                path |= 1u64 << b;
                f += df;
                r += dr;
            }
        }
    }
    write_u64_grid(&mut out, "BETWEEN", &between);
    write_u64_grid(&mut out, "LINE", &line);

    let mut seed = 0x5EED_A4A4_2026_0001u64;
    let pieces: Vec<Vec<u64>> = (0..12).map(|_| (0..64).map(|_| splitmix(&mut seed)).collect()).collect();
    let castling: Vec<u64> = (0..16).map(|_| splitmix(&mut seed)).collect();
    let ep: Vec<u64> = (0..8).map(|_| splitmix(&mut seed)).collect();
    let side = splitmix(&mut seed);
    write_u64_grid(&mut out, "ZOBRIST_PIECES", &pieces);
    write_u64s(&mut out, "ZOBRIST_CASTLING", &castling);
    write_u64s(&mut out, "ZOBRIST_EP", &ep);
    writeln!(out, "pub static ZOBRIST_SIDE: u64 = 0x{side:016x};").unwrap();

    let dest = Path::new(&env::var("OUT_DIR").unwrap()).join("tables.rs");
    fs::write(dest, out).unwrap();
}
