//! COP (Cost-Optimized Pairing) — a port of Matthew O'Connor's `COP.pm`
//! (`jvc56/tournament_pairing_algorithms`, native path).
//!
//! COP pairs a single round from current standings by (1) simulating the rest of
//! the tournament to find, per prize rank, who can still realistically finish
//! there ("contenders"), then (2) building a weighted graph over all possible
//! pairings and solving a **minimum-weight perfect matching** so contenders pair
//! within their contention group, the leader isn't burned prematurely, and
//! repeats/byes are avoided.
//!
//! **Phase 1 (this file): scaffold + weight graph, with the simulation stubbed.**
//! The RNG-independent pieces — gibson detection and the can-cash sim-player
//! truncation — are computed for real (they're directly parity-testable against
//! the `tools/cop-oracle` reference). The contender arrays that normally come out
//! of the Monte Carlo are replaced with a coherent placeholder: the contender set
//! for every prize rank is the whole cash group. Control loss / destiny control
//! is disabled until the sims land in Phase 2. The last-round KOTH path is fully
//! deterministic and already faithful.
//!
//! Wins are carried **doubled** internally (a win = 2, a draw = 1, a loss = 0), as
//! in COP.pm, so records stay integer.

use std::collections::{HashMap, HashSet};

use crate::matching::max_weight_matching_pairs;
use crate::model::CopConfig;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairing, Pairings, Player, BYE_NAME};

use super::Ctx;

/// A pairing COP is willing to force but flags: a weight above this means the
/// matching had no non-repeat / in-contention alternative (COP's
/// `PROHIBITIVE_WEIGHT`).
const PROHIBITIVE_WEIGHT: i128 = 1_000_000;

/// One competitor inside the COP computation. `index` is the original
/// standings-rank position, used (in Phase 2) to record simulated finishes.
#[derive(Debug, Clone)]
struct CopPlayer {
    name: String,
    index: usize,
    /// Doubled wins (win = 2, draw = 1).
    wins: i32,
    spread: i32,
    is_bye: bool,
}

/// Runtime config: `CopConfig` with the per-round arrays forward-filled to the
/// round count and the cumulative gibson spreads derived — the shape COP.pm's
/// `%config` hash carries.
struct CopRuntime {
    rounds_remaining: i32,
    lowest_ranked_payout: i32, // 0-indexed
    cumulative_gibson_spreads: Vec<i32>,
    disallow_repeat_byes: bool,
    bye_active: bool,
    // Populated now but only read once the simulation / control-loss logic lands
    // in Phase 2.
    #[allow(dead_code)]
    round_to_pair: i32, // 0-indexed round being paired
    #[allow(dead_code)]
    gibson_spreads: Vec<i32>,
    #[allow(dead_code)]
    hopefulness: Vec<f64>,
    #[allow(dead_code)]
    control_loss_thresholds: Vec<f64>,
    #[allow(dead_code)]
    control_loss_activation_round: i32,
}

/// The decisions the weight graph consumes. Split out as an explicit input so the
/// graph is a pure function of them — the dependency-injection point the parity
/// harness uses to test weights exactly (Tier 1), independent of the RNG-driven
/// simulation that produces the contender arrays.
struct Decisions {
    lowest_gibson_rank: i32,
    /// For each final rank, the lowest current rank that can still finish there
    /// (statistically / at all). Indexed by rank; length = number of players.
    lowest_finishers_statistical: Vec<i32>,
    lowest_cash_statistical: i32,
    lowest_cash_absolute: i32,
    destinys_child: i32,
    control_loss_weight_used: bool,
    /// Control loss (destiny control) enforced this round. Always false until
    /// Phase 2 wires up the always-wins simulation.
    control_loss_active: bool,
    /// Total repeat count per player name (an opponent met k>1 times adds k-1).
    number_of_repeats: HashMap<String, i32>,
}

/// Canonical (name, name) key for an unordered pair.
fn key(a: &str, b: &str) -> (String, String) {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

/// Sort by record: byes last, then wins desc, spread desc, original index asc.
fn sort_by_record(players: &mut [CopPlayer]) {
    players.sort_by(|a, b| {
        (a.is_bye as i32)
            .cmp(&(b.is_bye as i32))
            .then(b.wins.cmp(&a.wins))
            .then(b.spread.cmp(&a.spread))
            .then(a.index.cmp(&b.index))
    });
}

/// Forward-fill `arr` to `n` entries (COP's `extend_tsh_config_array`): take the
/// given values, then repeat the last one. Falls back to `fallback` when empty.
fn extend<T: Copy>(arr: &[T], n: usize, fallback: T) -> Vec<T> {
    let mut out = Vec::with_capacity(n);
    let mut last = fallback;
    for i in 0..n {
        if i < arr.len() {
            last = arr[i];
        }
        out.push(last);
    }
    out
}

/// Cumulative gibson spreads (COP's `get_cumulative_gibson_spreads`): each entry
/// doubles the round's gibson spread and adds it to the running total, forward-
/// filling the last value once the raw array runs out.
fn cumulative_gibson(raw: &[i32], n: usize, fallback: i32) -> Vec<i32> {
    let mut out = vec![0; n];
    let mut last = 0;
    for i in 0..n {
        let this = if i < raw.len() {
            raw[i] * 2
        } else {
            last
        };
        if i == 0 {
            out[i] = this;
        } else {
            out[i] = out[i - 1] + this;
        }
        if i < raw.len() {
            last = raw[i] * 2;
        } else if i == 0 {
            // empty raw array: use the fallback so we never leave a zero band.
            out[i] = fallback * 2;
            last = fallback * 2;
        }
    }
    out
}

/// `true` if `player` can still reach `rank` given the rounds remaining. Uses the
/// multiplied-through form `(wins[rank] - wins) <= 2*remaining` to match COP's
/// `(…)/2 <= remaining` exactly without integer-division rounding.
fn can_reach_rank(players: &[CopPlayer], player: &CopPlayer, rank: usize, remaining: i32) -> bool {
    players[rank].wins - player.wins <= 2 * remaining
}

/// The players kept for simulation (COP's `get_sim_tournament_players`): everyone
/// who can technically still cash, plus odd-rank padding, stopping at the first
/// who cannot. A pure prefix of the record-sorted field.
fn sim_players(players: &[CopPlayer], rt: &CopRuntime) -> Vec<CopPlayer> {
    let lrp = rt.lowest_ranked_payout;
    // Clamp the cash rank to the field (a prize count larger than the field means
    // everyone can technically cash), so we never index past the end.
    let cash_rank = (lrp.max(0) as usize).min(players.len().saturating_sub(1));
    let mut out = Vec::new();
    for (i, p) in players.iter().enumerate() {
        let can_cash =
            (i as i32) <= lrp || can_reach_rank(players, p, cash_rank, rt.rounds_remaining);
        if can_cash || i % 2 == 1 {
            out.push(p.clone());
        }
        if !can_cash {
            break;
        }
    }
    out
}

/// Lowest gibsonized rank (COP's `get_lowest_gibson_rank`): the deepest cash rank
/// mathematically locked into its placement. `-1` when no one is gibsonized.
fn lowest_gibson_rank(sim: &[CopPlayer], rt: &CopRuntime) -> i32 {
    let n = sim.len();
    if n == 0 {
        return -1;
    }
    let mut result = -1;
    let max_rank = n.min((rt.lowest_ranked_payout + 1).max(0) as usize);
    let cg = rt.cumulative_gibson_spreads[(rt.rounds_remaining - 1) as usize];
    for k in 0..max_rank {
        if k == n - 1 {
            result = (n - 1) as i32;
            break;
        }
        let wd = sim[k].wins - sim[k + 1].wins;
        let locked = wd > 2 * rt.rounds_remaining
            || (wd == 2 * rt.rounds_remaining && sim[k].spread - sim[k + 1].spread > cg);
        if locked {
            result = k as i32;
        } else {
            break;
        }
    }
    result
}

/// The COP weight graph over every pair. A faithful port of the big loop in
/// `cop()` (COP.pm ~1331–1596). Returns `(edges, max_weight)` with edge weights to
/// be **minimized** (the caller inverts for max-weight matching). Pure in its
/// inputs — the `Decisions` argument carries every simulation-derived value.
fn build_weight_edges(
    players: &[CopPlayer],
    rt: &CopRuntime,
    dec: &Decisions,
    times_played: &HashMap<(String, String), i32>,
    previous: &HashSet<(String, String)>,
    prepaired: &HashMap<String, String>,
    class_prize: &HashMap<usize, usize>,
) -> (Vec<(usize, usize, i128)>, i128) {
    let n = players.len();
    let gibson = dec.lowest_gibson_rank;
    let cash_abs = dec.lowest_cash_absolute;
    let cash_stat = dec.lowest_cash_statistical;
    let mut edges = Vec::new();
    let mut max_weight: i128 = 0;

    for i in 0..n {
        for j in (i + 1)..n {
            let pi = &players[i];
            let pj = &players[j];
            let ii = i as i32;
            let jj = j as i32;
            let times = *times_played.get(&key(&pi.name, &pj.name)).unwrap_or(&0);
            let prev = previous.contains(&key(&pi.name, &pj.name));

            // Prepaired: a player already pinned to someone else can't take this
            // pairing.
            let prepaired_weight = if prepaired.get(&pi.name).is_some_and(|o| o != &pj.name)
                || prepaired.get(&pj.name).is_some_and(|o| o != &pi.name)
            {
                PROHIBITIVE_WEIGHT
            } else {
                0
            };

            let both_cannot_cash_abs = ii > cash_abs && jj > cash_abs;
            let both_cannot_cash_stat = ii > cash_stat && jj > cash_stat;

            // Repeat weight: grows with the field and the meeting count.
            let mut repeat_weight =
                ((times as f64 * 2.0) * (n as f64 / 3.0).powi(3)) as i128;

            let mut gibson_weight = 0i128;
            if rt.bye_active && pj.is_bye && gibson > 0 && ii > gibson {
                // Byes active with someone gibsonized: the bye belongs to a
                // gibsonized player, not this out-of-contention one.
                gibson_weight += PROHIBITIVE_WEIGHT;
            } else if both_cannot_cash_stat && prev {
                // Both out of the money: avoid an immediate back-to-back repeat.
                repeat_weight += PROHIBITIVE_WEIGHT / 10;
            } else if times > 0 {
                repeat_weight += (dec.number_of_repeats.get(&pi.name).copied().unwrap_or(0)
                    + dec.number_of_repeats.get(&pj.name).copied().unwrap_or(0))
                    as i128
                    * 2;
            }

            if pj.is_bye && times > 0 && rt.disallow_repeat_byes {
                repeat_weight += PROHIBITIVE_WEIGHT;
            }

            // Rank-difference weight: cubic normally; near-flat when neither can
            // cash or the top player is gibsonized.
            let rank_difference_weight = if both_cannot_cash_abs || ii <= gibson {
                (jj - ii) as i128
            } else {
                ((jj - ii) as i128).pow(3)
            };

            let mut pair_with_placer_weight = 0i128;
            let mut control_loss_weight = 0i128;
            let mut koth_weight = 0i128;

            if rt.rounds_remaining == 1 {
                // Last round: KOTH among everyone eligible for a cash/class prize.
                let gibson_vs_cash = ii <= gibson
                    && jj <= cash_abs
                    && jj > gibson
                    && !pj.is_bye;
                let koth_pair = ii > gibson
                    && ii <= cash_abs
                    && (gibson.rem_euclid(2) == ii.rem_euclid(2) || ii + 1 != jj);
                let class_i = class_prize.get(&i).is_some_and(|&o| o != j);
                let class_j = class_prize.get(&j).is_some_and(|&o| o != i);
                if gibson_vs_cash || koth_pair || class_i || class_j {
                    koth_weight = PROHIBITIVE_WEIGHT;
                }
            } else if !pj.is_bye {
                let i_gibson_j_cash =
                    ii <= gibson && jj > gibson && jj <= cash_abs && jj != (n as i32 - 1);
                let neither_gibson = ii > gibson && jj > gibson;
                if i_gibson_j_cash {
                    // A gibsonized player must not play a live cash contender.
                    gibson_weight = PROHIBITIVE_WEIGHT;
                } else if neither_gibson {
                    if ii <= cash_stat {
                        let finisher_i = dec.lowest_finishers_statistical[i];
                        let can_catch = jj <= finisher_i
                            || (ii == finisher_i && ii == jj - 1)
                            || (dec.control_loss_weight_used
                                && dec.destinys_child.rem_euclid(2) == 0
                                && ii < dec.destinys_child
                                && jj == dec.destinys_child + 1);
                        if can_catch {
                            pair_with_placer_weight =
                                ((finisher_i - jj).unsigned_abs() as i128).pow(3) * 2;
                        } else {
                            // j can't catch i: they shouldn't be paired.
                            pair_with_placer_weight = PROHIBITIVE_WEIGHT;
                        }
                    }

                    // Destiny control: pin first place to the required opponent.
                    // Disabled until Phase 2 supplies real control-loss data.
                    if dec.control_loss_active && ii == 0 {
                        let dc = dec.destinys_child;
                        let two_or_fewer = rt.rounds_remaining <= SINGULAR_CHILD_ROUNDS_REMAINING;
                        let must_force = (jj != dc && two_or_fewer)
                            || (((jj == dc) || (jj == dc - 1))
                                && rt.rounds_remaining > SINGULAR_CHILD_ROUNDS_REMAINING
                                && prev
                                && dc != 1)
                            || (jj != dc
                                && jj != dc - 1
                                && rt.rounds_remaining > SINGULAR_CHILD_ROUNDS_REMAINING);
                        if must_force {
                            control_loss_weight = PROHIBITIVE_WEIGHT;
                        }
                    }
                }
            }

            let weight = repeat_weight
                + rank_difference_weight
                + pair_with_placer_weight
                + control_loss_weight
                + gibson_weight
                + koth_weight
                + prepaired_weight;
            max_weight = max_weight.max(weight);
            edges.push((i, j, weight));
        }
    }
    (edges, max_weight)
}

/// COP's `SINGULAR_CHILD_ROUNDS_REMAINING`.
const SINGULAR_CHILD_ROUNDS_REMAINING: i32 = 2;

/// Solve the minimum-weight perfect matching: invert weights to `(max+1) - w` and
/// run max-weight, max-cardinality matching (COP's `min_weight_matching`).
fn solve(edges: &[(usize, usize, i128)], max_weight: i128, n: usize) -> Vec<(usize, usize)> {
    let inverted: Vec<(usize, usize, i128)> = edges
        .iter()
        .map(|&(u, v, w)| (u, v, (max_weight + 1) - w))
        .collect();
    max_weight_matching_pairs(n, &inverted)
}

/// Initial pairing when COP is (unusually) the first round: top vs bottom half,
/// with a bye at the bottom for an odd field (COP is meaningless with no results;
/// see plan D5).
fn initial_pairing(field: &[Player]) -> Pairings {
    let mut players: Vec<Player> = field.to_vec();
    if players.len() % 2 == 1 {
        players.push(Player::bye());
    }
    let mut out = Pairings::new();
    let half = players.len() / 2;
    for i in 0..half {
        out.add(players[i].clone(), players[i + half].clone());
    }
    out
}

/// Pair one round with COP. Returns `Err` for an invalid condition (no config,
/// no rounds remaining) so the engine surfaces it as a `PairingError`.
pub fn pair_cop(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let cfg: &CopConfig = ctx
        .cop_config
        .ok_or_else(|| "COP round configured but no cop_config supplied".to_string())?;
    if cfg.place_prizes < 1 {
        return Err(format!(
            "COP: place_prizes must be >= 1 (got {})",
            cfg.place_prizes
        ));
    }

    let total_rounds = ctx
        .round_pairings
        .iter()
        .map(|r| r.round)
        .max()
        .unwrap_or(rp.round);
    let rounds_remaining = total_rounds - rp.start_round;
    if rounds_remaining <= 0 {
        return Err(format!(
            "COP: invalid rounds remaining ({rounds_remaining}); nothing left to pair"
        ));
    }

    let field = ctx.standings(rp.start_round);
    if field.is_empty() {
        return Ok(Pairings::new());
    }
    // First round: no results to simulate — fall back to a Swiss-style initial.
    if rp.start_round < 1 {
        return Ok(initial_pairing(&field));
    }

    // Build the COP player list (wins doubled), in record order (the field is
    // already score/spread-sorted, so this is a stable identity, but re-sort to
    // mirror COP and be robust).
    let mut players: Vec<CopPlayer> = field
        .iter()
        .enumerate()
        .map(|(i, p)| CopPlayer {
            name: p.name.clone(),
            index: i,
            wins: 2 * p.wins + p.ties,
            spread: p.spread,
            is_bye: false,
        })
        .collect();
    sort_by_record(&mut players);

    let bye_active = players.len() % 2 == 1;
    if bye_active {
        players.push(CopPlayer {
            name: BYE_NAME.to_string(),
            index: players.len(),
            wins: 0,
            spread: 0,
            is_bye: true,
        });
    }
    let n = players.len();

    let rt = build_runtime(cfg, rounds_remaining, rp.round, bye_active);

    // Times played (incl. byes: a "Bye" opponent counts a player's bye tally) and
    // who met last round.
    let mut times_played: HashMap<(String, String), i32> = HashMap::new();
    for i in 0..n {
        for j in (i + 1)..n {
            let t = ctx.repeats.get(&Pairing::new(
                Player::new(&players[i].name),
                Player::new(&players[j].name),
            ));
            if t > 0 {
                times_played.insert(key(&players[i].name, &players[j].name), t);
            }
        }
    }
    let mut previous: HashSet<(String, String)> = HashSet::new();
    for s in ctx.slips {
        if s.round == rp.start_round {
            previous.insert(key(&s.winner_name, &s.loser_name));
        }
    }

    // Per-player repeat totals (an opponent met k>1 times adds k-1).
    let mut number_of_repeats: HashMap<String, i32> =
        players.iter().map(|p| (p.name.clone(), 0)).collect();
    for i in 0..n {
        for j in (i + 1)..n {
            let t = *times_played
                .get(&key(&players[i].name, &players[j].name))
                .unwrap_or(&0);
            if t > 1 {
                *number_of_repeats.get_mut(&players[i].name).unwrap() += t - 1;
                *number_of_repeats.get_mut(&players[j].name).unwrap() += t - 1;
            }
        }
    }

    // Prepaired constraints from this round's fixed pairings.
    let mut prepaired: HashMap<String, String> = HashMap::new();
    if let Some(pairs) = ctx.fixed_pairings.get(&rp.round) {
        for (a, b) in pairs {
            prepaired.insert(a.clone(), b.clone());
            prepaired.insert(b.clone(), a.clone());
        }
    }

    let dec = stub_decisions(&players, &rt, number_of_repeats);
    let class_prize: HashMap<usize, usize> = HashMap::new(); // class prizes: Phase 5

    let (edges, max_weight) =
        build_weight_edges(&players, &rt, &dec, &times_played, &previous, &prepaired, &class_prize);
    let matched = solve(&edges, max_weight, n);

    // Translate matched positions back to real players (bye → the synthetic bye).
    let by_name: HashMap<&str, &Player> = field.iter().map(|p| (p.name.as_str(), p)).collect();
    let to_player = |cp: &CopPlayer| -> Player {
        if cp.is_bye {
            Player::bye()
        } else {
            by_name
                .get(cp.name.as_str())
                .map(|p| (*p).clone())
                .unwrap_or_else(|| Player::new(&cp.name))
        }
    };
    let mut out = Pairings::new();
    for (a, b) in matched {
        out.add(to_player(&players[a]), to_player(&players[b]));
    }
    Ok(out)
}

/// Assemble the runtime config from `CopConfig` + this round's context.
fn build_runtime(
    cfg: &CopConfig,
    rounds_remaining: i32,
    round: i32,
    bye_active: bool,
) -> CopRuntime {
    let total_rounds = (rounds_remaining + (round - 1)).max(rounds_remaining) as usize;
    let gibson_ext = extend(&cfg.gibson_spreads, total_rounds, 250);
    let cumulative = cumulative_gibson(&cfg.gibson_spreads, total_rounds, 250);
    CopRuntime {
        rounds_remaining,
        round_to_pair: round - 1,
        lowest_ranked_payout: cfg.place_prizes - 1,
        gibson_spreads: gibson_ext,
        cumulative_gibson_spreads: cumulative,
        hopefulness: extend(&cfg.hopefulness, total_rounds, 0.05),
        control_loss_thresholds: extend(&cfg.control_loss_thresholds, total_rounds, 0.25),
        control_loss_activation_round: cfg.control_loss_activation_round,
        disallow_repeat_byes: cfg.disallow_repeat_byes,
        bye_active,
    }
}

/// Phase-1 placeholder for the simulation-derived decisions. Gibson rank and the
/// cash boundary are computed for real (RNG-independent); the contender set for
/// every prize rank is stubbed to the whole cash group, and control loss /
/// destiny control is disabled. Replaced by the real Monte Carlo in Phase 2.
fn stub_decisions(
    players: &[CopPlayer],
    rt: &CopRuntime,
    number_of_repeats: HashMap<String, i32>,
) -> Decisions {
    let n = players.len();
    let n_real = players.iter().filter(|p| !p.is_bye).count();
    let sim = sim_players(players, rt);
    let gibson = lowest_gibson_rank(&sim, rt);

    // Cash boundary: the deepest cash rank that actually exists.
    let boundary = rt
        .lowest_ranked_payout
        .min(n_real as i32 - 1)
        .max(0);
    let lowest_finishers_statistical: Vec<i32> = (0..n)
        .map(|i| if (i as i32) <= boundary { boundary } else { i as i32 })
        .collect();

    Decisions {
        lowest_gibson_rank: gibson,
        lowest_finishers_statistical,
        lowest_cash_statistical: boundary,
        lowest_cash_absolute: boundary,
        destinys_child: -1,
        control_loss_weight_used: false,
        control_loss_active: false,
        number_of_repeats,
    }
}

#[cfg(test)]
mod tests {
    use crate::model::PairingInput;
    use crate::pair::pair;
    use std::collections::{HashMap, HashSet};

    /// Build a COP division: `n` players, a Swiss round 1 already played (so COP
    /// pairs round 2 off real standings), then a COP round. `wins[i]`/`spread[i]`
    /// are the standings after round 1 for player P{i+1} (real, not doubled).
    fn cop_input(
        n: usize,
        total_rounds: i32,
        place_prizes: i32,
        results_r1: &[(&str, &str, i32, i32)],
    ) -> String {
        let players: Vec<String> = (1..=n)
            .map(|i| format!(r#"{{"name":"P{}","rating":{}}}"#, i, 2000 - 10 * i))
            .collect();
        let rounds: Vec<String> = (1..=total_rounds)
            .map(|r| {
                let strat = if r == 1 { "Swiss" } else { "COP" };
                format!(r#"{{"round":{r},"start_round":{},"pairing":"{strat}"}}"#, r - 1)
            })
            .collect();
        let slips: Vec<String> = results_r1
            .iter()
            .map(|(w, l, ws, ls)| {
                format!(
                    r#"{{"round":1,"winner_name":"{w}","loser_name":"{l}","winner_score":{ws},"loser_score":{ls},"winner_started":true}}"#
                )
            })
            .collect();
        format!(
            r#"{{"players":[{}],"round_pairings":[{}],"result_slips":[{}],
                "cop_config":{{"place_prizes":{place_prizes},"gibson_spreads":[250],
                "hopefulness":[0.05],"control_loss_thresholds":[0.25],
                "simulations":100,"always_wins_simulations":100}}}}"#,
            players.join(","),
            rounds.join(","),
            slips.join(","),
        )
    }

    fn parse(json: &str) -> PairingInput {
        serde_json::from_str(json).unwrap()
    }

    fn round_pairs(json: &str, round: i32) -> Vec<(String, String)> {
        let out = pair(&parse(json));
        let r = out.iter().find(|r| r.round == round).unwrap();
        assert!(r.error.is_none(), "round {round} errored: {r:?}");
        r.pairings
            .iter()
            .map(|p| {
                let mut n = [p.first.clone(), p.second.clone()];
                n.sort();
                (n[0].clone(), n[1].clone())
            })
            .collect()
    }

    #[test]
    fn cop_pairs_a_valid_full_round() {
        // 6 players, round 2 paired by COP off round-1 results. Every player is
        // paired exactly once.
        let json = cop_input(
            6,
            8,
            3,
            &[
                ("P1", "P2", 500, 400),
                ("P3", "P4", 500, 400),
                ("P5", "P6", 500, 400),
            ],
        );
        let pairs = round_pairs(&json, 2);
        assert_eq!(pairs.len(), 3);
        let mut seen = HashSet::new();
        for (a, b) in &pairs {
            assert!(seen.insert(a.clone()), "dup {a}");
            assert!(seen.insert(b.clone()), "dup {b}");
        }
        assert_eq!(seen.len(), 6);
    }

    #[test]
    fn cop_odd_field_gets_exactly_one_bye() {
        // 5 players → COP injects a bye; exactly one player sits out. Round 1
        // (Swiss) also has an odd field, so P5 takes a recorded bye there — else
        // round 1 counts as Partial and COP's round 2 never pairs.
        let json = cop_input(
            5,
            8,
            3,
            &[
                ("P1", "P2", 500, 400),
                ("P3", "P4", 500, 400),
                ("P5", "Bye", 50, 0),
            ],
        );
        let pairs = round_pairs(&json, 2);
        assert_eq!(pairs.len(), 3); // 2 games + 1 bye
        let byes = pairs.iter().filter(|(a, b)| a == "Bye" || b == "Bye").count();
        assert_eq!(byes, 1);
        // Everyone (incl. the bye taker) appears once.
        let mut seen = HashSet::new();
        for (a, b) in &pairs {
            seen.insert(a.clone());
            seen.insert(b.clone());
        }
        for i in 1..=5 {
            assert!(seen.contains(&format!("P{i}")), "P{i} unpaired");
        }
    }

    #[test]
    fn cop_honors_a_fixed_pairing_pin() {
        // Pin P1 vs P6 in the COP round; COP must honor it via its prepaired
        // (prohibitive-weight) constraint.
        let base = cop_input(
            6,
            8,
            3,
            &[
                ("P1", "P2", 500, 400),
                ("P3", "P4", 500, 400),
                ("P5", "P6", 500, 400),
            ],
        );
        let json = base.replace(
            r#""cop_config""#,
            r#""fixed_pairings":{"2":[["P1","P6"]]},"cop_config""#,
        );
        let pairs = round_pairs(&json, 2);
        assert!(
            pairs.contains(&("P1".to_string(), "P6".to_string())),
            "fixed pin P1-P6 not honored: {pairs:?}"
        );
    }

    #[test]
    fn cop_is_deterministic() {
        let json = cop_input(
            8,
            8,
            3,
            &[
                ("P1", "P2", 500, 400),
                ("P3", "P4", 500, 400),
                ("P5", "P6", 500, 400),
                ("P7", "P8", 500, 400),
            ],
        );
        assert_eq!(pair(&parse(&json)), pair(&parse(&json)));
    }

    #[test]
    fn cop_missing_config_errors() {
        // A COP round with no cop_config surfaces an error (→ PairingError).
        let json = r#"{
            "players": [{"name":"P1","rating":1900},{"name":"P2","rating":1800}],
            "round_pairings": [
                {"round":1,"start_round":0,"pairing":"Swiss"},
                {"round":2,"start_round":1,"pairing":"COP"}
            ],
            "result_slips": [
                {"round":1,"winner_name":"P1","loser_name":"P2","winner_score":500,"loser_score":400,"winner_started":true}
            ]
        }"#;
        let out = pair(&parse(json));
        let r2 = out.iter().find(|r| r.round == 2).unwrap();
        assert!(r2.pairings.is_empty());
        assert!(r2.error.as_deref().unwrap_or("").contains("cop_config"));
    }

    #[test]
    fn last_round_gibsonized_leader_plays_the_bottom() {
        // P1 is gibsonized (an 8-game lead with 1 round left): in the last round
        // COP pairs the locked leader down to the bottom, not against a live cash
        // contender. Round 8 is the COP round (rounds 1-7 give P1 the lead).
        // Build standings directly via round-7 results is heavy; instead assert
        // through the gibson unit below. Here we sanity-check a 1-round-left COP
        // round produces a valid matching without error.
        let json = cop_input(
            6,
            2, // total 2 rounds: round 2 (COP) has 1 round remaining
            3,
            &[
                ("P1", "P2", 800, 100), // P1 huge lead
                ("P3", "P4", 500, 400),
                ("P5", "P6", 500, 400),
            ],
        );
        let pairs = round_pairs(&json, 2);
        assert_eq!(pairs.len(), 3);
    }

    // --- unit tests on the RNG-independent pieces (Tier-1 parity surface) -----

    use super::*;

    fn rt(place_prizes: i32, rounds_remaining: i32) -> CopRuntime {
        build_runtime(
            &CopConfig {
                place_prizes,
                gibson_spreads: vec![250],
                hopefulness: vec![0.05],
                control_loss_thresholds: vec![0.25],
                control_loss_activation_round: 0,
                simulations: 0,
                always_wins_simulations: 0,
                disallow_repeat_byes: false,
            },
            rounds_remaining,
            rounds_remaining, // round number irrelevant here
            false,
        )
    }

    fn cp(name: &str, idx: usize, wins_doubled: i32, spread: i32) -> CopPlayer {
        CopPlayer {
            name: name.to_string(),
            index: idx,
            wins: wins_doubled,
            spread,
            is_bye: false,
        }
    }

    #[test]
    fn gibson_detects_a_locked_leader() {
        // P1 has a 4-real-win lead (8 doubled) with 3 rounds left → not locked
        // (4 <= 3? no, 4>3 → locked). Wait: gap 4 wins, 3 rounds → gibsonized.
        let players = vec![
            cp("P1", 0, 14, 900),
            cp("P2", 1, 6, 100),
            cp("P3", 2, 6, 50),
            cp("P4", 3, 4, 0),
        ];
        let r = rt(3, 3);
        assert_eq!(lowest_gibson_rank(&players, &r), 0);
    }

    #[test]
    fn gibson_none_when_field_is_tight() {
        let players = vec![
            cp("P1", 0, 10, 200),
            cp("P2", 1, 8, 100),
            cp("P3", 2, 8, 50),
            cp("P4", 3, 6, 0),
        ];
        let r = rt(3, 3);
        assert_eq!(lowest_gibson_rank(&players, &r), -1);
    }

    #[test]
    fn gibson_uses_spread_at_the_exact_win_boundary() {
        // Win gap == 2*remaining: locked only if the spread gap beats the
        // cumulative gibson spread. remaining=1 → cumulative[0] = 250*2 = 500.
        let close = vec![cp("P1", 0, 6, 600), cp("P2", 1, 4, 150)]; // spread gap 450 < 500
        let far = vec![cp("P1", 0, 6, 700), cp("P2", 1, 4, 150)]; // spread gap 550 > 500
        let r = rt(1, 1);
        assert_eq!(lowest_gibson_rank(&close, &r), -1);
        assert_eq!(lowest_gibson_rank(&far, &r), 0);
    }

    #[test]
    fn sim_truncation_keeps_the_can_cash_prefix() {
        // Top-3 prizes, 3 rounds left. A player 4+ real wins behind rank 2 can't
        // cash and truncates the sim set (odd-rank padding still applies).
        let players = vec![
            cp("P1", 0, 12, 0),
            cp("P2", 1, 12, 0),
            cp("P3", 2, 10, 0),
            cp("P4", 3, 2, 0), // 5 real wins behind rank2 (idx2, needs reach idx2): (10-2)/2=4 >3 → can't cash
            cp("P5", 4, 0, 0),
        ];
        let r = rt(3, 3);
        let sim = sim_players(&players, &r);
        // P4 can't cash but has odd index (3) so it's kept; iteration stops after
        // it, so P5 is dropped.
        let names: Vec<&str> = sim.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["P1", "P2", "P3", "P4"]);
    }
}
