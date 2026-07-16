//! Tournament-level orchestration: walk the configured rounds, pair the ones
//! that are ready, and emit the result.

use std::collections::{HashMap, HashSet};

use rand_chacha::ChaCha8Rng;

use crate::model::{OutPairing, PairingInput, PlayerData, ResultSlipData, RoundResult};
use crate::rng::seeded;
use crate::round_pairing::{normalize_round_robin_start_rounds, RoundPairing, RP};
use crate::standings::{standings_after_round, Pairings, Player, Repeats, Starts, BYE_NAME};
use crate::strategies::{basic, quads, roundrobin, swiss, Ctx};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RoundStatus {
    Empty,
    Partial,
    Finished,
}

fn status_of(status: &HashMap<i32, RoundStatus>, round: i32) -> RoundStatus {
    *status.get(&round).unwrap_or(&RoundStatus::Empty)
}

/// Dispatch to the strategy for this round. Returns `Err` for an invalid
/// condition (unknown strategy, or a field too small for the format).
fn run_strategy(rp: &RoundPairing, ctx: &mut Ctx) -> Result<Pairings, String> {
    Ok(match rp.pairing {
        RP::KotH => basic::pair_koth(ctx, rp),
        RP::QotH => basic::pair_qoth(ctx, rp),
        RP::Swiss => swiss::pair_swiss(ctx, rp),
        RP::RoundRobin => roundrobin::pair_round_robin(ctx, rp)?,
        RP::DoubleRoundRobin => roundrobin::pair_double_round_robin(ctx, rp)?,
        RP::Random => basic::pair_random(ctx, rp),
        RP::RandomNoRepeats => basic::pair_random_no_repeats(ctx, rp),
        RP::QuadsClustered => quads::pair_clustered_quads(ctx, rp)?,
        RP::QuadsDistributed => quads::pair_distributed_quads(ctx, rp)?,
        RP::QuadsEqualized => quads::pair_equalized_quads(ctx, rp)?,
        RP::Sixes => quads::pair_sixes(ctx, rp)?,
        RP::Charlottesville => roundrobin::pair_charlottesville(ctx, rp)?,
        RP::SwissPlusRandom => swiss::pair_swiss_plus_random(ctx, rp),
        RP::Unknown => return Err("unknown pairing strategy".to_string()),
    })
}

/// Count how many byes each player has already received, from result history.
fn byes_so_far(slips: &[ResultSlipData]) -> HashMap<String, i32> {
    let mut byes: HashMap<String, i32> = HashMap::new();
    for s in slips {
        if s.winner_name.eq_ignore_ascii_case(BYE_NAME) {
            *byes.entry(s.loser_name.clone()).or_insert(0) += 1;
        } else if s.loser_name.eq_ignore_ascii_case(BYE_NAME) {
            *byes.entry(s.winner_name.clone()).or_insert(0) += 1;
        }
    }
    byes
}

/// Return a `(player, "Bye")` pair to force when the field is odd, else `None`.
/// The bye goes to the lowest-ranked player with the fewest byes so far.
fn bye_pairing(
    players: &[PlayerData],
    slips: &[ResultSlipData],
    rp: &RoundPairing,
    fixed_pairs: &[(String, String)],
) -> Option<(String, String)> {
    if rp.pairing.is_round_robin() || rp.pairing.is_quad() {
        return None;
    }
    let empty = HashSet::new();
    let field = standings_after_round(players, slips, rp.start_round, &empty);
    let fixed_names: HashSet<&str> = fixed_pairs
        .iter()
        .flat_map(|(a, b)| [a.as_str(), b.as_str()])
        .collect();
    let eligible: Vec<&Player> = field
        .iter()
        .filter(|p| !fixed_names.contains(p.name.as_str()))
        .collect();
    if eligible.len().is_multiple_of(2) {
        return None;
    }
    let byes = byes_so_far(slips);
    let fewest = eligible
        .iter()
        .map(|p| *byes.get(&p.name).unwrap_or(&0))
        .min()
        .unwrap();
    // Lowest-ranked (standings run best-first) among those with the fewest byes.
    for p in eligible.iter().rev() {
        if *byes.get(&p.name).unwrap_or(&0) == fewest {
            return Some((p.name.clone(), BYE_NAME.to_string()));
        }
    }
    Some((eligible.last().unwrap().name.clone(), BYE_NAME.to_string()))
}

/// Status of every round that has any results, by game count.
fn round_status(players: &[PlayerData], slips: &[ResultSlipData]) -> HashMap<i32, RoundStatus> {
    let n_real = players
        .iter()
        .filter(|e| !e.name.eq_ignore_ascii_case(BYE_NAME))
        .count();
    let n_games = n_real.div_ceil(2);
    let mut round_counts: HashMap<i32, usize> = HashMap::new();
    for s in slips {
        *round_counts.entry(s.round).or_insert(0) += 1;
    }
    let mut counts = HashMap::new();
    for (round, count) in round_counts {
        let st = if count == n_games {
            RoundStatus::Finished
        } else if count > 0 {
            RoundStatus::Partial
        } else {
            RoundStatus::Empty
        };
        counts.insert(round, st);
    }
    counts
}

fn can_pair(rp: &RoundPairing, status: &HashMap<i32, RoundStatus>) -> bool {
    let stat = status_of(status, rp.round);
    if stat == RoundStatus::Finished || stat == RoundStatus::Partial {
        return false;
    }
    if rp.pairing.is_round_robin() {
        // Round robins don't depend on a previous round's results.
        return true;
    }
    rp.start_round == 0 || status_of(status, rp.start_round) == RoundStatus::Finished
}

/// Pairings reconstructed from a finished round's result slips (starter first).
fn extract_pairings(slips: &[ResultSlipData], round: i32) -> Pairings {
    let mut p = Pairings::new();
    for r in slips {
        if r.round == round {
            p.add_result_slip(r);
        }
    }
    p
}

/// Pair a single round: inject a bye for an odd field, run the strategy on the
/// remaining (non-fixed) players, then add the fixed/bye pairs back in.
#[allow(clippy::too_many_arguments)]
fn pair_round(
    players: &[PlayerData],
    slips: &[ResultSlipData],
    round_pairings: &[RoundPairing],
    fixed_map: &HashMap<i32, Vec<(String, String)>>,
    published_map: &HashMap<i32, Vec<(String, String)>>,
    repeats: &Repeats,
    rng: &mut ChaCha8Rng,
    rp: &RoundPairing,
) -> Result<Pairings, String> {
    // The round-robin family (round robin, double round robin, Charlottesville)
    // honors fixed pairings inside the strategy — it schedules matchings across
    // the block rather than excluding players — so it must see the full field.
    // Skip the exclude/bye/append path entirely.
    if matches!(
        rp.pairing,
        RP::RoundRobin | RP::DoubleRoundRobin | RP::Charlottesville
    ) {
        let empty = HashSet::new();
        let mut ctx = Ctx {
            players,
            slips,
            round_pairings,
            excluded: &empty,
            fixed_pairings: fixed_map,
            published_pairings: published_map,
            repeats,
            rng,
        };
        return run_strategy(rp, &mut ctx);
    }

    let mut fixed_pairs: Vec<(String, String)> =
        fixed_map.get(&rp.round).cloned().unwrap_or_default();

    if let Some(bye) = bye_pairing(players, slips, rp, &fixed_pairs) {
        fixed_pairs.push(bye);
    }

    let mut excluded: HashSet<String> = HashSet::new();
    for (a, b) in &fixed_pairs {
        excluded.insert(a.clone());
        excluded.insert(b.clone());
    }

    // Run the strategy with the fixed players excluded from standings.
    let mut result = {
        let mut ctx = Ctx {
            players,
            slips,
            round_pairings,
            excluded: &excluded,
            fixed_pairings: fixed_map,
            published_pairings: published_map,
            repeats,
            rng,
        };
        run_strategy(rp, &mut ctx)?
    };

    if !fixed_pairs.is_empty() {
        // Look up full Player records (with score/starts) for the fixed players.
        let empty = HashSet::new();
        let all = standings_after_round(players, slips, rp.start_round, &empty);
        let by_name: HashMap<&str, &Player> = all.iter().map(|p| (p.name.as_str(), p)).collect();
        for (name1, name2) in &fixed_pairs {
            let p1 = by_name
                .get(name1.as_str())
                .map(|p| (*p).clone())
                .unwrap_or_else(|| Player::new(name1));
            let p2 = by_name
                .get(name2.as_str())
                .map(|p| (*p).clone())
                .unwrap_or_else(|| Player::new(name2));
            result.add(p1, p2);
        }
    }

    Ok(result)
}

/// Pair a whole tournament round by round. The public entry point.
pub fn pair(input: &PairingInput) -> Vec<RoundResult> {
    let mut rps = input.round_pairings.clone();
    normalize_round_robin_start_rounds(&mut rps);

    let players = &input.players;
    let slips = &input.result_slips;

    let mut repeats = Repeats::default();
    let mut starts = Starts::new();
    let mut rng = seeded(input.seed);
    let status = round_status(players, slips);

    let mut ret: Vec<RoundResult> = Vec::new();
    for rp in &rps {
        if status_of(&status, rp.round) == RoundStatus::Finished {
            // Replay a finished round into the repeat/starts history.
            for p in extract_pairings(slips, rp.round).pairings {
                repeats.add(&p);
                starts.register(&p, rp.round);
            }
        } else if can_pair(rp, &status) {
            match pair_round(
                players,
                slips,
                &rps,
                &input.fixed_pairings,
                &input.published_pairings,
                &repeats,
                &mut rng,
                rp,
            ) {
                Ok(paired) => {
                    let mut out_pairings = Vec::new();
                    for p in paired.pairings {
                        let reps = repeats.add(&p);
                        let oriented = starts.add(&p, rp.round);
                        out_pairings.push(OutPairing {
                            first: oriented.first.name,
                            second: oriented.second.name,
                            repeats: reps,
                        });
                    }
                    ret.push(RoundResult {
                        round: rp.round,
                        pairings: out_pairings,
                        error: None,
                    });
                }
                // Invalid condition: emit an empty round carrying the reason.
                Err(error) => ret.push(RoundResult {
                    round: rp.round,
                    pairings: Vec::new(),
                    error: Some(error),
                }),
            }
        }
    }
    ret
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(json: &str) -> PairingInput {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn koth_round_one_pairs_by_seed() {
        // Four players, one KotH round from seedings: 1-2, 3-4 by rating.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].round, 1);
        let pairs: Vec<(String, String)> = out[0]
            .pairings
            .iter()
            .map(|p| {
                let mut names = [p.first.clone(), p.second.clone()];
                names.sort();
                (names[0].clone(), names[1].clone())
            })
            .collect();
        assert!(pairs.contains(&("A".into(), "B".into())));
        assert!(pairs.contains(&("C".into(), "D".into())));
    }

    #[test]
    fn odd_field_gets_a_bye() {
        // Three players, KotH: someone is paired against "Bye".
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}]
            }"#,
        );
        let out = pair(&inp);
        let has_bye = out[0]
            .pairings
            .iter()
            .any(|p| p.first == "Bye" || p.second == "Bye");
        assert!(has_bye);
        // Lowest seed (C) takes the first bye.
        let c_has_bye = out[0].pairings.iter().any(|p| {
            (p.first == "C" && p.second == "Bye") || (p.first == "Bye" && p.second == "C")
        });
        assert!(c_has_bye);
    }

    #[test]
    fn swiss_initial_is_top_vs_bottom_half() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "Swiss"}]
            }"#,
        );
        let out = pair(&inp);
        let pairs: Vec<(String, String)> = out[0]
            .pairings
            .iter()
            .map(|p| {
                let mut n = [p.first.clone(), p.second.clone()];
                n.sort();
                (n[0].clone(), n[1].clone())
            })
            .collect();
        // 1 vs 3, 2 vs 4 (top half vs bottom half).
        assert!(pairs.contains(&("A".into(), "C".into())));
        assert!(pairs.contains(&("B".into(), "D".into())));
    }

    #[test]
    fn odd_round_robin_byes_each_player_once_with_no_repeats() {
        // 5 players over a full 5-round rotation. With the ghost-bye fix, each
        // player should bye exactly once and meet every other player exactly once.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1990},
                    {"name": "B", "rating": 1980},
                    {"name": "C", "rating": 1970},
                    {"name": "D", "rating": 1960},
                    {"name": "E", "rating": 1950}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
                    {"round": 2, "start_round": 0, "pairing": "RoundRobin"},
                    {"round": 3, "start_round": 0, "pairing": "RoundRobin"},
                    {"round": 4, "start_round": 0, "pairing": "RoundRobin"},
                    {"round": 5, "start_round": 0, "pairing": "RoundRobin"}
                ]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 5);

        let mut byes: HashMap<String, i32> = HashMap::new();
        let mut meetings: HashMap<(String, String), i32> = HashMap::new();
        for round in &out {
            // 5 players -> 1 ghost -> 6 slots -> 3 games (one is a bye game).
            assert_eq!(round.pairings.len(), 3, "round {}", round.round);
            for p in &round.pairings {
                let (a, b) = (p.first.clone(), p.second.clone());
                if a == "Bye" {
                    *byes.entry(b).or_default() += 1;
                } else if b == "Bye" {
                    *byes.entry(a).or_default() += 1;
                } else {
                    let key = if a < b { (a, b) } else { (b, a) };
                    *meetings.entry(key).or_default() += 1;
                }
            }
        }

        for name in ["A", "B", "C", "D", "E"] {
            assert_eq!(byes.get(name).copied().unwrap_or(0), 1, "{name} bye count");
        }
        // C(5,2) = 10 distinct pairs, each met exactly once.
        assert_eq!(meetings.len(), 10);
        assert!(meetings.values().all(|&c| c == 1), "repeats: {meetings:?}");
    }

    #[test]
    fn odd_charlottesville_byes_each_round_no_repeats() {
        // 5 players over a full rotation (len(g2) = 3 rounds): one player byes
        // per round, all distinct, and no real pair repeats.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1990},
                    {"name": "B", "rating": 1980},
                    {"name": "C", "rating": 1970},
                    {"name": "D", "rating": 1960},
                    {"name": "E", "rating": 1950}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Charlottesville"},
                    {"round": 2, "start_round": 0, "pairing": "Charlottesville"},
                    {"round": 3, "start_round": 0, "pairing": "Charlottesville"}
                ]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 3);

        let mut byes: HashMap<String, i32> = HashMap::new();
        let mut meetings: HashMap<(String, String), i32> = HashMap::new();
        for round in &out {
            assert_eq!(round.pairings.len(), 3, "round {}", round.round);
            let mut seen: HashSet<String> = HashSet::new();
            for p in &round.pairings {
                assert!(seen.insert(p.first.clone()));
                assert!(seen.insert(p.second.clone()));
                let (a, b) = (p.first.clone(), p.second.clone());
                if a == "Bye" {
                    *byes.entry(b).or_default() += 1;
                } else if b == "Bye" {
                    *byes.entry(a).or_default() += 1;
                } else {
                    let key = if a < b { (a, b) } else { (b, a) };
                    *meetings.entry(key).or_default() += 1;
                }
            }
        }
        assert_eq!(byes.values().sum::<i32>(), 3);
        assert!(byes.values().all(|&c| c == 1), "byes: {byes:?}");
        assert!(meetings.values().all(|&c| c == 1), "repeats: {meetings:?}");
    }

    #[test]
    fn odd_quads_one_bye_per_round_no_repeats() {
        // An odd field gets a bye so it divides into whole quads/hexes. Across
        // all four quad/sixes strategies: exactly one player byes per round, each
        // round is a valid matching, and no real pair repeats within the block.
        for (n, strat) in [
            (7, "Quads_Clustered"),
            (5, "Quads_Clustered"),
            (7, "Quads_Distributed"),
            (9, "Quads_Equalized"),
            (5, "Sixes"),
            (7, "Sixes"),
        ] {
            let players: Vec<String> = (0..n)
                .map(|i| format!(r#"{{"name":"P{}","rating":{}}}"#, i + 1, 2000 - 10 * i))
                .collect();
            let rounds: Vec<String> = (1..=3)
                .map(|r| format!(r#"{{"round":{r},"start_round":0,"pairing":"{strat}"}}"#))
                .collect();
            let json = format!(
                r#"{{"players":[{}],"round_pairings":[{}]}}"#,
                players.join(","),
                rounds.join(",")
            );
            let out = pair(&input(&json));
            assert_eq!(out.len(), 3, "{strat} n{n}");

            let mut meetings: HashMap<(String, String), i32> = HashMap::new();
            for round in &out {
                let mut seen: HashSet<String> = HashSet::new();
                let mut byes_this_round = 0;
                for p in &round.pairings {
                    assert!(seen.insert(p.first.clone()), "{strat} n{n}");
                    assert!(seen.insert(p.second.clone()), "{strat} n{n}");
                    let (a, b) = (p.first.clone(), p.second.clone());
                    if a == "Bye" || b == "Bye" {
                        byes_this_round += 1;
                    } else {
                        let key = if a < b { (a, b) } else { (b, a) };
                        *meetings.entry(key).or_default() += 1;
                    }
                }
                assert_eq!(byes_this_round, 1, "{strat} n{n} round {}", round.round);
            }
            assert!(
                meetings.values().all(|&c| c == 1),
                "{strat} n{n} repeats: {meetings:?}"
            );
        }
    }

    #[test]
    fn qoth_small_field_falls_back_to_koth() {
        // Fewer than 4 players can't form Queen-of-the-Hill groups of four, so it
        // falls back to KotH-style consecutive pairing (with a bye for an odd
        // field) instead of panicking.
        let inp = input(
            r#"{
                "players": [
                    {"name": "P1", "rating": 1990},
                    {"name": "P2", "rating": 1980},
                    {"name": "P3", "rating": 1970}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "QotH"}]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 1);
        let mut pairs: Vec<(String, String)> = out[0]
            .pairings
            .iter()
            .map(|p| {
                let mut n = [p.first.clone(), p.second.clone()];
                n.sort();
                (n[0].clone(), n[1].clone())
            })
            .collect();
        pairs.sort();
        assert_eq!(
            pairs,
            vec![
                ("Bye".to_string(), "P3".to_string()),
                ("P1".to_string(), "P2".to_string()),
            ]
        );
    }

    #[test]
    fn quads_too_few_players_report_error() {
        // Two players can't form a quad: the round comes back empty with an error.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "Quads_Clustered"}]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 1);
        assert!(out[0].pairings.is_empty());
        assert_eq!(out[0].error.as_deref(), Some("field too small for quads"));
    }

    #[test]
    fn unknown_strategy_reports_error() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "Bogus"}]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 1);
        assert!(out[0].pairings.is_empty());
        assert_eq!(out[0].error.as_deref(), Some("unknown pairing strategy"));
    }

    #[test]
    fn valid_round_has_no_error() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out[0].error, None);
    }

    // --- round-robin fixed pairings (round-permutation model) ----------------

    fn rr_players_json(n: usize) -> String {
        (1..=n)
            .map(|i| format!(r#"{{"name":"P{}","rating":{}}}"#, i, 2000 - 10 * i))
            .collect::<Vec<_>>()
            .join(",")
    }

    fn rr_rounds_json(n: usize, pairing: &str) -> String {
        (1..=n)
            .map(|r| format!(r#"{{"round":{r},"start_round":0,"pairing":"{pairing}"}}"#))
            .collect::<Vec<_>>()
            .join(",")
    }

    fn round_pairs(out: &[RoundResult], round: i32) -> HashSet<(String, String)> {
        out.iter()
            .find(|r| r.round == round)
            .map(|r| {
                r.pairings
                    .iter()
                    .map(|p| {
                        let mut n = [p.first.clone(), p.second.clone()];
                        n.sort();
                        (n[0].clone(), n[1].clone())
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    fn meetings(out: &[RoundResult]) -> HashMap<(String, String), i32> {
        let mut m: HashMap<(String, String), i32> = HashMap::new();
        for round in out {
            for p in &round.pairings {
                if p.first == "Bye" || p.second == "Bye" {
                    continue;
                }
                let mut n = [p.first.clone(), p.second.clone()];
                n.sort();
                *m.entry((n[0].clone(), n[1].clone())).or_default() += 1;
            }
        }
        m
    }

    #[test]
    fn round_robin_fixed_pairing_meets_in_requested_round() {
        // 6 players, full RR (5 rounds). Force P1 vs P6 in round 1; the block is
        // still a complete round robin (every pair once).
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{{"1":[["P1","P6"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
        );
        let out = pair(&input(&json));
        assert!(round_pairs(&out, 1).contains(&("P1".into(), "P6".into())));
        let m = meetings(&out);
        assert_eq!(m.len(), 15); // C(6, 2)
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn double_round_robin_fixed_pairing_pins_both_rounds() {
        // DRR: rounds 1 & 2 share template position 0; fixing P1 vs P6 at round 1
        // pins it for both, and every pair is met twice.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{{"1":[["P1","P6"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(10, "DoubleRoundRobin"),
        );
        let out = pair(&input(&json));
        assert!(round_pairs(&out, 1).contains(&("P1".into(), "P6".into())));
        assert!(round_pairs(&out, 2).contains(&("P1".into(), "P6".into())));
        let m = meetings(&out);
        assert_eq!(m.len(), 15);
        assert!(
            m.values().all(|&c| c == 2),
            "expected each pair twice: {m:?}"
        );
    }

    #[test]
    fn round_robin_more_rounds_than_field_reports_error() {
        // 4 players -> a round robin has 3 rounds; a 5-round block overflows the
        // rotation. The overflow round comes back empty carrying a clear error,
        // not a panic.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}]}}"#,
            rr_players_json(4),
            rr_rounds_json(5, "RoundRobin"),
        );
        let out = pair(&input(&json));
        let r4 = out.iter().find(|r| r.round == 4).unwrap();
        assert!(r4.pairings.is_empty());
        assert!(r4.error.as_deref().unwrap_or("").contains("beyond the rotation"));
        // The valid rounds 1-3 still pair.
        for round in 1..=3 {
            let r = out.iter().find(|x| x.round == round).unwrap();
            assert!(r.error.is_none());
            assert_eq!(r.pairings.len(), 2);
        }
    }

    #[test]
    fn round_robin_conflicting_fixed_pairings_report_error() {
        // P1 pinned to two opponents in the same round is impossible; the
        // validation layer names the exact conflict.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{{"1":[["P1","P2"],["P1","P3"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
        );
        let out = pair(&input(&json));
        let r1 = out.iter().find(|r| r.round == 1).unwrap();
        assert!(r1.pairings.is_empty());
        let msg = r1.error.as_deref().unwrap_or("");
        assert!(msg.contains("P1 is fixed against both"), "got: {msg}");
    }

    #[test]
    fn round_robin_same_pair_two_rounds_reports_error() {
        // A pair meets exactly once in a round robin; fixing them in two rounds
        // is impossible and named as such.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{{"1":[["P1","P2"]],"2":[["P1","P2"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
        );
        let out = pair(&input(&json));
        let msg = out
            .iter()
            .filter_map(|r| r.error.as_deref())
            .find(|m| m.contains("meet only once"))
            .unwrap_or("");
        assert!(msg.contains("P1") && msg.contains("meet only once"), "got: {msg}");
    }

    #[test]
    fn round_robin_fixing_an_already_played_pair_reports_error() {
        // P1 beat P2 in round 1; fixing that same pair into round 3 is rejected
        // with an "already played" message (they meet once per cycle).
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],
                 "result_slips":[{{"round":1,"winner_name":"P1","loser_name":"P2","winner_score":400,"loser_score":300,"winner_started":true}}],
                 "fixed_pairings":{{"3":[["P1","P2"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
        );
        let out = pair(&input(&json));
        let msg = out
            .iter()
            .filter_map(|r| r.error.as_deref())
            .find(|m| m.contains("already played"))
            .unwrap_or("");
        assert!(msg.contains("P1") && msg.contains("P2"), "got: {msg}");
    }

    #[test]
    fn round_robin_mid_event_fixed_pairing_after_results() {
        // Play round 1 (its default pairing), then add a fixed pairing for a later
        // round: played round 1 is a fixed point, the later round honors the new
        // pairing, and together they remain a complete round robin.
        let base_json = format!(
            r#"{{"players":[{}],"round_pairings":[{}]}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
        );
        let base = pair(&input(&base_json));
        let r1 = round_pairs(&base, 1);
        let slips: Vec<String> = r1
            .iter()
            .map(|(a, b)| {
                format!(
                    r#"{{"round":1,"winner_name":"{a}","loser_name":"{b}","winner_score":400,"loser_score":350,"winner_started":true}}"#
                )
            })
            .collect();
        // Two players who have not met in round 1, to fix into round 4.
        let (x, y) = (1..=6)
            .flat_map(|a| (a + 1..=6).map(move |b| (a, b)))
            .map(|(a, b)| (format!("P{a}"), format!("P{b}")))
            .find(|pair| !r1.contains(pair))
            .unwrap();
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"result_slips":[{}],"fixed_pairings":{{"4":[["{x}","{y}"]]}}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
            slips.join(","),
        );
        let out = pair(&input(&json));
        assert!(round_pairs(&out, 4).contains(&(x.clone(), y.clone())));
        let mut m = meetings(&out);
        for pair in &r1 {
            *m.entry(pair.clone()).or_default() += 1; // round 1 is finished, not in output
        }
        assert_eq!(m.len(), 15);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    /// Build a `fixed_pairings` JSON object from `(round, (a, b))` pins.
    fn fixed_json(pins: &[(i32, (String, String))]) -> String {
        let mut by_round: HashMap<i32, Vec<(String, String)>> = HashMap::new();
        for (r, (a, b)) in pins {
            by_round.entry(*r).or_default().push((a.clone(), b.clone()));
        }
        let mut rounds: Vec<i32> = by_round.keys().copied().collect();
        rounds.sort_unstable();
        let entries: Vec<String> = rounds
            .iter()
            .map(|r| {
                let pairs: Vec<String> = by_round[r]
                    .iter()
                    .map(|(a, b)| format!(r#"["{a}","{b}"]"#))
                    .collect();
                format!(r#""{r}":[{}]"#, pairs.join(","))
            })
            .collect();
        format!("{{{}}}", entries.join(","))
    }

    /// Assert every produced round is a valid matching (no player twice) and
    /// return the meeting counts across the block.
    fn valid_matchings(out: &[RoundResult]) {
        for round in out {
            let mut seen: HashSet<&str> = HashSet::new();
            for p in &round.pairings {
                assert!(seen.insert(&p.first), "dup in round {}", round.round);
                assert!(seen.insert(&p.second), "dup in round {}", round.round);
            }
        }
    }

    #[test]
    fn round_robin_two_pairs_same_round_solves() {
        // The Background repro: P1-P2 and P3-P4 sit in different circle templates,
        // so the template permutation can't co-locate them. The general solver
        // finds a valid round robin with both in round 1.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let r1 = round_pairs(&out, 1);
        assert!(r1.contains(&("P1".into(), "P2".into())));
        assert!(r1.contains(&("P3".into(), "P4".into())));
        let m = meetings(&out);
        assert_eq!(m.len(), 15); // C(6, 2)
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn round_robin_noncircle_full_round_solves() {
        // Pin an entire round to the four adjacent-seed pairs {P1P2,P3P4,P5P6,
        // P7P8} — not a circle template, so this forces a non-circle-isomorphic
        // factorization the fast path can't reach. The solver completes it.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(7, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
                (1, ("P5".into(), "P6".into())),
                (1, ("P7".into(), "P8".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let r1 = round_pairs(&out, 1);
        for pair in [("P1", "P2"), ("P3", "P4"), ("P5", "P6"), ("P7", "P8")] {
            assert!(r1.contains(&(pair.0.into(), pair.1.into())), "{r1:?}");
        }
        let m = meetings(&out);
        assert_eq!(m.len(), 28); // C(8, 2)
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn round_robin_fully_pinned_round_plus_later_pin() {
        // A complete matching pinned in round 1, plus a separate pin in round 3.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(6),
            rr_rounds_json(5, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
                (1, ("P5".into(), "P6".into())),
                (3, ("P1".into(), "P3".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        assert!(round_pairs(&out, 1).contains(&("P5".into(), "P6".into())));
        assert!(round_pairs(&out, 3).contains(&("P1".into(), "P3".into())));
        let m = meetings(&out);
        assert_eq!(m.len(), 15);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn round_robin_fixed_bye_and_pair_same_round() {
        // Odd field: a phantom Bye is vertex 6. Fix P1-P2 and give P4 the bye, both
        // in round 1. The solver composes the bye pin with the other fixed pairing.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(5),
            rr_rounds_json(5, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P4".into(), "Bye".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let r1 = round_pairs(&out, 1);
        assert!(r1.contains(&("P1".into(), "P2".into())), "{r1:?}");
        assert!(r1.contains(&("Bye".into(), "P4".into())), "{r1:?}");
        let m = meetings(&out);
        assert_eq!(m.len(), 10); // C(5, 2), byes excluded
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn double_round_robin_two_pairs_same_round_solves() {
        // DRR: rounds 1 & 2 share position 0. Two pairs fixed into round 1 must
        // both appear in rounds 1 and 2, and every pair is met twice.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(6),
            rr_rounds_json(10, "DoubleRoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        for r in [1, 2] {
            let rp = round_pairs(&out, r);
            assert!(rp.contains(&("P1".into(), "P2".into())), "round {r}: {rp:?}");
            assert!(rp.contains(&("P3".into(), "P4".into())), "round {r}: {rp:?}");
        }
        let m = meetings(&out);
        assert_eq!(m.len(), 15);
        assert!(m.values().all(|&c| c == 2), "expected twice: {m:?}");
    }

    #[test]
    fn round_robin_partial_block_same_round() {
        // An 8-player field but only a 4-round block. Two pairs fixed into round 1
        // (again different circle templates) — feasible inside 4 disjoint rounds.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert_eq!(out.len(), 4);
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        valid_matchings(&out);
        let r1 = round_pairs(&out, 1);
        assert!(r1.contains(&("P1".into(), "P2".into())));
        assert!(r1.contains(&("P3".into(), "P4".into())));
        // 4 disjoint rounds of 4 games = 16 distinct meetings, none repeated.
        let m = meetings(&out);
        assert_eq!(m.len(), 16);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn round_robin_solver_is_deterministic() {
        // The solver is RNG-free: two runs of the same pinned block are identical.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(7, "RoundRobin"),
            fixed_json(&[
                (1, ("P1".into(), "P2".into())),
                (1, ("P3".into(), "P4".into())),
                (4, ("P1".into(), "P5".into())),
            ]),
        );
        assert_eq!(pair(&input(&json)), pair(&input(&json)));
    }

    #[test]
    fn round_robin_solver_output_always_valid_under_random_pins() {
        // Fuzz random pin sets: whenever the block pairs without error, it must be
        // a valid, complete, repeat-free round robin honoring every pin (the
        // solver never emits an invalid schedule).
        let n = 8usize;
        let mut state: u64 = 0x9e3779b97f4a7c15;
        let mut rng = || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _ in 0..40 {
            let mut pins: Vec<(i32, (String, String))> = Vec::new();
            let k = 2 + (rng() % 4) as usize;
            for _ in 0..k {
                let a = 1 + (rng() % n as u64);
                let mut b = 1 + (rng() % n as u64);
                while b == a {
                    b = 1 + (rng() % n as u64);
                }
                let r = 1 + (rng() % (n as u64 - 1)) as i32;
                pins.push((r, (format!("P{a}"), format!("P{b}"))));
            }
            let json = format!(
                r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
                rr_players_json(n),
                rr_rounds_json(n - 1, "RoundRobin"),
                fixed_json(&pins),
            );
            let out = pair(&input(&json));
            valid_matchings(&out);
            if out.iter().all(|r| r.error.is_none()) {
                let m = meetings(&out);
                assert_eq!(m.len(), n * (n - 1) / 2, "incomplete RR for pins {pins:?}");
                assert!(m.values().all(|&c| c == 1), "repeat for pins {pins:?}: {m:?}");
                for (r, (a, b)) in &pins {
                    let mut names = [a.clone(), b.clone()];
                    names.sort();
                    assert!(
                        round_pairs(&out, *r).contains(&(names[0].clone(), names[1].clone())),
                        "pin {a}-{b}@{r} not honored: {out:?}"
                    );
                }
            }
        }
    }

    // --- Charlottesville fixed pairings -------------------------------------

    #[test]
    fn charlottesville_fixed_pairing_meets_in_requested_round() {
        // 8 players, 4-round Charlottesville. Force the cross-group pair P1-P4 into
        // round 2; the block stays a complete bipartite round robin.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "Charlottesville"),
            fixed_json(&[(2, ("P1".into(), "P4".into()))]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        assert!(round_pairs(&out, 2).contains(&("P1".into(), "P4".into())));
        let m = meetings(&out);
        assert_eq!(m.len(), 16); // 4x4 cross-group pairs
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn charlottesville_two_fixed_pairings_same_round() {
        // Two disjoint cross-group pairs fixed into one round.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "Charlottesville"),
            fixed_json(&[
                (1, ("P1".into(), "P4".into())),
                (1, ("P3".into(), "P8".into())),
            ]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let r1 = round_pairs(&out, 1);
        assert!(r1.contains(&("P1".into(), "P4".into())), "{r1:?}");
        assert!(r1.contains(&("P3".into(), "P8".into())), "{r1:?}");
        let m = meetings(&out);
        assert_eq!(m.len(), 16);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn charlottesville_same_group_fixed_pairing_reports_error() {
        // P1 and P3 are both in the second snake group and never play; fixing them
        // together is rejected with a specific message.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "Charlottesville"),
            fixed_json(&[(1, ("P1".into(), "P3".into()))]),
        );
        let out = pair(&input(&json));
        let msg = out
            .iter()
            .filter_map(|r| r.error.as_deref())
            .find(|m| m.contains("same Charlottesville group"))
            .unwrap_or("");
        assert!(msg.contains("P1") && msg.contains("P3"), "got: {msg}");
    }

    #[test]
    fn charlottesville_fixed_bye_odd_field() {
        // 5 players → phantom Bye. Give P1 the bye in round 2 (a cross-group edge);
        // every real cross-group pair still meets once.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(5),
            rr_rounds_json(3, "Charlottesville"),
            fixed_json(&[(2, ("P1".into(), "Bye".into()))]),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let r2 = round_pairs(&out, 2);
        assert!(r2.contains(&("Bye".into(), "P1".into())), "{r2:?}");
        // Real cross-group pairs: {P2,P4} x {P1,P3,P5} = 6, each met once.
        let m = meetings(&out);
        assert_eq!(m.len(), 6);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn charlottesville_solver_is_deterministic() {
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"fixed_pairings":{}}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "Charlottesville"),
            fixed_json(&[
                (2, ("P1".into(), "P4".into())),
                (3, ("P3".into(), "P2".into())),
            ]),
        );
        assert_eq!(pair(&input(&json)), pair(&input(&json)));
    }

    #[test]
    fn charlottesville_without_fixed_pairings_is_unchanged() {
        // The no-fixed-pairing path must stay the plain rotation (byte-identical):
        // a complete bipartite round robin with every cross pair exactly once.
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}]}}"#,
            rr_players_json(8),
            rr_rounds_json(4, "Charlottesville"),
        );
        let out = pair(&input(&json));
        assert!(out.iter().all(|r| r.error.is_none()), "{out:?}");
        let m = meetings(&out);
        assert_eq!(m.len(), 16);
        assert!(m.values().all(|&c| c == 1), "repeats: {m:?}");
    }

    #[test]
    fn dropping_out_of_played_round_robin_reports_error() {
        // P4 withdraws after playing round 1 of a round-robin block. The block
        // can't be re-paired around them, so the next round comes back empty
        // carrying a clear error (mirrors the Python engine's PairingError).
        let json = r#"{
            "players": [
                {"name": "P1", "rating": 1990},
                {"name": "P2", "rating": 1980},
                {"name": "P3", "rating": 1970},
                {"name": "P4", "rating": 1960, "dropped": true}
            ],
            "round_pairings": [
                {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
                {"round": 2, "start_round": 0, "pairing": "RoundRobin"},
                {"round": 3, "start_round": 0, "pairing": "RoundRobin"}
            ],
            "result_slips": [
                {"round": 1, "winner_name": "P1", "loser_name": "P4", "winner_score": 400, "loser_score": 300, "winner_started": true},
                {"round": 1, "winner_name": "P2", "loser_name": "P3", "winner_score": 400, "loser_score": 300, "winner_started": true}
            ]
        }"#;
        let out = pair(&input(json));
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert!(r2.pairings.is_empty());
        assert!(r2.error.as_deref().unwrap_or("").contains("withdrew"));
    }
}
