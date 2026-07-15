//! Quad and sixes strategies. Players are split into small groups (4 or 6) that
//! play an internal round robin over a few rounds.
//!
//! These strategies assume an even field that is `4n` or `4n+2` (quads) / a
//! valid sixes size. A malformed field yields no pairings rather than panicking.

use std::collections::HashSet;

use crate::round_pairing::RoundPairing;
use crate::standings::{Pairings, Player};

use super::{guard_no_dropped_in_block, Ctx};

// Quad pairings for four players 0-3, indexed by round position.
const PAIRINGS4: [[[usize; 2]; 2]; 3] = [[[0, 3], [1, 2]], [[0, 2], [1, 3]], [[0, 1], [2, 3]]];

// Incomplete round robin for six players 0-5, indexed by round position.
const PAIRINGS6: [[[usize; 2]; 3]; 3] = [
    [[0, 1], [2, 3], [4, 5]],
    [[0, 2], [3, 4], [1, 5]],
    [[0, 3], [1, 4], [2, 5]],
];

/// Start-round standings for a quad/sixes round, with a bye appended for an odd
/// field so it divides into whole quads/hexes. Whoever is grouped with the bye
/// sits the round out (the bye follows the strategy's own distribution).
fn quad_standings(ctx: &Ctx, rp: &RoundPairing) -> Result<Vec<Player>, String> {
    let block_rounds: HashSet<i32> = ctx
        .round_pairings
        .iter()
        .filter(|o| o.pairing == rp.pairing && o.start_round == rp.start_round)
        .map(|o| o.round)
        .collect();
    guard_no_dropped_in_block(ctx, &block_rounds, "quad block", "quad blocks")?;
    let mut standings = ctx.standings(rp.start_round);
    if !standings.len().is_multiple_of(2) {
        standings.push(Player::bye());
    }
    Ok(standings)
}

/// 0-based position of `rp` within its run of same-strategy, same-start_round
/// entries — i.e. which game of the quad rotation this round is.
fn quad_position(rp: &RoundPairing, round_pairings: &[RoundPairing]) -> usize {
    let mut pos = 0;
    for r in round_pairings {
        if r.pairing == rp.pairing && r.start_round == rp.start_round {
            if r.round == rp.round {
                return pos;
            }
            pos += 1;
        }
    }
    0
}

/// Add the pairings for one round position across every group.
fn pair_groups_at_position(groups: &[Vec<Player>], pos: usize) -> Pairings {
    let mut out = Pairings::new();
    for group in groups {
        let pairs: &[[usize; 2]] = if group.len() == 4 {
            match PAIRINGS4.get(pos) {
                Some(p) => p,
                None => continue,
            }
        } else {
            match PAIRINGS6.get(pos) {
                Some(p) => p,
                None => continue,
            }
        };
        for &[a, b] in pairs {
            if let (Some(pa), Some(pb)) = (group.get(a), group.get(b)) {
                out.add(pa.clone(), pb.clone());
            }
        }
    }
    out
}

/// Index up to which the field divides into whole quads (`None` if the field
/// size is not a valid quad configuration).
fn last_quad_position(n: usize) -> Option<usize> {
    match n % 4 {
        0 => Some(n),
        2 => n.checked_sub(6),
        _ => None,
    }
}

/// Index up to which the field divides into whole hexes (`None` if invalid).
fn last_hex_position(n: usize) -> Option<usize> {
    match n % 6 {
        0 => Some(n),
        2 => n.checked_sub(8),
        4 => n.checked_sub(4),
        _ => None,
    }
}

/// Append a trailing hex group (the leftover six players) if present.
fn maybe_add_hex(quads: &mut Vec<Vec<Player>>, standings: &[Player], last_quad: usize) {
    if last_quad < standings.len() {
        quads.push(standings[last_quad..].to_vec());
    }
}

/// Append trailing quad group(s) after the hexes (8 leftover -> two quads, 4 -> one).
fn maybe_add_quads(hexes: &mut Vec<Vec<Player>>, standings: &[Player], last_hex: usize) {
    let diff = standings.len() - last_hex;
    if diff == 8 {
        hexes.push(standings[last_hex..last_hex + 4].to_vec());
        hexes.push(standings[last_hex + 4..].to_vec());
    } else if diff == 4 {
        hexes.push(standings[last_hex..last_hex + 4].to_vec());
    }
}

pub fn pair_clustered_quads(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let pos = quad_position(rp, ctx.round_pairings);
    let standings = quad_standings(ctx, rp)?;
    let last_quad = last_quad_position(standings.len())
        .ok_or_else(|| "field too small for quads".to_string())?;
    let mut quads: Vec<Vec<Player>> = Vec::new();
    let mut i = 0;
    while i < last_quad {
        quads.push(standings[i..i + 4].to_vec());
        i += 4;
    }
    maybe_add_hex(&mut quads, &standings, last_quad);
    Ok(pair_groups_at_position(&quads, pos))
}

pub fn pair_distributed_quads(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let pos = quad_position(rp, ctx.round_pairings);
    let standings = quad_standings(ctx, rp)?;
    let last_quad = last_quad_position(standings.len())
        .ok_or_else(|| "field too small for quads".to_string())?;
    let stride = last_quad / 4;
    let mut quads: Vec<Vec<Player>> = vec![Vec::new(); stride];
    for (i, p) in standings.iter().take(last_quad).enumerate() {
        quads[i % stride].push(p.clone());
    }
    maybe_add_hex(&mut quads, &standings, last_quad);
    Ok(pair_groups_at_position(&quads, pos))
}

pub fn pair_equalized_quads(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let pos = quad_position(rp, ctx.round_pairings);
    let standings = quad_standings(ctx, rp)?;
    let last_quad = last_quad_position(standings.len())
        .ok_or_else(|| "field too small for quads".to_string())?;
    let stride = last_quad / 4;
    let new_standings = snake(&standings, last_quad, stride);
    let mut quads: Vec<Vec<Player>> = vec![Vec::new(); stride];
    for (i, p) in new_standings.iter().take(last_quad).enumerate() {
        quads[i % stride].push(p.clone());
    }
    maybe_add_hex(&mut quads, &standings, last_quad);
    Ok(pair_groups_at_position(&quads, pos))
}

pub fn pair_sixes(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let pos = quad_position(rp, ctx.round_pairings);
    let standings = quad_standings(ctx, rp)?;
    let last_hex = last_hex_position(standings.len())
        .ok_or_else(|| "field too small for sixes".to_string())?;
    let stride = last_hex / 6;
    let new_standings = snake(&standings, last_hex, stride);
    let mut hexes: Vec<Vec<Player>> = vec![Vec::new(); stride];
    for (i, p) in new_standings.iter().take(last_hex).enumerate() {
        hexes[i % stride].push(p.clone());
    }
    maybe_add_quads(&mut hexes, &standings, last_hex);
    Ok(pair_groups_at_position(&hexes, pos))
}

/// Snake-reorder the first `count` players in chunks of `stride`, flipping every
/// other chunk, so opponent-seed sums end up roughly equal.
fn snake(standings: &[Player], count: usize, stride: usize) -> Vec<Player> {
    let mut out: Vec<Player> = Vec::with_capacity(count);
    let mut flip = false;
    let mut i = 0;
    while i < count {
        let end = (i + stride).min(standings.len());
        let mut chunk: Vec<Player> = standings[i..end].to_vec();
        if flip {
            chunk.reverse();
        }
        flip = !flip;
        out.extend(chunk);
        i += stride;
    }
    out
}
