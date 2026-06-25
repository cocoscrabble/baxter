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
                let weight = -(30 * c.repeats as i128 + c.distance as i128);
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
