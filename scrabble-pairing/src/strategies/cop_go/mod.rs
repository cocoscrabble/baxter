//! COP, re-ported from liwords' Go implementation (`plans/PLAN_COP_GO_PORT.md`).
//! Under construction beside `cop.rs`; it replaces it at stage 5.

pub mod cephes;
pub mod cop;
pub mod factor3;
pub mod matching;
pub mod precomp;
pub mod rand;
pub mod request;
pub mod score_diffs;
pub mod standings;
pub mod trace;
pub mod verify;
