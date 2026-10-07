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
    malus_mult: 300, 100, 800;
    malus_max: 2400, 800, 6000;
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
    corr_cont: 128, 0, 256;         // eval correction by the previous move
    hist_prune: 0, 0, 8000;       // skip quiet moves whose history is below -this x (reduced depth + 1)
    tt_hist: 0, 0, 1;             // a table cutoff by a quiet move rewards that move's history
    prior_bonus: 1, 0, 1;         // a node that fails low rewards the opponent's quiet move that led to it (+5.2 Elo)
    eval_hist: 0, 0, 40;          // history of the opponent's quiet move learns from how the eval changed after it
    lmr_ttcap: 0, 0, 1;           // reduce quiet moves one more ply when the table's move is a capture
    cont4: 0, 0, 1;               // continuation history also keyed by our own move two turns ago
    corr_joint: 1, 0, 1;          // correction tables learn jointly (each toward what the others leave), not each the whole error
    smp_vote: 0, 0, 1;            // experimental: choose a move using completed searches from every worker
    threat_hist: 1, 0, 1;         // quiet history learns separately when a move leaves or enters an attacked square
    pawn_hist: 1, 0, 1;           // quiet history per pawn structure
    threat_order: 100, 0, 300;      // order quiets that rescue a piece attacked by a cheaper one first (% of base values)
    check_order: 16384, 0, 65536;     // ordering bonus for a quiet move that gives check and does not lose material
    fh_blend: 0, 0, 1;            // blend a fail-high score toward beta by depth
    lmr_cutoff: 0, 0, 1;          // reduce one more ply when the children keep failing high (cutoff count > 2)
    lmr_capt: 0, 0, 32768;        // captures: reduce by capture history / this (0 = off)
    se_neg: 0, 0, 1;              // stronger negative singular extensions (-2 for table score >= beta, -2 at cut nodes)
    fut_hist: 0, 0, 32768;        // futility/LMP/SEE pruning depth also counts quiet history / this (0 = off)
    improving_v2: 0, 0, 1;        // "improving" falls back to four plies back when two plies back was in check
    tm_falling: 0, 0, 4000;       // time: scale the soft limit by how far the score fell / this (0 = off)
    see_eval: 190, 100, 300;      // evaluation units per 100 SEE units (a pawn): converts margins between the two
    qs_lmp: 0, 0, 8;              // quiescence: after this many moves, skip captures that are not recaptures, checks or promotions (0 = off)
    corr_minor: 128, 0, 256;        // eval correction by the knights and bishops of both sides (0 = off)
    draw_jitter: 0, 0, 1;         // search draws score -1 or +1 by node count
    mvv_mult: 16, 4, 40;          // capture ordering: weight of the captured piece's value against capture history
    hindsight: 0, 0, 600;         // reduced node: +1 ply if the opponent's position did not worsen, -1 if both evals sum above this (0 = off)
    lmr_corr: 0, 0, 1000;         // reduce less by |eval correction| / this (0 = off)
    lmr_pv: 0, 0, 1;              // reduce late moves from the second move at PV nodes too (not the root)
    upcoming_rep: 1, 0, 1;        // a side that can force a repetition scores at least a draw (cuckoo tables)
    tt_cut_node: 0, 0, 1;         // table cutoffs at depth <= 5 only where the node type agrees with the bound
    nmp_cutnode: 0, 0, 1;         // null-move pruning only at expected cut nodes
    cap_fut: 250, 0, 800;           // capture futility base margin at reduced depth < 7 (0 = off)
    see_capt_hist: 0, 0, 512;     // capture SEE pruning threshold loosened by capture history / this (0 = off)
    smp_skip: 0, 0, 1;            // helper threads stagger their iteration depths
    see_q_check: 0, 0, 1;         // quiet SEE pruning spares moves that give check (sacrifices with check)
    w_butterfly: 64, 16, 160;     // quiet history weights in 64ths: butterfly,
    w_pawn: 64, 0, 160;           //   pawn structure,
    w_cont1: 64, 16, 160;         //   one-ply continuation,
    w_cont2: 64, 0, 160;          //   two-ply continuation
    se_beta_mult: 16, 6, 48;      // singular beta = table score - depth * this / 16
    lmr_ttpv: 0, 0, 2;            // table-PV nodes: reduce one ply less when the table score beats alpha (2: and one more when its depth covers this node)
    alpha_red: 2, 0, 3;           // after a move raises alpha (no cutoff), search the remaining moves this many plies shallower (depth 3-13; 2: +13.1 Elo, 1: +1.6)
    triple_ext: 0, 0, 200;        // singular tt quiet move failing this far below the double-extension margin extends 3 plies (0 = off)
    se_limit: 0, 0, 1;            // singular extensions only below twice the root depth in plies
    qs_fh_blend: 0, 0, 1;         // quiescence fail-highs return the midpoint of the score and beta
    iir_all: 0, 0, 1;             // internal iterative reduction at every node type, not only PV and expected cut nodes
    low_ply: 0, 0, 256;           // quiet ordering: history of moves at the first four plies of this search, weight in 16ths (0 = off)
    dext_limit: 0, 0, 16;         // at most this many double or triple extensions on the path from the root (0 = no limit)
    iir_pv: 0, 0, 3;              // PV nodes without a table move reduce this many plies more than other nodes (internal iterative reduction)
    lmr_cut: 1, 0, 3;             // extra reduction at expected cut nodes
    post_lmr: 0, 0, 1;            // after a reduced quiet move is re-searched at full depth, its continuation history learns the result
    corr_major: 0, 0, 256;        // eval correction by the rooks and queens of both sides (0 = off)
    corr_cont2: 0, 0, 256;        // eval correction by the last two moves together (0 = off)
    qs_evasion_see: 0, 0, 1;      // quiescence in check: once an evasion has saved the position, skip evasions that lose material
    mat_scale: 0, 0, 1;           // scale the network's evaluation by (base + material) / 32768, material = 450 per minor, 650 per rook, 1250 per queen
    mat_scale_base: 26500, 16000, 32768;
}
