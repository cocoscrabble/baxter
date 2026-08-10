//! Tournament-level orchestration: walk the configured rounds, pair the ones
//! that are ready, and emit the result.

use std::collections::{HashMap, HashSet};

use rand_chacha::ChaCha8Rng;

use crate::model::{
    CopConfig, OutPairing, PairingInput, PlayerData, ResultSlipData, RoundResult, SwissConfig,
};
use crate::rng::seeded;
use crate::round_pairing::{normalize_round_robin_start_rounds, RoundPairing, RP};
use crate::standings::{
    standings_after_round, Pairing, Pairings, Player, Repeats, Starts, BYE_NAME,
};
use crate::strategies::{basic, cop, quads, roundrobin, swiss, Ctx};

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
        RP::SwissNoRepeats => swiss::pair_swiss_no_repeats(ctx, rp)?,
        RP::SwissMinRepeats => swiss::pair_swiss_min_repeats(ctx, rp)?,
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
        RP::Cop => cop::pair_cop(ctx, rp)?,
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
    inactive: &HashSet<String>,
) -> Option<(String, String)> {
    if rp.pairing.is_round_robin() || rp.pairing.is_quad() {
        return None;
    }
    // Players sitting the round out are not part of the field, so they neither
    // make it odd nor can receive the bye.
    let field = standings_after_round(players, slips, rp.start_round, inactive);
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

/// Status of every round that has any results.
///
/// A round is finished when every *real* (non-bye) player has a result — a game
/// or a bye. Counting real-player appearances (2 per game, 1 per bye) rather than
/// a fixed games-per-round makes this robust to a round with several byes
/// (absences/forfeits, common in imported historical data); such a round would
/// otherwise be stuck as Partial and never pair the next round.
///
/// Players marked inactive for a round are not expected to appear in it, so they
/// are subtracted from that round's target. Without this a round that reserved
/// anyone could never read as finished, and the round after it would never pair.
fn round_status(
    players: &[PlayerData],
    slips: &[ResultSlipData],
    inactive_players: &HashMap<i32, Vec<String>>,
) -> HashMap<i32, RoundStatus> {
    let n_real = players
        .iter()
        .filter(|e| !e.name.eq_ignore_ascii_case(BYE_NAME))
        .count();
    let mut appearances: HashMap<i32, usize> = HashMap::new();
    for s in slips {
        let real = [&s.winner_name, &s.loser_name]
            .iter()
            .filter(|name| !name.eq_ignore_ascii_case(BYE_NAME))
            .count();
        *appearances.entry(s.round).or_insert(0) += real;
    }
    let mut counts = HashMap::new();
    for (round, real) in appearances {
        let expected = n_real.saturating_sub(
            inactive_players
                .get(&round)
                .map(|names| {
                    names
                        .iter()
                        .filter(|n| !n.eq_ignore_ascii_case(BYE_NAME))
                        .count()
                })
                .unwrap_or(0),
        );
        let st = if real >= expected {
            RoundStatus::Finished
        } else if real > 0 {
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

/// Pairings reconstructed from a round's result slips (starter first).
fn extract_pairings(slips: &[ResultSlipData], round: i32) -> Pairings {
    let mut p = Pairings::new();
    for r in slips {
        if r.round == round {
            p.add_result_slip(r);
        }
    }
    p
}

/// Which record owns the start when a result slip and its published pairing
/// disagree about who went first.
///
/// `true` — the published pairing wins. The players were handed a board with a
/// starter named on it, and every later round's orientation is balanced against
/// that assignment, so a slip entered the other way round is treated as a
/// mis-keyed start rather than a re-decision. `false` would make the entered
/// result authoritative instead. Flipping this alone changes only the ledger;
/// the app-side rule that rewrites the stored slip lives in
/// `tournaments/starts.py::PUBLISHED_PAIRING_OWNS_THE_START` and must agree.
const PUBLISHED_ORIENTATION_WINS: bool = true;

/// Unordered key for a pair of names.
fn canon_pair(a: &str, b: &str) -> (String, String) {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

/// Everything already decided about a round, oriented starter-first: its result
/// slips, plus the saved orientation of every published game not yet played.
///
/// A published (or in-progress) round's printed first/second assignment is
/// authoritative from the moment it is published — waiting for results would let
/// a regeneration re-decide orientations the players have already been handed.
///
/// Each game is counted exactly once. A slip and a saved pairing may describe the
/// same game, in which case the slip says *who played whom* and the saved pairing
/// says *who started* (see `PUBLISHED_ORIENTATION_WINS`). A saved pairing whose
/// players turn up in some other game — a round edited after publishing — is
/// stale and dropped: a player named on a slip for the round is "covered", and
/// only the bye opponent is exempt, since one round can hold several byes.
///
/// Draft rounds contribute nothing: they carry no slips, and the caller leaves
/// them out of `published`.
fn replay_pairings(
    round: i32,
    slips: &[ResultSlipData],
    published: &HashMap<i32, Vec<(String, String)>>,
) -> Pairings {
    let saved: HashMap<(String, String), (&String, &String)> = published
        .get(&round)
        .into_iter()
        .flatten()
        .map(|(a, b)| (canon_pair(a, b), (a, b)))
        .collect();

    let mut ret = Pairings::new();
    let mut covered: HashSet<String> = HashSet::new();
    for played in extract_pairings(slips, round).pairings {
        let oriented = match saved.get(&canon_pair(&played.first.name, &played.second.name)) {
            Some((first, second)) if PUBLISHED_ORIENTATION_WINS => {
                Pairing::new(Player::new(*first), Player::new(*second))
            }
            _ => played,
        };
        for name in [&oriented.first.name, &oriented.second.name] {
            if !name.eq_ignore_ascii_case(BYE_NAME) {
                covered.insert(name.clone());
            }
        }
        ret.pairings.push(oriented);
    }
    for (first, second) in published.get(&round).into_iter().flatten() {
        if covered.contains(first) || covered.contains(second) {
            continue;
        }
        ret.add(Player::new(first), Player::new(second));
    }
    ret
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
    inactive_map: &HashMap<i32, Vec<String>>,
    repeats: &Repeats,
    rng: &mut ChaCha8Rng,
    cop_config: Option<&CopConfig>,
    swiss_config: &SwissConfig,
    rp: &RoundPairing,
) -> Result<Pairings, String> {
    let inactive: HashSet<String> = inactive_map
        .get(&rp.round)
        .map(|names| names.iter().cloned().collect())
        .unwrap_or_default();
    // The round-robin family (round robin, double round robin, Charlottesville)
    // honors fixed pairings inside the strategy — it schedules matchings across
    // the block rather than excluding players — so it must see the full field.
    // COP joins them: it owns bye assignment (via its weight graph) and reads
    // fixed pairings as its own "prepaired" constraints, so it too needs the full
    // field with no pre-injected bye. Skip the exclude/bye/append path entirely.
    if matches!(
        rp.pairing,
        RP::RoundRobin | RP::DoubleRoundRobin | RP::Charlottesville | RP::Cop
    ) {
        // These strategies see the whole field — `excluded` normally stays empty
        // because they resolve fixed pairings themselves rather than by removing
        // players. Reserved players are the one thing they must still not see,
        // and the exclusion set is the only thing that removes them: from round 1
        // onward the field comes out of the *results*, so dropping someone from
        // `players` would leave anyone who has already played still standing.
        let mut ctx = Ctx {
            players,
            slips,
            round_pairings,
            excluded: &inactive,
            fixed_pairings: fixed_map,
            published_pairings: published_map,
            repeats,
            rng,
            cop_config,
            swiss_config,
        };
        return run_strategy(rp, &mut ctx);
    }

    let mut fixed_pairs: Vec<(String, String)> =
        fixed_map.get(&rp.round).cloned().unwrap_or_default();

    if let Some(bye) = bye_pairing(players, slips, rp, &fixed_pairs, &inactive) {
        fixed_pairs.push(bye);
    }

    // Excluding a name keeps the strategy from seeing it: fixed players because
    // their game is already decided, inactive players because they have none.
    let mut excluded: HashSet<String> = inactive.clone();
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
            cop_config,
            swiss_config,
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
    let status = round_status(players, slips, &input.inactive_players);

    let mut ret: Vec<RoundResult> = Vec::new();
    for rp in &rps {
        // A round with any saved pairing or result is history, not a candidate
        // for pairing: replay it into the repeat/starts ledger so its games and
        // its first/second orientation carry into every later round. This covers
        // a finished round, a partially played one, and a published round whose
        // results are all still outstanding — all three are already printed.
        let saved = replay_pairings(rp.round, slips, &input.published_pairings);
        if !saved.is_empty() {
            for p in saved.pairings {
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
                &input.inactive_players,
                &repeats,
                &mut rng,
                input.cop_config.as_ref(),
                &input.swiss_config,
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
    fn round_with_multiple_byes_is_finished_and_pairs_next() {
        // Round 1 has one real game and two byes (four players, two absent). Every
        // real player is accounted for, so round 1 counts as finished and round 2
        // pairs off it — a round with several byes must not be stuck as Partial.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Swiss"},
                    {"round": 2, "start_round": 1, "pairing": "Swiss"}
                ],
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 1, "winner_name": "C", "loser_name": "Bye", "winner_score": 50, "loser_score": 0, "winner_started": false},
                    {"round": 1, "winner_name": "D", "loser_name": "Bye", "winner_score": 50, "loser_score": 0, "winner_started": false}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert!(r2.error.is_none(), "{r2:?}");
        assert_eq!(r2.pairings.len(), 2);
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

    /// SwissPlusRandom pairs the top `spr_split` players Swiss and the rest
    /// RandomNoRepeats. With the whole field in the Swiss slice the bottom
    /// players pair by standings adjacency; shrinking the split hands them to
    /// the random pool, which pairs them differently.
    #[test]
    fn swiss_config_spr_split_sizes_the_swiss_slice() {
        let players: Vec<String> = (1..=14)
            .map(|i| format!(r#"{{"name": "P{i}", "rating": {}}}"#, 2000 - i * 10))
            .collect();
        let slips: Vec<String> = (1..=7)
            .map(|i| {
                format!(
                    r#"{{"round": 1, "winner_name": "P{}", "loser_name": "P{}", "winner_score": 500, "loser_score": 400, "winner_started": true}}"#,
                    i,
                    i + 7
                )
            })
            .collect();
        let body = format!(
            r#""players": [{}], "result_slips": [{}],
               "round_pairings": [
                 {{"round": 1, "start_round": 0, "pairing": "Swiss"}},
                 {{"round": 2, "start_round": 1, "pairing": "SwissPlusRandom"}}
               ]"#,
            players.join(","),
            slips.join(","),
        );
        let round2 = |cfg: &str| -> Vec<(String, String)> {
            let inp = input(&format!(r#"{{{body}{cfg}}}"#));
            let out = pair(&inp);
            let r2 = out.iter().find(|r| r.round == 2).unwrap();
            assert!(r2.error.is_none(), "{r2:?}");
            assert_eq!(r2.pairings.len(), 7);
            let mut pairs: Vec<(String, String)> = r2
                .pairings
                .iter()
                .map(|p| {
                    let mut n = [p.first.clone(), p.second.clone()];
                    n.sort();
                    (n[0].clone(), n[1].clone())
                })
                .collect();
            pairs.sort();
            pairs
        };

        // Whole field paired Swiss: the 0-win block pairs off adjacent standings.
        let all_swiss = round2(r#", "swiss_config": {"spr_split": 14}"#);
        assert!(all_swiss.contains(&("P11".into(), "P12".into())), "{all_swiss:?}");
        assert!(all_swiss.contains(&("P13".into(), "P14".into())), "{all_swiss:?}");

        // A split of 4 pushes P5..P14 into the random pool, breaking that adjacency.
        let split4 = round2(r#", "swiss_config": {"spr_split": 4}"#);
        assert_ne!(all_swiss, split4);
        assert!(!split4.contains(&("P13".into(), "P14".into())), "{split4:?}");
    }

    /// `swiss_weight` trades a repeat off against standings distance. Two players
    /// who have already met are paired anyway when the weight is low enough that
    /// the distance penalty of avoiding them dominates.
    #[test]
    fn swiss_config_weight_changes_repeat_avoidance() {
        // P1..P4 all on 1 win after beating P5..P8; P1 and P2 have already met.
        let body = r#""players": [
                {"name": "P1", "rating": 1900}, {"name": "P2", "rating": 1890},
                {"name": "P3", "rating": 1880}, {"name": "P4", "rating": 1870},
                {"name": "P5", "rating": 1860}, {"name": "P6", "rating": 1850},
                {"name": "P7", "rating": 1840}, {"name": "P8", "rating": 1830}
            ],
            "result_slips": [
                {"round": 1, "winner_name": "P1", "loser_name": "P2", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P3", "loser_name": "P4", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P5", "loser_name": "P6", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P7", "loser_name": "P8", "winner_score": 500, "loser_score": 400, "winner_started": true}
            ],
            "round_pairings": [
                {"round": 1, "start_round": 0, "pairing": "Swiss"},
                {"round": 2, "start_round": 1, "pairing": "Swiss"}
            ]"#;
        let repeat_paired = |cfg: &str| -> bool {
            let inp = input(&format!(r#"{{{body}{cfg}}}"#));
            let out = pair(&inp);
            let r2 = out.iter().find(|r| r.round == 2).unwrap();
            assert!(r2.error.is_none(), "{r2:?}");
            r2.pairings
                .iter()
                .any(|p| matches!((p.first.as_str(), p.second.as_str()), ("P1", "P2") | ("P2", "P1")))
        };
        // A heavy repeat penalty must keep the rematch off the board.
        assert!(!repeat_paired(r#", "swiss_config": {"swiss_weight": 1000}"#));
    }

    /// max_distance gates which candidate edges exist at all; setting it to 1
    /// removes every edge (distance is always >= 1), so the round cannot pair.
    #[test]
    fn swiss_config_max_distance_gates_candidate_edges() {
        let body = r#""players": [
                {"name": "P1", "rating": 1900}, {"name": "P2", "rating": 1890},
                {"name": "P3", "rating": 1880}, {"name": "P4", "rating": 1870},
                {"name": "P5", "rating": 1860}, {"name": "P6", "rating": 1850},
                {"name": "P7", "rating": 1840}, {"name": "P8", "rating": 1830}
            ],
            "result_slips": [
                {"round": 1, "winner_name": "P1", "loser_name": "P5", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P2", "loser_name": "P6", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P3", "loser_name": "P7", "winner_score": 500, "loser_score": 400, "winner_started": true},
                {"round": 1, "winner_name": "P4", "loser_name": "P8", "winner_score": 500, "loser_score": 400, "winner_started": true}
            ],
            "round_pairings": [
                {"round": 1, "start_round": 0, "pairing": "Swiss"},
                {"round": 2, "start_round": 1, "pairing": "Swiss"}
            ]"#;
        let inp = input(&format!(r#"{{{body}}}"#));
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert!(r2.error.is_none(), "{r2:?}");
        assert_eq!(r2.pairings.len(), 4);

        // With no admissible edges the top group never pairs, so the round comes
        // back empty rather than silently inventing pairings.
        let inp = input(&format!(
            r#"{{{body}, "swiss_config": {{"max_distance": 1}}}}"#
        ));
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert!(
            r2.pairings.len() < 4 || r2.error.is_some(),
            "max_distance=1 should not produce a full round: {r2:?}"
        );
    }

    /// Score groups key on match points, not wins, so a player who has drawn is
    /// grouped with the players they are actually level with.
    ///
    /// P1 draws twice and wins twice: 2 wins + 2 draws = 3 points, the same as
    /// the 3-1 players, and adjacent to them in the standings. Keying groups on
    /// wins (as the Google Sheets original does) drops P1 into the 2-win group
    /// and pairs them a full point down, even though the 3-point players are
    /// unmet and available.
    #[test]
    fn swiss_score_groups_keep_a_drawing_player_with_their_points_peers() {
        fn game(round: i32, w: &str, l: &str, drawn: bool) -> String {
            let (ws, ls) = if drawn { (450, 450) } else { (500, 400) };
            format!(
                r#"{{"round": {round}, "winner_name": "{w}", "loser_name": "{l}",
                     "winner_score": {ws}, "loser_score": {ls}, "winner_started": true}}"#
            )
        }
        let players: Vec<String> = (1..=12)
            .map(|i| format!(r#"{{"name": "P{i}", "rating": {}}}"#, 2000 - i * 10))
            .collect();
        // P1 draws P2 and P3, then beats P4 and P6 — so P1 has met none of the
        // other 3-point players and repeats cannot be what separates them.
        let slips = [
            game(1, "P1", "P2", true), game(1, "P3", "P4", false), game(1, "P5", "P6", false),
            game(1, "P7", "P8", false), game(1, "P9", "P10", false), game(1, "P11", "P12", false),
            game(2, "P1", "P3", true), game(2, "P2", "P4", false), game(2, "P5", "P8", false),
            game(2, "P7", "P6", false), game(2, "P9", "P11", false), game(2, "P10", "P12", false),
            game(3, "P1", "P4", false), game(3, "P2", "P3", false), game(3, "P5", "P9", false),
            game(3, "P7", "P11", false), game(3, "P6", "P10", false), game(3, "P8", "P12", false),
            game(4, "P1", "P6", false), game(4, "P2", "P5", false), game(4, "P9", "P7", false),
            game(4, "P3", "P8", false), game(4, "P4", "P12", false), game(4, "P11", "P10", false),
        ];
        let rps: Vec<String> = (1..=5)
            .map(|r| format!(r#"{{"round": {r}, "start_round": {}, "pairing": "Swiss"}}"#, r - 1))
            .collect();
        let inp = input(&format!(
            r#"{{"players": [{}], "result_slips": [{}], "round_pairings": [{}]}}"#,
            players.join(","),
            slips.join(","),
            rps.join(","),
        ));
        let out = pair(&inp);
        let r5 = out.iter().find(|r| r.round == 5).unwrap();
        assert!(r5.error.is_none(), "{r5:?}");

        let opponent = r5
            .pairings
            .iter()
            .find_map(|p| match (p.first.as_str(), p.second.as_str()) {
                ("P1", other) | (other, "P1") => Some(other.to_string()),
                _ => None,
            })
            .expect("P1 must be paired");
        // P5, P7 and P9 are the other 3-point players; P2 is on 3.5. Anything
        // else means P1 was pulled out of their score group.
        assert!(
            ["P2", "P5", "P7", "P9"].contains(&opponent.as_str()),
            "P1 (3 points) was paired with {opponent}, outside their score group"
        );
    }

    #[test]
    fn minimal_repeat_swiss_avoids_repeats_when_a_no_repeat_matching_exists() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Swiss"},
                    {"round": 2, "start_round": 1, "pairing": "SwissMinRepeats"}
                ],
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 1, "winner_name": "C", "loser_name": "D", "winner_score": 400, "loser_score": 350, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let round = out.iter().find(|round| round.round == 2).unwrap();

        assert!(round.error.is_none(), "{round:?}");
        assert_eq!(round.pairings.len(), 2);
        assert!(round.pairings.iter().all(|pairing| pairing.repeats == 1));
    }

    #[test]
    fn minimal_repeat_swiss_repeats_when_a_repeat_is_unavoidable() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Swiss"},
                    {"round": 2, "start_round": 1, "pairing": "SwissMinRepeats"}
                ],
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 350, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let round = out.iter().find(|round| round.round == 2).unwrap();

        assert!(round.error.is_none(), "{round:?}");
        assert_eq!(round.pairings.len(), 1);
        assert_eq!(round.pairings[0].repeats, 2);
    }

    #[test]
    fn no_repeat_swiss_never_repeats_when_a_perfect_matching_exists() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Swiss"},
                    {"round": 2, "start_round": 1, "pairing": "SwissNoRepeats"}
                ],
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 1, "winner_name": "C", "loser_name": "D", "winner_score": 400, "loser_score": 350, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out.len(), 1);
        assert!(out[0].error.is_none(), "{out:?}");
        let pairs: HashSet<(String, String)> = out[0]
            .pairings
            .iter()
            .map(|p| {
                let mut names = [p.first.clone(), p.second.clone()];
                names.sort();
                (names[0].clone(), names[1].clone())
            })
            .collect();
        assert!(!pairs.contains(&("A".into(), "B".into())));
        assert!(!pairs.contains(&("C".into(), "D".into())));
        assert_eq!(pairs.len(), 2);
    }

    #[test]
    fn no_repeat_swiss_reports_when_repeat_free_pairing_is_impossible() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 1, "pairing": "RoundRobin"},
                    {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
                    {"round": 3, "start_round": 1, "pairing": "RoundRobin"},
                    {"round": 4, "start_round": 3, "pairing": "SwissNoRepeats"}
                ],
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 1, "winner_name": "C", "loser_name": "D", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 2, "winner_name": "A", "loser_name": "C", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 2, "winner_name": "B", "loser_name": "D", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 3, "winner_name": "A", "loser_name": "D", "winner_score": 400, "loser_score": 350, "winner_started": true},
                    {"round": 3, "winner_name": "B", "loser_name": "C", "winner_score": 400, "loser_score": 350, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let round = out.iter().find(|round| round.round == 4).unwrap();
        assert!(round.pairings.is_empty());
        assert!(round
            .error
            .as_deref()
            .unwrap_or("")
            .contains("no repeat-free Swiss pairing"));
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
    fn equalized_quads_make_four_quads_and_one_hex_for_22_players() {
        let players: Vec<String> = (0..22)
            .map(|i| format!(r#"{{"name":"P{}","rating":{}}}"#, i + 1, 2000 - 10 * i))
            .collect();
        let rounds: Vec<String> = (1..=3)
            .map(|round| {
                format!(
                    r#"{{"round":{round},"start_round":0,"pairing":"Quads_Equalized"}}"#
                )
            })
            .collect();
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}]}}"#,
            players.join(","),
            rounds.join(",")
        );

        let out = pair(&input(&json));
        assert_eq!(out.len(), 3);
        let mut opponents: HashMap<String, HashSet<String>> = HashMap::new();
        let mut meetings: HashSet<(String, String)> = HashSet::new();
        for round in &out {
            assert!(round.error.is_none(), "{round:?}");
            assert_eq!(round.pairings.len(), 11);
            for pairing in &round.pairings {
                let mut names = [pairing.first.clone(), pairing.second.clone()];
                names.sort();
                assert!(meetings.insert((names[0].clone(), names[1].clone())));
                opponents
                    .entry(pairing.first.clone())
                    .or_default()
                    .insert(pairing.second.clone());
                opponents
                    .entry(pairing.second.clone())
                    .or_default()
                    .insert(pairing.first.clone());
            }
        }

        let mut component_sizes = Vec::new();
        let mut unseen: HashSet<String> = opponents.keys().cloned().collect();
        while let Some(first) = unseen.iter().next().cloned() {
            let mut stack = vec![first];
            let mut size = 0;
            while let Some(player) = stack.pop() {
                if !unseen.remove(&player) {
                    continue;
                }
                size += 1;
                stack.extend(opponents[&player].iter().cloned());
            }
            component_sizes.push(size);
        }
        component_sizes.sort();
        assert_eq!(component_sizes, vec![4, 4, 4, 4, 6]);
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

    // -- inactive players ------------------------------------------------
    //
    // A player marked inactive for a round sits it out entirely: no game, no
    // bye, and not withdrawn — the rest of the field pairs around them.

    #[test]
    fn inactive_players_are_left_out_of_the_round() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}],
                "inactive_players": {"1": ["A", "B"]}
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out[0].pairings.len(), 1);
        let p = &out[0].pairings[0];
        let mut names = [p.first.clone(), p.second.clone()];
        names.sort();
        assert_eq!(names, ["C".to_string(), "D".to_string()]);
    }

    #[test]
    fn an_inactive_player_never_receives_the_bye() {
        // Five players with one reserved leaves an even field: no bye at all.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600},
                    {"name": "E", "rating": 1500}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}],
                "inactive_players": {"1": ["E"]}
            }"#,
        );
        let out = pair(&inp);
        assert_eq!(out[0].pairings.len(), 2);
        for p in &out[0].pairings {
            assert!(p.first != "Bye" && p.second != "Bye");
            assert!(p.first != "E" && p.second != "E");
        }
    }

    #[test]
    fn reserving_a_player_makes_an_even_field_odd_and_someone_gets_the_bye() {
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "KotH"}],
                "inactive_players": {"1": ["D"]}
            }"#,
        );
        let out = pair(&inp);
        let has_bye = out[0]
            .pairings
            .iter()
            .any(|p| p.first == "Bye" || p.second == "Bye");
        assert!(has_bye, "the remaining three should leave one player byed");
        // …and it is not the reserved player who gets it.
        assert!(!out[0]
            .pairings
            .iter()
            .any(|p| p.first == "D" || p.second == "D"));
    }

    #[test]
    fn a_round_missing_only_its_inactive_players_still_counts_as_finished() {
        // Without this, a round that reserved anyone would never read as
        // finished and the round after it would never pair.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 1, "pairing": "KotH"}
                ],
                "inactive_players": {"1": ["C", "D"]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 300, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2);
        assert!(r2.is_some(), "round 2 should have paired off round 1");
        assert_eq!(r2.unwrap().pairings.len(), 2);
    }

    #[test]
    fn cop_pairs_the_field_left_after_a_reservation() {
        // COP takes the whole-field path, so a reserved player is removed from
        // the field it is handed rather than excluded around it.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600},
                    {"name": "E", "rating": 1500},
                    {"name": "F", "rating": 1400}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 1, "pairing": "COP"}
                ],
                "cop_config": {"place_prizes": 3, "simulations": 20, "always_wins_simulations": 20},
                "inactive_players": {"2": ["A", "B"]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 300, "winner_started": true},
                    {"round": 1, "winner_name": "C", "loser_name": "D", "winner_score": 400, "loser_score": 300, "winner_started": true},
                    {"round": 1, "winner_name": "E", "loser_name": "F", "winner_score": 400, "loser_score": 300, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert_eq!(r2.error, None);
        assert_eq!(r2.pairings.len(), 2);
        for p in &r2.pairings {
            assert!(p.first != "A" && p.second != "A");
            assert!(p.first != "B" && p.second != "B");
        }
    }

    // -- published pairings in the start ledger --------------------------
    //
    // A published round is already printed: its games and its first/second
    // orientation are history from that moment, not from when its results land.

    #[test]
    fn a_published_round_with_no_results_is_replayed_not_repaired() {
        // Round 1 was published with B first — the opposite of what the engine
        // picks on its own — so round 2 must give the start back to A.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 0, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["B", "A"]]}
            }"#,
        );
        let out = pair(&inp);
        assert!(
            out.iter().all(|r| r.round != 1),
            "a published round must not be re-paired"
        );
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert_eq!(r2.pairings[0].first, "A");
        assert_eq!(r2.pairings[0].repeats, 2, "round 1's game must count");
    }

    #[test]
    fn a_partial_round_contributes_each_saved_start_exactly_once() {
        // Round 1 is published in full but only A-B has been played. A's start
        // comes from the slip, C's from the saved pairing: one apiece. Round 2
        // pins A against C, so the tie (rather than A leading 2-1) is the
        // assertion that neither was counted twice.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 0, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["A", "B"], ["C", "D"]]},
                "fixed_pairings": {"2": [["A", "C"]]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 300, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        let ac = r2
            .pairings
            .iter()
            .find(|p| p.first == "A" || p.first == "C")
            .unwrap();
        assert_eq!(ac.first, "A", "A and C are level on starts");
    }

    #[test]
    fn a_fully_finished_round_is_not_double_counted_with_its_pairings() {
        // Every game has both a slip and a saved pairing. B and D each started
        // once, so round 2's pin between them is a tie broken by order — the
        // same answer a slips-only ledger gives.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 1, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["B", "A"], ["D", "C"]]},
                "fixed_pairings": {"2": [["A", "B"]]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 300, "winner_started": false},
                    {"round": 1, "winner_name": "C", "loser_name": "D", "winner_score": 400, "loser_score": 300, "winner_started": false}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        let ab = r2
            .pairings
            .iter()
            .find(|p| p.first == "A" || p.first == "B")
            .unwrap();
        assert_eq!(ab.first, "A", "B started round 1 and A did not");
        assert_eq!(ab.repeats, 2);
    }

    #[test]
    fn a_published_bye_charges_nobody_a_start() {
        // The bye opponent is the notional starter, so A sits round 1 out
        // without being charged. Round 2 pins A against C, who also has no
        // start: level, so A (named first) goes first.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 0, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["Bye", "A"], ["B", "C"]]},
                "fixed_pairings": {"2": [["A", "C"]]}
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        let ac = r2
            .pairings
            .iter()
            .find(|p| p.first == "A" || p.first == "C")
            .unwrap();
        assert_eq!(ac.first, "A");
    }

    #[test]
    fn a_result_entered_against_the_published_start_defers_to_the_board() {
        // Round 1 was published with B first, but the result was keyed with A as
        // the starter. The printed board owns the start, so B carries round 1's
        // start and round 2 gives it to A. Were the slip to win instead, the
        // orientation would be the other way round.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 1, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["B", "A"]]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "B", "winner_score": 400, "loser_score": 300, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert_eq!(r2.pairings[0].first, "A");
    }

    #[test]
    fn a_published_round_edited_after_publishing_defers_to_its_results() {
        // The saved pairing says A-B and C-D, but A and C actually played each
        // other. The board only owns the *start*, never who played whom: the
        // stale saved pairs are dropped rather than replayed, which would charge
        // A (and C) a second start and count a game that never existed. B and D
        // are still covered by their saved pairing.
        let inp = input(
            r#"{
                "players": [
                    {"name": "A", "rating": 1900},
                    {"name": "B", "rating": 1800},
                    {"name": "C", "rating": 1700},
                    {"name": "D", "rating": 1600}
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "KotH"},
                    {"round": 2, "start_round": 0, "pairing": "KotH"}
                ],
                "published_pairings": {"1": [["A", "B"], ["C", "D"]]},
                "fixed_pairings": {"2": [["A", "D"]]},
                "result_slips": [
                    {"round": 1, "winner_name": "A", "loser_name": "C", "winner_score": 400, "loser_score": 300, "winner_started": true}
                ]
            }"#,
        );
        let out = pair(&inp);
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        let ad = r2
            .pairings
            .iter()
            .find(|p| p.first == "A" || p.first == "D")
            .unwrap();
        // A started once (the slip); D never started, so D goes first.
        assert_eq!(ad.first, "D");
        assert_eq!(ad.repeats, 1, "A and D have not met");
    }
}
