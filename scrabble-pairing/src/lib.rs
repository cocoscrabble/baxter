//! Pairing engine for Scrabble tournaments.

mod vendor;

pub mod matching;
pub mod model;
pub mod pair;
pub mod rng;
pub mod round_pairing;
pub mod standings;
pub mod strategies;

pub use model::{PairingInput, RoundResult};
pub use pair::pair;

/// Pair a tournament from a JSON string, returning JSON. The serialized I/O
/// boundary (see `model`) - the entry point for non-Rust callers (Python via
/// PyO3, a browser via wasm-bindgen) and the simplest thing to test against a
/// reference implementation.
pub fn pair_json(input: &str) -> Result<String, serde_json::Error> {
    let parsed: model::PairingInput = serde_json::from_str(input)?;
    let result = pair::pair(&parsed);
    serde_json::to_string(&result)
}
