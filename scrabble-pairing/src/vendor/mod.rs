//! Code vendored from [rustworkx-core](https://github.com/Qiskit/rustworkx)
//! 0.18.0, licensed under Apache-2.0.
//!
//! We vendor only the maximum-weight-matching algorithm (and its tiny
//! `dictmap` helper) rather than depending on the published `rustworkx-core`
//! crate, because that crate hard-depends on `rayon`, `ndarray`, and
//! `getrandom` with no feature gates — none of which compile to
//! `wasm32-unknown-unknown`. The matching module itself needs only `std`,
//! `hashbrown`, `petgraph`, `indexmap`, and `foldhash`, all of which are
//! wasm-clean. See `ATTRIBUTION.md` for details and the upstream license.

// Vendored third-party code: keep it verbatim, don't warn on unused helpers.
#![allow(dead_code)]

pub mod dictmap;
pub mod max_weight_matching;
