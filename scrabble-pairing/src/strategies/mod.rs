//! The pairing strategies and the context they run against.
//!
//! Each strategy is a function `(&mut Ctx, &RoundPairing) -> Pairings`. `Ctx`
//! bundles the immutable tournament data plus the seeded RNG that the strategies
//! read from.

pub mod basic;
pub mod quads;
pub mod swiss;

use std::collections::{HashMap, HashSet};

use rand_chacha::ChaCha8Rng;
use rand_core::RngCore;

use crate::matching::max_weight_matching_pairs;
use crate::model::{PlayerData, ResultSlipData};
use crate::round_pairing::RoundPairing;
use crate::standings::{standings_after_round, Pairing, Pairings, Player, Repeats};

/// Fixed-point scale for the random tiebreak in `pair_no_repeats_blossom`. The
/// matching takes `i128` weights, so a fractional random tiebreak is expressed as
/// `rng_int in [0, REPEAT_SCALE)`: repeats dominate the weight, the random
/// integer only separates otherwise-equal candidate pairs.
const REPEAT_SCALE: i128 = 1_000_000;

/// Everything a strategy needs: the tournament data, the set of names excluded
/// this round (fixed players), the fixed pairings by round (read directly by the
/// round-robin strategies, which permute rounds rather than excluding players),
/// the running repeat counts, and the RNG.
pub struct Ctx<'a> {
    pub players: &'a [PlayerData],
    pub slips: &'a [ResultSlipData],
    pub round_pairings: &'a [RoundPairing],
    pub excluded: &'a HashSet<String>,
    pub fixed_pairings: &'a HashMap<i32, Vec<(String, String)>>,
    pub repeats: &'a Repeats,
    pub rng: &'a mut ChaCha8Rng,
}

impl Ctx<'_> {
    /// Standings after `round`, with byes and excluded players filtered out.
    pub fn standings(&self, round: i32) -> Vec<Player> {
        standings_after_round(self.players, self.slips, round, self.excluded)
    }
}

/// Blossom matching that minimizes repeat opponents, with a random tiebreak.
pub fn pair_no_repeats_blossom(
    players: &[Player],
    repeats: &Repeats,
    rng: &mut ChaCha8Rng,
) -> Pairings {
    let n = players.len();
    let mut edges: Vec<(usize, usize, i128)> = Vec::new();
    // Iterate every ordered pair but only act on the name-ascending one — this
    // keeps one edge (and one RNG draw) per pair, in a deterministic order.
    for i in 0..n {
        for j in 0..n {
            if players[i].name < players[j].name {
                let reps =
                    repeats.get(&Pairing::new(players[i].clone(), players[j].clone())) as i128;
                let tiebreak = (rng.next_u64() % REPEAT_SCALE as u64) as i128;
                let weight = -(10 * reps * REPEAT_SCALE + tiebreak);
                edges.push((i, j, weight));
            }
        }
    }
    let mut out = Pairings::new();
    for (v1, v2) in max_weight_matching_pairs(n, &edges) {
        out.add(players[v1].clone(), players[v2].clone());
    }
    out
}
