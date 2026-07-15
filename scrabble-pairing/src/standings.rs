//! Players, standings, and the per-round bookkeeping (`Repeats`, `Starts`).
//!
//! Standings are built from a name-keyed map and then stable-sorted by score,
//! so players tied on score keep the order in which they were first seen. An
//! insertion-ordered `IndexMap` plus a stable sort guarantees that.

use std::collections::HashMap;

use indexmap::IndexMap;

use crate::model::{PlayerData, ResultSlipData};

/// A competitor with their running record. `score` is `wins + 0.5*ties`.
/// Name of the synthetic bye opponent. A player paired against this one sits the
/// round out.
pub const BYE_NAME: &str = "Bye";

#[derive(Debug, Clone, PartialEq)]
pub struct Player {
    pub name: String,
    pub wins: i32,
    pub losses: i32,
    pub ties: i32,
    pub score: f64,
    pub spread: i32,
    pub starts: i32,
}

impl Player {
    pub fn new(name: impl Into<String>) -> Self {
        Player {
            name: name.into(),
            wins: 0,
            losses: 0,
            ties: 0,
            score: 0.0,
            spread: 0,
            starts: 0,
        }
    }

    /// The synthetic bye opponent.
    pub fn bye() -> Self {
        Player::new(BYE_NAME)
    }

    pub fn is_bye(&self) -> bool {
        self.name.eq_ignore_ascii_case(BYE_NAME)
    }
}

/// An unordered pairing of two players.
#[derive(Debug, Clone)]
pub struct Pairing {
    pub first: Player,
    pub second: Player,
}

impl Pairing {
    pub fn new(first: Player, second: Player) -> Self {
        Pairing { first, second }
    }
}

/// An ordered list of pairings.
#[derive(Debug, Clone, Default)]
pub struct Pairings {
    pub pairings: Vec<Pairing>,
}

impl Pairings {
    pub fn new() -> Self {
        Pairings::default()
    }

    pub fn add(&mut self, first: Player, second: Player) {
        self.pairings.push(Pairing::new(first, second));
    }

    pub fn add_result_slip(&mut self, r: &ResultSlipData) {
        let winner = Player::new(&r.winner_name);
        let loser = Player::new(&r.loser_name);
        if r.winner_started {
            self.add(winner, loser);
        } else {
            self.add(loser, winner);
        }
    }

    pub fn len(&self) -> usize {
        self.pairings.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pairings.is_empty()
    }
}

/// One player's outcome in a single game.
struct Outcome {
    name: String,
    spread: i32,
    start: bool,
}

impl Outcome {
    fn from_slip(slip: &ResultSlipData, winner: bool) -> Self {
        if winner {
            Outcome {
                name: slip.winner_name.clone(),
                spread: slip.winner_score - slip.loser_score,
                start: slip.winner_started,
            }
        } else {
            Outcome {
                name: slip.loser_name.clone(),
                spread: slip.loser_score - slip.winner_score,
                start: !slip.winner_started,
            }
        }
    }
}

/// Accumulated player records over a set of result slips.
#[derive(Debug, Default)]
pub struct Results {
    players: IndexMap<String, Player>,
}

impl Results {
    fn update_player(&mut self, outcome: &Outcome) {
        let p = self
            .players
            .entry(outcome.name.clone())
            .or_insert_with(|| Player::new(&outcome.name));
        p.spread += outcome.spread;
        if outcome.spread > 0 {
            p.wins += 1;
        } else if outcome.spread == 0 {
            p.ties += 1;
        } else {
            p.losses += 1;
        }
        p.score = p.wins as f64 + 0.5 * p.ties as f64;
        p.starts += outcome.start as i32;
    }

    fn add_result(&mut self, slip: &ResultSlipData) {
        // Winner first, then loser — this fixes the insertion order (see module note).
        self.update_player(&Outcome::from_slip(slip, true));
        self.update_player(&Outcome::from_slip(slip, false));
    }

    /// Players ordered by score (highest first), then by spread (standard
    /// Scrabble order: among equal records, higher cumulative spread ranks
    /// higher); remaining ties keep first-seen order.
    pub fn standings(&self) -> Vec<Player> {
        let mut standings: Vec<Player> = self.players.values().cloned().collect();
        // Stable sort by descending score, then descending spread (partial_cmp
        // is fine — scores are finite).
        standings.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap()
                .then(b.spread.cmp(&a.spread))
        });
        standings
    }
}

/// Tally of records from every slip in rounds `<= round`.
pub fn results_after_round(slips: &[ResultSlipData], round: i32) -> Results {
    let mut res = Results::default();
    for r in slips {
        if r.round <= round {
            res.add_result(r);
        }
    }
    res
}

/// Initial seeding order: players by descending rating (ties keep input order).
/// Dropped (withdrawn) entrants are unpairable, so they never seed a round.
pub fn seedings(players: &[PlayerData]) -> Vec<Player> {
    let mut indexed: Vec<&PlayerData> = players.iter().filter(|p| !p.dropped).collect();
    // sort_by is stable, so equal ratings keep their original input order.
    indexed.sort_by(|a, b| b.rating.cmp(&a.rating));
    indexed.into_iter().map(|p| Player::new(&p.name)).collect()
}

/// Standings after `round`, with the bye and any excluded players removed.
///
/// Round 0 uses the seedings; later rounds use accumulated results. Withdrawn
/// players are dropped from the pairable field (their games still counted for
/// everyone else), and a late entrant with no results yet is appended as a zero
/// record — in seeding (rating) order among newcomers — so it starts getting
/// paired. The bye is never a competitor, and `excluded` (fixed players this
/// round) are filtered so a strategy only sees the remaining field.
pub fn standings_after_round(
    players: &[PlayerData],
    slips: &[ResultSlipData],
    round: i32,
    excluded: &std::collections::HashSet<String>,
) -> Vec<Player> {
    let mut s = if round == 0 {
        seedings(players)
    } else {
        let mut s = results_after_round(slips, round).standings();
        let dropped: std::collections::HashSet<&str> = players
            .iter()
            .filter(|p| p.dropped)
            .map(|p| p.name.as_str())
            .collect();
        if !dropped.is_empty() {
            s.retain(|p| !dropped.contains(p.name.as_str()));
        }
        let present: std::collections::HashSet<String> =
            s.iter().map(|p| p.name.clone()).collect();
        let mut newcomers: Vec<&PlayerData> = players
            .iter()
            .filter(|p| !p.dropped && !present.contains(&p.name))
            .collect();
        newcomers.sort_by(|a, b| b.rating.cmp(&a.rating));
        for p in newcomers {
            s.push(Player::new(&p.name));
        }
        s
    };
    s.retain(|p| !p.is_bye());
    if !excluded.is_empty() {
        s.retain(|p| !excluded.contains(&p.name));
    }
    s
}

/// Tracks how many times each unordered pair of players has met.
#[derive(Debug, Default)]
pub struct Repeats {
    matches: HashMap<(String, String), i32>,
}

impl Repeats {
    fn key(p: &Pairing) -> (String, String) {
        let (a, b) = (p.first.name.clone(), p.second.name.clone());
        if a <= b {
            (a, b)
        } else {
            (b, a)
        }
    }

    /// Record one more meeting of this pair; returns the new count.
    pub fn add(&mut self, p: &Pairing) -> i32 {
        let entry = self.matches.entry(Self::key(p)).or_insert(0);
        *entry += 1;
        *entry
    }

    /// How many times this pair has met so far (0 if never).
    pub fn get(&self, p: &Pairing) -> i32 {
        *self.matches.get(&Self::key(p)).unwrap_or(&0)
    }
}

/// Tracks who has gone first, to balance starts across the event.
#[derive(Debug, Default)]
pub struct Starts {
    starts: HashMap<String, i32>,
    h2h: HashMap<(String, String), bool>,
    recent_starts: HashMap<String, i32>,
    fixed_starts: HashMap<(i32, String), bool>,
}

impl Starts {
    pub fn new() -> Self {
        Starts::default()
    }

    fn record(&mut self, name1: &str, name2: &str, round: i32, p1_starts: bool) {
        if p1_starts {
            *self.starts.entry(name1.to_string()).or_insert(0) += 1;
            self.recent_starts.insert(name1.to_string(), round);
            self.h2h
                .insert((name1.to_string(), name2.to_string()), true);
            self.h2h
                .insert((name2.to_string(), name1.to_string()), false);
        } else {
            *self.starts.entry(name2.to_string()).or_insert(0) += 1;
            self.recent_starts.insert(name2.to_string(), round);
            self.h2h
                .insert((name1.to_string(), name2.to_string()), false);
            self.h2h
                .insert((name2.to_string(), name1.to_string()), true);
        }
    }

    /// Record a known start from a finished round (player1 went first).
    pub fn register(&mut self, p: &Pairing, round: i32) {
        self.record(&p.first.name, &p.second.name, round, true);
    }

    /// Decide who starts, record it, and return the pairing oriented starter-first.
    pub fn add(&mut self, p: &Pairing, round: i32) -> Pairing {
        let name1 = p.first.name.clone();
        let name2 = p.second.name.clone();
        let p1_starts = if p.first.is_bye() {
            true
        } else if p.second.is_bye() {
            false
        } else if *self
            .fixed_starts
            .get(&(round, name1.clone()))
            .unwrap_or(&false)
        {
            true
        } else if *self
            .fixed_starts
            .get(&(round, name2.clone()))
            .unwrap_or(&false)
        {
            false
        } else {
            let starts1 = *self.starts.get(&name1).unwrap_or(&0);
            let starts2 = *self.starts.get(&name2).unwrap_or(&0);
            if starts1 == starts2 {
                match self.h2h.get(&(name1.clone(), name2.clone())) {
                    // Whoever went first most recently should go second now.
                    None => {
                        *self.recent_starts.get(&name1).unwrap_or(&0)
                            <= *self.recent_starts.get(&name2).unwrap_or(&0)
                    }
                    Some(prev) => !prev,
                }
            } else {
                starts1 < starts2
            }
        };
        self.record(&name1, &name2, round, p1_starts);
        if p1_starts {
            p.clone()
        } else {
            Pairing::new(p.second.clone(), p.first.clone())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn player(name: &str, rating: i32) -> PlayerData {
        PlayerData {
            name: name.into(),
            rating,
            dropped: false,
        }
    }

    fn slip(round: i32, w: &str, l: &str, ws: i32, ls: i32, started: bool) -> ResultSlipData {
        ResultSlipData {
            round,
            winner_name: w.into(),
            loser_name: l.into(),
            winner_score: ws,
            loser_score: ls,
            winner_started: started,
        }
    }

    #[test]
    fn seedings_sort_by_rating_desc() {
        let es = [player("Lo", 1500), player("Hi", 1900), player("Mid", 1700)];
        let names: Vec<_> = seedings(&es).into_iter().map(|p| p.name).collect();
        assert_eq!(names, vec!["Hi", "Mid", "Lo"]);
    }

    #[test]
    fn standings_accumulate_and_filter_bye() {
        let es = [player("A", 1800), player("B", 1700), player("Bye", 0)];
        let slips = [
            slip(1, "A", "B", 500, 400, true),
            slip(1, "A", "Bye", 50, 0, true),
        ];
        let excluded = std::collections::HashSet::new();
        let s = standings_after_round(&es, &slips, 1, &excluded);
        let names: Vec<_> = s.iter().map(|p| p.name.clone()).collect();
        assert!(!names.contains(&"Bye".to_string()));
        // A has 2 wins, leads.
        assert_eq!(s[0].name, "A");
        assert_eq!(s[0].wins, 2);
    }

    #[test]
    fn repeats_count_pairs_unordered() {
        let mut r = Repeats::default();
        let p = Pairing::new(Player::new("A"), Player::new("B"));
        let p_rev = Pairing::new(Player::new("B"), Player::new("A"));
        assert_eq!(r.get(&p), 0);
        assert_eq!(r.add(&p), 1);
        assert_eq!(r.get(&p_rev), 1); // order-independent
        assert_eq!(r.add(&p_rev), 2);
    }

    #[test]
    fn starts_balances_who_goes_first() {
        let mut s = Starts::new();
        let p = Pairing::new(Player::new("A"), Player::new("B"));
        // First meeting: A starts (no history; tie broken by recent_starts).
        let oriented = s.add(&p, 1);
        assert_eq!(oriented.first.name, "A");
        // A now has a start; next time B should start.
        let p2 = Pairing::new(Player::new("A"), Player::new("B"));
        let oriented2 = s.add(&p2, 2);
        assert_eq!(oriented2.first.name, "B");
    }
}
