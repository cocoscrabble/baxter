//! The "basic" strategies: King/Queen of the Hill and the two random pairings.
//! (The round-robin family lives in `roundrobin.rs`.)

use crate::rng::shuffle;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairings, Player};

use super::{pair_no_repeats_blossom, Ctx};

/// Pair consecutive standings: 1-2, 3-4, … (King of the Hill).
pub fn pair_koth(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let standings = ctx.standings(rp.start_round);
    chunk_pairs(&standings)
}

/// Queen of the Hill: 1-3, 2-4 within each group of four (with a special
/// last-six handling when the field is 4n+2).
pub fn pair_qoth(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let s = ctx.standings(rp.start_round);
    let n = s.len();
    if n < 4 {
        // Too few players for Queen-of-the-Hill groups of four; fall back to KotH.
        return chunk_pairs(&s);
    }
    let mut out = Pairings::new();
    if n % 4 == 2 {
        let last = n - 6;
        let mut i = 0;
        while i < last {
            out.add(s[i].clone(), s[i + 2].clone());
            out.add(s[i + 1].clone(), s[i + 3].clone());
            i += 4;
        }
        // Pair the trailing six 1-4, 2-5, 3-6.
        out.add(s[last].clone(), s[last + 3].clone());
        out.add(s[last + 1].clone(), s[last + 4].clone());
        out.add(s[last + 2].clone(), s[last + 5].clone());
    } else {
        let mut i = 0;
        while i < n {
            out.add(s[i].clone(), s[i + 2].clone());
            out.add(s[i + 1].clone(), s[i + 3].clone());
            i += 4;
        }
    }
    out
}

/// Shuffle the standings and pair consecutively.
pub fn pair_random(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let mut standings = ctx.standings(rp.start_round);
    shuffle(ctx.rng, &mut standings);
    chunk_pairs(&standings)
}

/// Random pairing that minimizes repeat opponents via blossom matching. The
/// very first round (no results yet) falls back to a plain random pairing.
pub fn pair_random_no_repeats(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    if rp.start_round < 1 {
        return pair_random(ctx, rp);
    }
    let players = ctx.standings(rp.start_round);
    pair_no_repeats_blossom(&players, ctx.repeats, ctx.rng)
}

// --- helpers ---------------------------------------------------------------

/// Pair standings two at a time (1-2, 3-4, …). A trailing odd player (which
/// should not occur — odd fields get a bye upstream) is dropped.
fn chunk_pairs(standings: &[Player]) -> Pairings {
    let mut out = Pairings::new();
    for chunk in standings.chunks(2) {
        if chunk.len() == 2 {
            out.add(chunk[0].clone(), chunk[1].clone());
        }
    }
    out
}
