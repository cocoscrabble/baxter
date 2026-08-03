//! Swiss pairing. Players are grouped by win count; within the top group we pick
//! the lowest-repeat, smallest-rating-distance opponents via blossom matching,
//! peel off that group, and repeat.

use std::collections::VecDeque;

use indexmap::IndexMap;

use crate::matching::max_weight_matching_pairs;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairing, Pairings, Player, Repeats};

use super::{pair_no_repeats_blossom, Ctx};

const SWISS_DISTANCE: usize = 10;
/// Don't pair candidates more than this many rating-rank places apart.
const MAX_DISTANCE: i32 = 11;

// Deterministic tie-break for the blossom matching. Equally-good pairings (same
// repeats, same total seed-distance) admit many max-weight matchings; perturbing
// each edge by a well-mixed per-edge value makes the maximum (almost surely)
// unique so the Python and Rust engines pick the same one. The primary objective
// is scaled up by WEIGHT_SCALE so the perturbation never overrides it; the
// per-edge value is a splitmix64 hash of the canonical (min, max) vertex pair,
// bit-for-bit identical to the Python engine's.
const TIEBREAK_MOD: u64 = 1 << 40;
const WEIGHT_SCALE: i128 = 1 << 52;

fn match_tiebreak(a: usize, b: usize) -> i128 {
    let mut x = ((a as u64) << 20) | (b as u64);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D049BB133111EB);
    x ^= x >> 31;
    (x % TIEBREAK_MOD) as i128
}

/// Win-count groups, highest first, each an ordered queue of players.
struct Groups {
    groups: VecDeque<VecDeque<Player>>,
}

impl Groups {
    fn from_standings(standings: &[Player]) -> Groups {
        let max_wins = standings.iter().map(|p| p.wins).max().unwrap_or(0).max(0);
        let mut groups: VecDeque<VecDeque<Player>> =
            (0..=max_wins).map(|_| VecDeque::new()).collect();
        for p in standings {
            groups[p.wins as usize].push_back(p.clone());
        }
        let mut g = Groups { groups };
        g.compact();
        // Reverse so the highest win-count group is at the front.
        g.groups = g.groups.drain(..).rev().collect();
        g.balance();
        g.compact();
        g
    }

    fn length(&self) -> usize {
        self.groups.len()
    }

    fn top(&self) -> &VecDeque<Player> {
        &self.groups[0]
    }

    fn bottom(&self) -> &VecDeque<Player> {
        &self.groups[self.groups.len() - 1]
    }

    fn compact(&mut self) {
        self.groups.retain(|g| !g.is_empty());
    }

    /// Pull a player up from the next group whenever a group has odd size.
    fn balance(&mut self) {
        for i in 0..self.groups.len().saturating_sub(1) {
            if !self.groups[i].len().is_multiple_of(2) {
                if let Some(fst) = self.groups[i + 1].pop_front() {
                    self.groups[i].push_back(fst);
                }
            }
        }
    }

    fn promote(&mut self, i: usize, j: usize) {
        if let Some(fst) = self.groups[j].pop_front() {
            self.groups[i].push_back(fst);
        }
    }

    /// Promote two players into group `i` from the groups just below it.
    fn promote2(&mut self, i: usize) {
        let j = i + 1;
        self.promote(i, j);
        if self.groups.get(j).map(|g| g.is_empty()).unwrap_or(true) {
            self.promote(i, j + 1);
        } else {
            self.promote(i, j);
        }
    }

    fn merge_bottom(&mut self) {
        if self.groups.len() == 1 {
            return;
        }
        let last = self.groups.pop_back().unwrap();
        self.groups.back_mut().unwrap().extend(last);
    }
}

/// A candidate opponent for an anchor player. Ordered by (repeats, distance,
/// name1, name2).
#[derive(PartialEq, Eq, PartialOrd, Ord)]
struct Candidate {
    repeats: i32,
    distance: i32,
    name1: String, // the opponent
    name2: String, // the anchor
}

/// Initial Swiss pairing: top half vs bottom half (1 vs h+1, 2 vs h+2, …).
fn pair_swiss_initial(standings: &[Player]) -> Pairings {
    let mut out = Pairings::new();
    let half = standings.len() / 2;
    for i in 0..half {
        out.add(standings[i].clone(), standings[i + half].clone());
    }
    out
}

/// For each player in the top group, the sorted list of acceptable opponents
/// (those met fewer than `nrep` times).
fn pair_swiss_top(groups: &Groups, repeats: &Repeats, nrep: i32) -> Vec<Vec<Candidate>> {
    let top = groups.top();
    let len = top.len();
    let mut candidates: Vec<Vec<Candidate>> = (0..len).map(|_| Vec::new()).collect();
    for i in 0..len {
        for j in 0..len {
            if i == j {
                continue;
            }
            let reps = repeats.get(&Pairing::new(top[i].clone(), top[j].clone()));
            if reps < nrep {
                candidates[i].push(Candidate {
                    repeats: reps,
                    distance: (i as i32 - j as i32).abs(),
                    name1: top[j].name.clone(),
                    name2: top[i].name.clone(),
                });
            }
        }
    }
    for c in candidates.iter_mut() {
        c.sort();
    }
    candidates
}

/// Match the top-group anchors to opponents via blossom, minimizing repeats and
/// rating distance. Returns the matched (name1, name2) pairs.
fn pair_candidates(bracket: &[Vec<Candidate>]) -> Vec<(String, String)> {
    // Anchor name -> node index (the anchor of bracket[i] is its candidates'
    // shared `name2`). Every node references anchors only.
    let mut names: IndexMap<String, usize> = IndexMap::new();
    let mut inames: Vec<String> = Vec::with_capacity(bracket.len());
    for (i, player_candidates) in bracket.iter().enumerate() {
        let name = player_candidates[0].name2.clone();
        names.insert(name.clone(), i);
        inames.push(name);
    }

    // Dedupe symmetric edges (each pair appears from both anchors) so the graph
    // stays simple (no parallel edges). Keep first-seen order for deterministic
    // matching.
    let mut seen: IndexMap<(usize, usize), i128> = IndexMap::new();
    for player_candidates in bracket {
        for c in player_candidates {
            if c.distance < MAX_DISTANCE {
                let v1 = names[&c.name1];
                let v2 = names[&c.name2];
                let key = if v1 <= v2 { (v1, v2) } else { (v2, v1) };
                let weight = WEIGHT_SCALE * -(30 * c.repeats as i128 + c.distance as i128)
                    + match_tiebreak(key.0, key.1);
                seen.entry(key).or_insert(weight);
            }
        }
    }
    let edges: Vec<(usize, usize, i128)> = seen.into_iter().map(|((a, b), w)| (a, b, w)).collect();

    max_weight_matching_pairs(bracket.len(), &edges)
        .into_iter()
        .map(|(v1, v2)| (inames[v1].clone(), inames[v2].clone()))
        .collect()
}

/// Core Swiss pairing for a list of players (already in standings order).
fn pair_swiss_players(players: &[Player], repeats: &Repeats) -> Pairings {
    let by_name: IndexMap<String, Player> = players
        .iter()
        .map(|p| (p.name.clone(), p.clone()))
        .collect();
    let mut groups = Groups::from_standings(players);
    let mut nrep = 1;
    // Termination guard: once nrep exceeds the field size, raising it further
    // can't unlock more edges.
    let max_nrep = players.len() as i32 + 1;
    let mut paired: Vec<Vec<(String, String)>> = Vec::new();

    // Don't leave too small a bottom group.
    if groups.length() > 1 {
        while groups.bottom().len() < 6 {
            groups.merge_bottom();
            if groups.length() <= 1 {
                break;
            }
        }
    }

    while groups.length() > 0 {
        if nrep > max_nrep {
            break;
        }
        let candidates = pair_swiss_top(&groups, repeats, nrep);
        if candidates.iter().any(|x| x.is_empty()) {
            if groups.length() == 1 {
                nrep += 1;
                continue;
            }
            groups.compact();
            groups.promote2(0);
            groups.compact();
            if groups.length() == 1 {
                nrep += 1;
                continue;
            }
            // Fall through to re-loop with the promoted top group at the same nrep.
        } else {
            let pairs = pair_candidates(&candidates);
            groups.compact();
            if pairs.is_empty() || pairs.len() != candidates.len() / 2 {
                // Couldn't fully pair the top group at this repeat limit.
                nrep += 1;
                continue;
            }
            groups.groups.pop_front();
            paired.push(pairs);
            if groups.length() == 0 {
                break;
            }
        }
    }

    let mut out = Pairings::new();
    for group in &paired {
        for (name1, name2) in group {
            out.add(by_name[name1].clone(), by_name[name2].clone());
        }
    }
    out
}

pub fn pair_swiss(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    if rp.start_round < 1 {
        let seeding = ctx.standings(0);
        return pair_swiss_initial(&seeding);
    }
    let players = ctx.standings(rp.start_round);
    pair_swiss_players(&players, ctx.repeats)
}

/// Swiss pairing with a hard no-repeat constraint.
///
/// Unlike regular Swiss, this considers the full field at once.  Win-group
/// distance is the primary cost and standings distance is secondary; blossom
/// matching then finds the best perfect matching using only opponents who have
/// not met.  Keeping the constraint in the edge set means it can never be
/// silently relaxed.
pub fn pair_swiss_no_repeats(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    if rp.start_round < 1 {
        return Ok(pair_swiss_initial(&ctx.standings(0)));
    }

    let players = ctx.standings(rp.start_round);
    let n = players.len();
    if !n.is_multiple_of(2) {
        return Err(format!(
            "round {} has an odd no-repeat Swiss field after bye assignment",
            rp.round
        ));
    }
    let mut edges: Vec<(usize, usize, i128)> = Vec::new();
    for i in 0..n {
        for j in (i + 1)..n {
            if ctx
                .repeats
                .get(&Pairing::new(players[i].clone(), players[j].clone()))
                > 0
            {
                continue;
            }
            let win_distance = (players[i].wins - players[j].wins).abs() as i128;
            let standings_distance = (j - i) as i128;
            // One win group must dominate every possible standings distance.
            let cost = (n as i128 + 1) * win_distance + standings_distance;
            let weight = -WEIGHT_SCALE * cost + match_tiebreak(i, j);
            edges.push((i, j, weight));
        }
    }

    let pairs = max_weight_matching_pairs(n, &edges);
    if pairs.len() != n / 2 {
        return Err(format!(
            "round {} has no repeat-free Swiss pairing for the remaining field",
            rp.round
        ));
    }

    let mut out = Pairings::new();
    for (i, j) in pairs {
        out.add(players[i].clone(), players[j].clone());
    }
    Ok(out)
}

/// Swiss pairing that minimizes repeats across the entire field.
///
/// The number of repeated games is the primary matching cost, so a repeat-free
/// perfect matching always wins when one exists. Prior meetings (avoiding a
/// third meeting before a second), win-group distance, and standings distance
/// are successive tiebreaks. This differs deliberately from legacy `Swiss`,
/// which pairs one win group at a time and can therefore repeat even when a
/// full-field no-repeat matching exists.
pub fn pair_swiss_min_repeats(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    if rp.start_round < 1 {
        return Ok(pair_swiss_initial(&ctx.standings(0)));
    }

    let players = ctx.standings(rp.start_round);
    let n = players.len();
    if !n.is_multiple_of(2) {
        return Err(format!(
            "round {} has an odd minimal-repeat Swiss field after bye assignment",
            rp.round
        ));
    }

    let min_wins = players.iter().map(|player| player.wins).min().unwrap_or(0);
    let max_wins = players.iter().map(|player| player.wins).max().unwrap_or(0);
    let max_win_distance = (max_wins - min_wins) as i128;
    let pairs_per_round = n as i128 / 2;
    let max_standings_distance = n.saturating_sub(1) as i128;
    let win_distance_scale = pairs_per_round * max_standings_distance + 1;
    let max_secondary_per_pair = win_distance_scale * max_win_distance + max_standings_distance;
    let max_secondary_total = pairs_per_round * max_secondary_per_pair;
    let prior_meetings_scale = max_secondary_total + 1;
    let max_prior_meetings = rp.start_round.max(0) as i128;
    let max_lower_cost_total =
        pairs_per_round * (prior_meetings_scale * max_prior_meetings + max_secondary_per_pair);
    let repeated_game_scale = max_lower_cost_total + 1;

    let mut edges: Vec<(usize, usize, i128)> = Vec::new();
    for i in 0..n {
        for j in (i + 1)..n {
            let prior_meetings = ctx
                .repeats
                .get(&Pairing::new(players[i].clone(), players[j].clone()))
                as i128;
            let win_distance = (players[i].wins - players[j].wins).abs() as i128;
            let standings_distance = (j - i) as i128;
            let secondary_cost = win_distance_scale * win_distance + standings_distance;
            let repeated_game = i128::from(prior_meetings > 0);
            let cost = repeated_game_scale * repeated_game
                + prior_meetings_scale * prior_meetings
                + secondary_cost;
            let weight = -WEIGHT_SCALE * cost + match_tiebreak(i, j);
            edges.push((i, j, weight));
        }
    }

    let pairs = max_weight_matching_pairs(n, &edges);
    if pairs.len() != n / 2 {
        return Err(format!(
            "round {} has no complete minimal-repeat Swiss pairing",
            rp.round
        ));
    }

    let mut out = Pairings::new();
    for (i, j) in pairs {
        out.add(players[i].clone(), players[j].clone());
    }
    Ok(out)
}

/// Top `SWISS_DISTANCE` players paired Swiss; the rest paired RandomNoRepeats.
pub fn pair_swiss_plus_random(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    if rp.start_round < 1 {
        let seeding = ctx.standings(0);
        return pair_swiss_initial(&seeding);
    }
    let players = ctx.standings(rp.start_round);
    let split = SWISS_DISTANCE.min(players.len());
    let swiss_players = &players[..split];
    let rand_players = &players[split..];
    let swiss_pairings = pair_swiss_players(swiss_players, ctx.repeats);
    let random_pairings = pair_no_repeats_blossom(rand_players, ctx.repeats, ctx.rng);
    let mut out = Pairings::new();
    out.pairings.extend(swiss_pairings.pairings);
    out.pairings.extend(random_pairings.pairings);
    out
}
