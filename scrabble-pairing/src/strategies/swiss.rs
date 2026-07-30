//! Swiss pairing. Players are grouped by win count; within the top group we pick
//! the lowest-repeat, smallest-rating-distance opponents via blossom matching,
//! peel off that group, and repeat.

use std::collections::VecDeque;

use indexmap::IndexMap;

use crate::matching::max_weight_matching_pairs;
use crate::model::SwissConfig;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairing, Pairings, Player, Repeats};

use super::{pair_no_repeats_blossom, Ctx};

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

/// Bucket key for a player's score group: match points in halves, so a draw
/// lands a player between the win counts either side of it.
///
/// This must be the same quantity the standings are *ranked* by (`score`, i.e.
/// `wins + 0.5*ties`). Keying on `wins` instead — as the Google Sheets script
/// this was ported from does — puts a drawing player in a group that doesn't
/// match their rank: 2-0-2 and 3-1-0 are both 3 points and adjacent in the
/// standings, but split into different win groups, so the drawer gets paired a
/// full point down. It also makes groups non-contiguous in the standings, which
/// breaks the distance metric the matching weighs.
///
/// With no draws every score is a whole number, so the odd buckets are empty and
/// `compact` removes them — grouping is then identical to keying on wins.
fn points_key(p: &Player) -> usize {
    (p.score * 2.0).round().max(0.0) as usize
}

/// Score groups, highest first, each an ordered queue of players.
struct Groups {
    groups: VecDeque<VecDeque<Player>>,
}

impl Groups {
    fn from_standings(standings: &[Player]) -> Groups {
        let max_key = standings.iter().map(points_key).max().unwrap_or(0);
        let mut groups: VecDeque<VecDeque<Player>> =
            (0..=max_key).map(|_| VecDeque::new()).collect();
        for p in standings {
            groups[points_key(p)].push_back(p.clone());
        }
        let mut g = Groups { groups };
        g.compact();
        // Reverse so the highest-scoring group is at the front.
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
fn pair_candidates(bracket: &[Vec<Candidate>], cfg: &SwissConfig) -> Vec<(String, String)> {
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
            if c.distance < cfg.max_distance {
                let v1 = names[&c.name1];
                let v2 = names[&c.name2];
                let key = if v1 <= v2 { (v1, v2) } else { (v2, v1) };
                let weight = WEIGHT_SCALE
                    * -(cfg.swiss_weight as i128 * c.repeats as i128 + c.distance as i128)
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
fn pair_swiss_players(players: &[Player], repeats: &Repeats, cfg: &SwissConfig) -> Pairings {
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
            let pairs = pair_candidates(&candidates, cfg);
            groups.compact();
            if pairs.is_empty() || pairs.len() != candidates.len() / 2 {
                // Couldn't fully pair the top group at this repeat limit.
                nrep += 1;
                continue;
            }
            groups.groups.pop_front();
            paired.push(pairs);
            nrep = 1;
            // Each score group gets to start from the strictest repeat limit.
            // `nrep` is raised only to get a *particular* group paired; carrying
            // that relaxation forward lets one hard group at the top authorize
            // rematches all the way down the field — and worse, a later group
            // whose members have already met then looks pairable, so the code
            // never tries promoting players up to avoid the rematch.
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
    pair_swiss_players(&players, ctx.repeats, ctx.swiss_config)
}

/// Top `spr_split` players paired Swiss; the rest paired RandomNoRepeats.
pub fn pair_swiss_plus_random(ctx: &mut Ctx, rp: &RoundPairing) -> Pairings {
    if rp.start_round < 1 {
        let seeding = ctx.standings(0);
        return pair_swiss_initial(&seeding);
    }
    let players = ctx.standings(rp.start_round);
    let split = ctx.swiss_config.spr_split.min(players.len());
    let swiss_players = &players[..split];
    let rand_players = &players[split..];
    let swiss_pairings = pair_swiss_players(swiss_players, ctx.repeats, ctx.swiss_config);
    let random_pairings = pair_no_repeats_blossom(rand_players, ctx.repeats, ctx.rng);
    let mut out = Pairings::new();
    out.pairings.extend(swiss_pairings.pairings);
    out.pairings.extend(random_pairings.pairings);
    out
}


#[cfg(test)]
mod tests {
    use super::*;

    fn player(name: &str, score: f64) -> Player {
        let mut p = Player::new(name);
        p.wins = score as i32;
        p.score = score;
        p
    }

    fn met(repeats: &mut Repeats, a: &Player, b: &Player) {
        repeats.add(&Pairing::new(a.clone(), b.clone()));
    }

    /// The repeat limit is raised to get a *particular* score group paired; it
    /// must not stay raised for the groups below it.
    ///
    /// Group A (4 players on 5 points) cannot be paired at all without a rematch
    /// — A2, A3 and A4 have all met each other, so every one of them can only
    /// play A1 — which forces `nrep` up. Group B is two players on 4 points who
    /// have also met, but who have *not* met anyone in group C. With the limit
    /// still relaxed, B1 v B2 looks acceptable and is paired as a rematch; reset,
    /// group B has no legal pairing at the strict limit, so players are promoted
    /// up from C and the rematch is avoided.
    #[test]
    fn a_raised_repeat_limit_does_not_leak_into_later_score_groups() {
        let a: Vec<Player> = (1..=4).map(|i| player(&format!("A{i}"), 5.0)).collect();
        let b: Vec<Player> = (1..=2).map(|i| player(&format!("B{i}"), 4.0)).collect();
        // Six, so the bottom group is not merged upward into B.
        let c: Vec<Player> = (1..=6).map(|i| player(&format!("C{i}"), 3.0)).collect();

        let mut repeats = Repeats::default();
        met(&mut repeats, &a[1], &a[2]);
        met(&mut repeats, &a[1], &a[3]);
        met(&mut repeats, &a[2], &a[3]);
        met(&mut repeats, &b[0], &b[1]);

        let field: Vec<Player> = a.iter().chain(&b).chain(&c).cloned().collect();
        let out = pair_swiss_players(&field, &repeats, &SwissConfig::default());

        let paired_together = out.pairings.iter().any(|p| {
            matches!(
                (p.first.name.as_str(), p.second.name.as_str()),
                ("B1", "B2") | ("B2", "B1")
            )
        });
        assert!(
            !paired_together,
            "B1 and B2 have already met and had unmet opponents available; \
             they were rematched anyway: {:?}",
            out.pairings
                .iter()
                .map(|p| (p.first.name.as_str(), p.second.name.as_str()))
                .collect::<Vec<_>>()
        );
    }
}
