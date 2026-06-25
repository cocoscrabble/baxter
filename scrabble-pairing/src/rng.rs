//! Seeded RNG helpers. We use `ChaCha8Rng` seeded purely from the caller's
//! input (never OS entropy), so the engine is deterministic and the crate stays
//! wasm-clean (no `getrandom`). We only need `RngCore::next_u64`, so we
//! implement the two primitives we use directly rather than pulling in `rand`.

use rand_chacha::ChaCha8Rng;
use rand_core::{RngCore, SeedableRng};

/// Build the engine's RNG from a seed.
pub fn seeded(seed: u64) -> ChaCha8Rng {
    ChaCha8Rng::seed_from_u64(seed)
}

/// A pseudo-random integer in `[0, n)`. `n` must be > 0. The modulo bias is
/// irrelevant here — the RNG only breaks ties between equally-good pairings.
pub fn below(rng: &mut ChaCha8Rng, n: u64) -> u64 {
    rng.next_u64() % n
}

/// In-place Fisher-Yates shuffle.
pub fn shuffle<T>(rng: &mut ChaCha8Rng, v: &mut [T]) {
    if v.len() < 2 {
        return;
    }
    for i in (1..v.len()).rev() {
        let j = below(rng, i as u64 + 1) as usize;
        v.swap(i, j);
    }
}
