//! Search parameters. Every value here can be set at runtime through a UCI
//! option of the same name, which is how the self-improvement loop tunes them
//! (SPSA) without rebuilding. Defaults are what ships.

macro_rules! tunables {
    ($($name:ident: $default:expr, $min:expr, $max:expr;)*) => {
        pub mod store {
            use std::sync::atomic::AtomicI32;
            $(
                #[allow(non_upper_case_globals)]
                pub static $name: AtomicI32 = AtomicI32::new($default);
            )*
        }

        $(
            #[inline(always)]
            pub fn $name() -> i32 {
                store::$name.load(std::sync::atomic::Ordering::Relaxed)
            }
        )*

        /// (name, default, min, max) for every tunable.
        pub const ALL: &[(&str, i32, i32, i32)] = &[$((stringify!($name), $default, $min, $max)),*];

        /// Set a tunable by name; returns false when the name is unknown or out of range.
        pub fn set(name: &str, value: i32) -> bool {
            match name {
                $(stringify!($name) => {
                    if !($min..=$max).contains(&value) { return false; }
                    store::$name.store(value, std::sync::atomic::Ordering::Relaxed);
                    true
                })*
                _ => false,
            }
        }

        pub fn get(name: &str) -> Option<i32> {
            match name {
                $(stringify!($name) => Some($name()),)*
                _ => None,
            }
        }
    };
}

tunables! {
    rfp_depth: 8, 4, 12;
    rfp_margin: 75, 30, 160;
    nmp_base: 3, 1, 6;
    nmp_depth_div: 3, 2, 6;
    nmp_eval_div: 200, 80, 400;
    lmr_base: 77, 30, 150;        // x100
    lmr_div: 236, 150, 400;       // x100
    lmr_hist_div: 8192, 2048, 32768;
    lmp_base: 3, 1, 8;
    fut_base: 90, 20, 250;
    fut_mult: 100, 40, 250;
    see_quiet: 60, 20, 150;
    see_noisy: 25, 5, 100;
    asp_delta: 20, 8, 60;
    hist_mult: 300, 100, 600;
    hist_max: 2400, 800, 4000;
    se_depth: 8, 5, 12;
    se_double_margin: 20, 5, 60;
    qs_see: 0, -100, 100;
    tm_soft_pct: 60, 30, 120;     // soft limit as % of base allocation
    tm_hard_mult: 4, 2, 8;
    // Search additions measured on the fleet (forge/tests.json); 0 turns each off.
    tm_nodes: 1, 0, 1;            // scale the soft limit by best-move effort and stability (+15.5 Elo)
    tm_node_base: 150, 100, 250;  // x100
    tm_node_mult: 135, 50, 250;   // x100
    corr_pawn: 128, 0, 256;       // pawn-structure eval correction, 128 = full weight (+29.4 Elo)
    razor_margin: 0, 0, 600;
    qs_fut_margin: 0, 0, 400;
    lmr_deeper: 1, 0, 1;          // re-search deeper or shallower after a reduced search fails high (+4.4 Elo)
    probcut_margin: 0, 0, 400;
    mopup: 1, 0, 1;               // drive a bare king to the edge (see eval::mop_up); passed as not worse, converts KBNK
    corr_np: 128, 0, 256;         // eval correction by each side's pieces other than pawns (+7.9 Elo)
    corr_cont: 0, 0, 256;         // eval correction by the previous move
    hist_prune: 0, 0, 8000;       // skip quiet moves whose history is below -this x (reduced depth + 1)
    tt_hist: 0, 0, 1;             // a table cutoff by a quiet move rewards that move's history
    prior_bonus: 1, 0, 1;         // a node that fails low rewards the opponent's quiet move that led to it (+5.2 Elo)
    eval_hist: 0, 0, 40;          // history of the opponent's quiet move learns from how the eval changed after it
    lmr_ttcap: 0, 0, 1;           // reduce quiet moves one more ply when the table's move is a capture
    cont4: 0, 0, 1;               // continuation history also keyed by our own move two turns ago
    corr_joint: 0, 0, 1;          // correction tables learn jointly (each toward what the others leave), not each the whole error
    smp_vote: 0, 0, 1;            // experimental: choose a move using completed searches from every worker
    cap_checks: 0, 0, 1;          // experimental: order losing checking captures before quiet moves
}
