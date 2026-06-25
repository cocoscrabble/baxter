//! Tournament-level orchestration: walk the configured rounds, pair the ones
//! that are ready, and emit the result.

use std::collections::{HashMap, HashSet};

use rand_chacha::ChaCha8Rng;

use crate::model::{OutPairing, PairingInput, PlayerData, ResultSlipData, RoundResult};
use crate::rng::seeded;
use crate::round_pairing::{normalize_round_robin_start_rounds, RoundPairing, RP};
use crate::standings::{standings_after_round, Pairings, Player, Repeats, Starts};
use crate::strategies::{basic, quads, swiss, Ctx};

/// Name of the synthetic bye opponent (matches `Player::is_bye`).
const BYE_NAME: &str = "Bye";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RoundStatus {
    Empty,
    Partial,
    Finished,
}

fn status_of(status: &HashMap<i32, RoundStatus>, round: i32) -> RoundStatus {
    *status.get(&round).unwrap_or(&RoundStatus::Empty)
}

/// Dispatch to the strategy for this round.
fn run_strategy(rp: &RoundPairing, ctx: &mut Ctx) -> Pairings {
    match rp.pairing {
        RP::KotH => basic::pair_koth(ctx, rp),
        RP::QotH => basic::pair_qoth(ctx, rp),
        RP::Swiss => swiss::pair_swiss(ctx, rp),
        RP::RoundRobin => basic::pair_round_robin(ctx, rp),
        RP::DoubleRoundRobin => basic::pair_double_round_robin(ctx, rp),
        RP::Random => basic::pair_random(ctx, rp),
        RP::RandomNoRepeats => basic::pair_random_no_repeats(ctx, rp),
        RP::QuadsClustered => quads::pair_clustered_quads(ctx, rp),
        RP::QuadsDistributed => quads::pair_distributed_quads(ctx, rp),
        RP::QuadsEqualized => quads::pair_equalized_quads(ctx, rp),
        RP::Sixes => quads::pair_sixes(ctx, rp),
        RP::Charlottesville => basic::pair_charlottesville(ctx, rp),
        RP::SwissPlusRandom => swiss::pair_swiss_plus_random(ctx, rp),
        // Unknown strategy: pair nobody, like a `STRATEGIES.get` miss.
        RP::Unknown => Pairings::new(),
    }
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
    repeats: &Repeats,
    rng: &mut ChaCha8Rng,
    rp: &RoundPairing,
) -> Pairings {
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
            repeats,
            rng,
        };
        run_strategy(rp, &mut ctx)
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

    result
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
            let paired = pair_round(
                players,
                slips,
                &rps,
                &input.fixed_pairings,
                &repeats,
                &mut rng,
                rp,
            );
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
            });
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
}
