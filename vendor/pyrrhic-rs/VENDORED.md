# pyrrhic-rs 0.2.0 (MIT), vendored

Unchanged except six pointer casts in src/tbprobe.rs (`as *const std::ffi::c_char` at
CStr::from_ptr calls, `as *const i8` for strcpy) so the crate builds where C `char` is
unsigned (ARM Linux). See LICENSE for the original terms.

lib.rs also starts with `#![allow(warnings)]`, as cargo caps lints for registry crates.
