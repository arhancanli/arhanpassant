//! Monotonic clock that also works in browsers. On wasm32 the host provides
//! `env.ap_now_ms()` (for example `performance.now()`), since std's Instant
//! is unavailable there.

#[cfg(not(target_arch = "wasm32"))]
pub use std::time::Instant;

#[cfg(target_arch = "wasm32")]
pub use wasm::Instant;

#[cfg(target_arch = "wasm32")]
mod wasm {
    use std::time::Duration;

    #[link(wasm_import_module = "env")]
    extern "C" {
        fn ap_now_ms() -> f64;
    }

    #[derive(Copy, Clone, Debug, PartialEq, PartialOrd)]
    pub struct Instant(f64);

    impl Instant {
        pub fn now() -> Instant {
            // SAFETY: a plain host function with no arguments.
            Instant(unsafe { ap_now_ms() })
        }

        pub fn duration_since(&self, earlier: Instant) -> Duration {
            Duration::from_secs_f64((self.0 - earlier.0).max(0.0) / 1000.0)
        }

        pub fn elapsed(&self) -> Duration {
            Instant::now().duration_since(*self)
        }
    }

    impl std::ops::Add<Duration> for Instant {
        type Output = Instant;
        fn add(self, d: Duration) -> Instant {
            Instant(self.0 + d.as_secs_f64() * 1000.0)
        }
    }
}
