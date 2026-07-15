//! The round-robin family: round robin, double round robin, and Charlottesville.
//!
//! A round robin over E players (E even; a phantom `Bye` is appended for odd
//! fields) is a 1-factorization of K_E: E-1 pairwise-disjoint perfect matchings,
//! one per round. Fixed pairings and played rounds pin edges into particular
//! rounds; the engine finds a schedule honoring all pins (see `rr_block_pairings`).

use std::collections::{HashMap, HashSet};

use crate::round_pairing::RoundPairing;
use crate::standings::{Pairings, Player, BYE_NAME};

use super::{guard_no_dropped_in_block, Ctx};

/// Round robin, honoring fixed pairings by permuting which round template lands
/// in which round. Always seeds from the initial seedings (round 0), never the
/// start-round standings — a round-robin block rotates off a fixed ordering.
pub fn pair_round_robin(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    rr_block_pairings(ctx, rp, 1)
}

/// Double round robin: each round template spans two consecutive calendar rounds
/// (k=2).
pub fn pair_double_round_robin(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    rr_block_pairings(ctx, rp, 2)
}

/// Seeding order with a `Bye` appended for odd fields, so the rotation runs over
/// an even number of players. Players keep their seeding order; what varies is
/// which round template lands in which calendar round (see `rr_permutation`).
fn rr_players(ctx: &Ctx) -> Vec<Player> {
    let mut players = ctx.standings(0);
    if !players.len().is_multiple_of(2) {
        players.push(Player::bye());
    }
    players
}

/// Canonical (sorted) key for an unordered name pair.
fn canon(a: &str, b: &str) -> (String, String) {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

/// Map each unordered name-pair to the (unique) round-robin template it appears
/// in, over `players` (even count). Template `t` is `pair_rr(n, t)`.
fn rr_template_of_pair(players: &[Player]) -> HashMap<(String, String), usize> {
    let n = players.len();
    let mut map = HashMap::new();
    for t in 0..(n - 1) {
        let (h1, h2) = pair_rr(n, t as i32);
        for i in 0..(n / 2) {
            map.insert(canon(&players[h1[i]].name, &players[h2[i]].name), t);
        }
    }
    map
}

/// The template index a played round used, from its (non-bye) game pairs. Every
/// game in a round belongs to the same template, so any one pins it; we check
/// they agree (else the field changed under a played round).
fn identify_template(
    pairset: &HashSet<(String, String)>,
    template_of_pair: &HashMap<(String, String), usize>,
) -> Result<usize, String> {
    let mut indices: HashSet<usize> = HashSet::new();
    for pair in pairset {
        match template_of_pair.get(pair) {
            Some(t) => {
                indices.insert(*t);
            }
            None => {
                return Err("A played round no longer matches the round-robin \
                            schedule (did the entrants change?)."
                    .to_string())
            }
        }
    }
    if indices.len() != 1 {
        return Err(
            "A played round is not a valid round-robin round; cannot place \
                    fixed pairings around it."
                .to_string(),
        );
    }
    Ok(indices.into_iter().next().unwrap())
}

/// Move template `t` to `position` by transposing it with whatever sits there.
/// A locked position (played, or set by an earlier fixed pairing) can't move.
fn place_template(
    assign: &mut [usize],
    where_: &mut [usize],
    locked: &mut HashSet<usize>,
    position: usize,
    t: usize,
    err: &str,
) -> Result<(), String> {
    if assign[position] == t {
        locked.insert(position);
        return Ok(());
    }
    if locked.contains(&position) || locked.contains(&where_[t]) {
        return Err(err.to_string());
    }
    let other = where_[t];
    let t_old = assign[position];
    assign[position] = t;
    where_[t] = position;
    assign[other] = t_old;
    where_[t_old] = other;
    locked.insert(position);
    Ok(())
}

/// Bijection `position -> template index` for one round-robin block, where a
/// position is a round (or round-pair, for double round robin) in the block.
/// Start from the identity; pin each played position to the template it used
/// (a fixed point); then move each fixed pairing's template to its position.
/// Deterministic, so per-round callers recompute the same bijection.
fn rr_permutation(
    num_positions: usize,
    template_of_pair: &HashMap<(String, String), usize>,
    played: &HashMap<usize, usize>,
    fixed: &[(usize, (String, String))],
) -> Result<Vec<usize>, String> {
    let mut assign: Vec<usize> = (0..num_positions).collect();
    let mut where_: Vec<usize> = (0..num_positions).collect();
    let mut locked: HashSet<usize> = HashSet::new();

    let mut positions: Vec<usize> = played.keys().copied().collect();
    positions.sort_unstable();
    for position in positions {
        place_template(
            &mut assign,
            &mut where_,
            &mut locked,
            position,
            played[&position],
            "Played round-robin rounds conflict with the fixed pairings.",
        )?;
    }
    for (position, pair) in fixed {
        let t = template_of_pair
            .get(pair)
            .ok_or_else(|| "A fixed pairing names players not in this round robin.".to_string())?;
        place_template(
            &mut assign,
            &mut where_,
            &mut locked,
            *position,
            *t,
            "Fixed pairings conflict: cannot place all of them in their requested rounds.",
        )?;
    }
    Ok(assign)
}

/// Round-robin family pairing for one calendar round, honoring fixed pairings by
/// permuting which template lands in which round (`k` calendar rounds per
/// template: 1 for round robin, 2 for double round robin).
fn rr_block_pairings(ctx: &Ctx, rp: &RoundPairing, k: i32) -> Result<Pairings, String> {
    let players = rr_players(ctx);
    let num_positions = players.len() - 1;
    let template_of_pair = rr_template_of_pair(&players);

    let block_rounds: HashSet<i32> = ctx
        .round_pairings
        .iter()
        .filter(|o| o.pairing == rp.pairing && o.start_round == rp.start_round)
        .map(|o| o.round)
        .collect();
    guard_no_dropped_in_block(ctx, &block_rounds, "round-robin", "round robins")?;

    let position_of = |round: i32| ((round - rp.start_round) / k) as usize;

    // A round robin over E players has exactly E-1 rounds (num_positions
    // templates); a double round robin has 2*(E-1). A block with more rounds
    // than that has no template for the overflow round — fail clearly instead of
    // indexing past the rotation.
    if position_of(rp.round) >= num_positions {
        let max_rounds = num_positions * k as usize;
        return Err(format!(
            "This round robin has only {max_rounds} round{} for the current \
             field; round {} is beyond the rotation — shorten the block or add \
             players.",
            if max_rounds == 1 { "" } else { "s" },
            rp.round,
        ));
    }

    // Played rounds become fixed points, identified from their recorded games.
    let mut played_pairs: HashMap<usize, HashSet<(String, String)>> = HashMap::new();
    for s in ctx.slips {
        if block_rounds.contains(&s.round)
            && !s.winner_name.eq_ignore_ascii_case(BYE_NAME)
            && !s.loser_name.eq_ignore_ascii_case(BYE_NAME)
        {
            played_pairs
                .entry(position_of(s.round))
                .or_default()
                .insert(canon(&s.winner_name, &s.loser_name));
        }
    }
    let mut played: HashMap<usize, usize> = HashMap::new();
    for (position, pairset) in &played_pairs {
        played.insert(*position, identify_template(pairset, &template_of_pair)?);
    }

    let mut fixed: Vec<(usize, (String, String))> = Vec::new();
    for (round, pairs) in ctx.fixed_pairings {
        if block_rounds.contains(round) {
            for (a, b) in pairs {
                fixed.push((position_of(*round), canon(a, b)));
            }
        }
    }
    fixed.sort_by(|x, y| x.0.cmp(&y.0).then_with(|| x.1.cmp(&y.1)));

    let assign = rr_permutation(num_positions, &template_of_pair, &played, &fixed)?;

    let t = assign[position_of(rp.round)];
    Ok(pair_rr_into(&players, players.len(), t as i32))
}

/// Charlottesville: split the field into two snaking groups and rotate one group
/// against the other. An odd field gets a bye player so the two groups are equal;
/// whoever is drawn against the bye sits the round out.
pub fn pair_charlottesville(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    let mut seeding = ctx.standings(0);
    if !seeding.len().is_multiple_of(2) {
        seeding.push(Player::bye());
    }
    let mut g1: Vec<usize> = Vec::new();
    let mut g2: Vec<usize> = Vec::new();
    for i in 0..seeding.len() {
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

// --- helpers ---------------------------------------------------------------

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
