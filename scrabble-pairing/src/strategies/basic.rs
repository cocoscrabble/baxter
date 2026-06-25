//! The "basic" strategies: King/Queen of the Hill, round robin, double round
//! robin, Charlottesville, and the two random pairings.

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

/// Round-robin pairing for game `pos` of the rotation. Always seeds from the
/// initial seedings (round 0), never the start-round standings — a round-robin
/// block rotates off a fixed ordering and ignores results.
pub fn pair_round_robin(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let standings = ctx.standings(0);
    let n = standings.len();
    let pos = rp.round - rp.start_round;
    pair_rr_into(&standings, n, pos)
}

/// Double round robin: consecutive pairs of rounds share one round-robin game.
pub fn pair_double_round_robin(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let standings = ctx.standings(0);
    let n = standings.len();
    let pos = (rp.round - rp.start_round) / 2;
    pair_rr_into(&standings, n, pos)
}

/// Charlottesville: split the field into two snaking groups and rotate one group
/// against the other.
pub fn pair_charlottesville(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let seeding = ctx.standings(0);
    let mut g1: Vec<usize> = Vec::new();
    let mut g2: Vec<usize> = Vec::new();
    for i in 0..ctx.players.len() {
        if i % 4 == 1 || i % 4 == 3 {
            g1.push(i);
        } else {
            g2.push(i);
        }
    }
    g2.reverse();
    let mut out = Pairings::new();
    if g2.is_empty() {
        return out;
    }
    let pos = (rp.round - rp.start_round).rem_euclid(g2.len() as i32) as usize;
    // rotated = g2[pos:] + g2[:pos]
    let rotated: Vec<usize> = g2[pos..].iter().chain(g2[..pos].iter()).copied().collect();
    for (i, &p1) in g1.iter().enumerate() {
        if let (Some(a), Some(b)) = (
            seeding.get(p1),
            rotated.get(i).and_then(|&p2| seeding.get(p2)),
        ) {
            out.add(a.clone(), b.clone());
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

/// Build the two index halves for round-robin game `r` over `n` players, then
/// pair `standings[h1[i]]` against `standings[h2[i]]`.
fn pair_rr_into(standings: &[Player], n: usize, r: i32) -> Pairings {
    let (h1, h2) = pair_rr(n, r);
    let mut out = Pairings::new();
    for i in 0..(n / 2) {
        out.add(standings[h1[i]].clone(), standings[h2[i]].clone());
    }
    out
}

/// Round-robin rotation indices for game `r`, including a defined wrap-around
/// when `r` exceeds the natural range.
fn pair_rr(n: usize, r: i32) -> (Vec<usize>, Vec<usize>) {
    // init = [1, 2, ..., n-1]
    let init: Vec<usize> = (1..n).collect();
    let h = n / 2;
    let start = n as i32 - 1 - r;
    // Split point for init[0:start] / init[start:], honoring a negative `start`
    // (counts from the end) and clamping out-of-range values.
    let l = init.len() as i32;
    let split = if start >= 0 {
        start.min(l)
    } else {
        (l + start).max(0)
    } as usize;
    let r1 = &init[..split];
    let r2 = &init[split..];
    // rotated = [0] + r2 + r1
    let mut rotated: Vec<usize> = Vec::with_capacity(n);
    rotated.push(0);
    rotated.extend_from_slice(r2);
    rotated.extend_from_slice(r1);
    let h1: Vec<usize> = rotated[..h].to_vec();
    let h2: Vec<usize> = rotated[h..].iter().rev().copied().collect();
    (h1, h2)
}
