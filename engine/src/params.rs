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
}
