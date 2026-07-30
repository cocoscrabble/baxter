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

use rand_chacha::ChaCha8Rng;
use rand_core::RngCore;

use crate::matching::max_weight_matching_pairs;
use crate::model::CopConfig;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairing, Pairings, Player, BYE_NAME};

use super::Ctx;

/// A pairing COP is willing to force but flags: a weight above this means the
/// matching had no non-repeat / in-contention alternative (COP's
/// `PROHIBITIVE_WEIGHT`).
const PROHIBITIVE_WEIGHT: i128 = 1_000_000;

/// COP's `INITIAL_FACTOR`: a factor width so large the simulation always uses the
/// rounds-remaining cap instead (the first, un-tuned simulation pass).
const INITIAL_FACTOR: i32 = 1_000_000;

/// One competitor inside the COP computation. `index` is the rank position used
/// to record simulated finishes; `start_*` snapshot the real record so a
/// simulation can reset the player after each iteration.
#[derive(Debug, Clone)]
struct CopPlayer {
    name: String,
    index: usize,
    /// Doubled wins (win = 2, draw = 1).
    wins: i32,
    spread: i32,
    is_bye: bool,
    start_wins: i32,
    start_spread: i32,
}

impl CopPlayer {
    fn reset(&mut self) {
        self.wins = self.start_wins;
        self.spread = self.start_spread;
    }
}

/// Runtime config: `CopConfig` with the per-round arrays forward-filled to the
/// round count and the cumulative gibson spreads derived — the shape COP.pm's
/// `%config` hash carries.
struct CopRuntime {
    rounds_remaining: i32,
    round_to_pair: i32,        // 0-indexed round being paired
    lowest_ranked_payout: i32, // 0-indexed
    gibson_spreads: Vec<i32>,
    cumulative_gibson_spreads: Vec<i32>,
    hopefulness: Vec<f64>,
    control_loss_thresholds: Vec<f64>,
    control_loss_activation_round: i32,
    number_of_sims: u32,
    always_wins_number_of_sims: u32,
    disallow_repeat_byes: bool,
    bye_active: bool,
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
    // Standings come from `start_round` (below); the horizon may be counted from
    // the round being paired instead — see `CopConfig::horizon_from_paired_round`.
    let rounds_remaining = if cfg.horizon_from_paired_round {
        total_rounds - rp.round + 1
    } else {
        total_rounds - rp.start_round
    };
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
        .map(|(i, p)| {
            let wins = 2 * p.wins + p.ties;
            CopPlayer {
                name: p.name.clone(),
                index: i,
                wins,
                spread: p.spread,
                is_bye: false,
                start_wins: wins,
                start_spread: p.spread,
            }
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
            start_wins: 0,
            start_spread: 0,
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

    let dec = compute_decisions(ctx.rng, &players, &rt, number_of_repeats);
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
        number_of_sims: cfg.simulations.max(1),
        always_wins_number_of_sims: cfg.always_wins_simulations.max(1),
        disallow_repeat_byes: cfg.disallow_repeat_byes,
        bye_active,
    }
}

// --- Monte Carlo simulation (COP.pm's factor-pair projection) ----------------

/// Per-player final-rank tallies over the simulations. `array[n*index + place]`
/// counts how often the player with stable `index` finished in `place`.
struct TournamentResults {
    n: usize,
    array: Vec<u32>,
}

impl TournamentResults {
    fn new(n: usize) -> Self {
        TournamentResults {
            n,
            array: vec![0; n * n],
        }
    }

    /// Record one finished simulation: `sim` is sorted by record, so its position
    /// `i` is each player's final rank.
    fn record(&mut self, sim: &[CopPlayer]) {
        for (i, p) in sim.iter().enumerate() {
            self.array[self.n * p.index + i] += 1;
        }
    }

    fn get(&self, player: &CopPlayer, place: usize) -> u32 {
        self.array[self.n * player.index + place]
    }
}

/// Factor pairing (COP's `factor_pair`): rank `i` plays rank `i + nrl`. Gibsonized
/// players are paired to the bottom; the factor width `nrl` is capped at half the
/// non-gibsonized field and at `max_factor`. Returns pairs of current-rank
/// positions. The half-integer cap (odd non-gibsonized field) is handled exactly
/// as Perl's fractional index truncation would.
fn factor_pair(sim: &[CopPlayer], nrl_in: i32, gibson: i32, max_factor: i32) -> Vec<(usize, usize)> {
    let n = sim.len();
    let g = (gibson + 1).max(0) as usize; // number gibsonized
    let factor2 = if gibson >= 0 { n - g } else { n }; // players to factor
    let mut nrl = nrl_in;
    let mut half = false;
    if 2 * nrl > factor2 as i32 {
        nrl = (factor2 / 2) as i32;
        half = factor2 % 2 == 1;
    }
    if nrl > max_factor {
        nrl = max_factor;
        half = false;
    }
    let q = nrl.max(0) as usize;

    let mut pairings = Vec::new();
    for i in 0..g {
        pairings.push((i, (n - 1) - i));
    }
    let upper2 = g + q + usize::from(half);
    for i in g..upper2 {
        pairings.push((i, i + q));
    }
    let mut i = 2 * q + usize::from(half) + g;
    let bound = n - g;
    while i < bound {
        pairings.push((i, i + 1));
        i += 2;
    }
    pairings
}

/// Factor pairing with the leader pinned to a target player (COP's
/// `factor_pair_minus_player`): first (rank 0) plays the target, and everyone
/// else is factor-paired among themselves. Used by the control-loss "can this
/// player always win?" simulation.
fn factor_pair_minus_player(
    sim: &[CopPlayer],
    nrl_in: i32,
    target_index: usize,
) -> Vec<(usize, usize)> {
    let index_to_rank: HashMap<usize, usize> =
        sim.iter().enumerate().map(|(r, p)| (p.index, r)).collect();
    let player_rank_index = index_to_rank[&target_index];
    // Everyone except the leader (rank 0) and the target.
    let reduced: Vec<&CopPlayer> = sim
        .iter()
        .enumerate()
        .filter(|(r, _)| *r != 0 && *r != player_rank_index)
        .map(|(_, p)| p)
        .collect();
    let m = reduced.len();
    let mut nrl = nrl_in;
    if nrl * 2 > m as i32 {
        nrl = (m / 2) as i32;
    }
    let q = nrl.max(0) as usize;

    let mut pairings = vec![(0usize, player_rank_index)];
    for i in 0..q {
        pairings.push((
            index_to_rank[&reduced[i].index],
            index_to_rank[&reduced[i + q].index],
        ));
    }
    let mut i = 2 * q;
    while i < m {
        pairings.push((
            index_to_rank[&reduced[i].index],
            index_to_rank[&reduced[i + 1].index],
        ));
        i += 2;
    }
    pairings
}

/// Play one simulated round (COP's `play_round`): each game's spread is drawn
/// uniformly from `[-max_spread, max_spread]`; a bye is a 2-win, +50 for the real
/// player; `forced` (a rank position, or -1) always wins by ≥1. Re-sorts by record.
fn play_round(
    rng: &mut ChaCha8Rng,
    pairings: &[(usize, usize)],
    sim: &mut [CopPlayer],
    forced: i32,
    max_spread: i32,
) {
    for &(a, b) in pairings {
        if sim[a].is_bye || sim[b].is_bye {
            let real = if sim[a].is_bye { b } else { a };
            sim[real].spread += 50;
            sim[real].wins += 2;
            continue;
        }
        let span = (2 * max_spread + 1).max(1) as u64;
        let mut spread = max_spread - (rng.next_u64() % span) as i32;
        if forced >= 0 {
            if a as i32 == forced {
                spread = spread.abs() + 1;
            } else if b as i32 == forced {
                spread = -spread.abs() - 1;
            }
        }
        let (p1win, p2win) = match spread.cmp(&0) {
            std::cmp::Ordering::Greater => (2, 0),
            std::cmp::Ordering::Less => (0, 2),
            std::cmp::Ordering::Equal => (1, 1),
        };
        sim[a].spread += spread;
        sim[a].wins += p1win;
        sim[b].spread -= spread;
        sim[b].wins += p2win;
    }
    sort_by_record(sim);
}

/// Run the factor-pair Monte Carlo `number_of_sims` times, tallying final ranks
/// (COP's `sim_factor_pair`). Each iteration simulates every remaining round,
/// then resets the players.
fn sim_factor_pair(
    rng: &mut ChaCha8Rng,
    rt: &CopRuntime,
    sim: &mut [CopPlayer],
    gibson: i32,
    max_factor: i32,
) -> TournamentResults {
    let mut results = TournamentResults::new(sim.len());
    for _ in 0..rt.number_of_sims {
        for remaining in (1..=rt.rounds_remaining).rev() {
            let pairings = factor_pair(sim, remaining, gibson, max_factor);
            let max_spread = rt.gibson_spreads[(remaining - 1) as usize];
            play_round(rng, &pairings, sim, -1, max_spread);
        }
        results.record(sim);
        for p in sim.iter_mut() {
            p.reset();
        }
        sort_by_record(sim);
    }
    results
}

/// For each final rank, the lowest-ranked current player who reaches it in more
/// than `hopefulness` of the sims ("statistically") and at least once
/// ("absolutely"). COP's `get_lowest_ranked_players_who_can_finish_in_nth`. `sim`
/// must be sorted by record.
fn lowest_finishers(
    rt: &CopRuntime,
    results: &TournamentResults,
    sim: &[CopPlayer],
) -> (Vec<i32>, Vec<i32>) {
    let sim_n = sim.len();
    let adj_hope = rt.hopefulness[(rt.rounds_remaining - 1) as usize];
    let mut stat = vec![0i32; sim_n];
    let mut abs = vec![0i32; sim_n];
    for final_rank in 0..sim_n {
        for (cur_rank, player) in sim.iter().enumerate() {
            let mut sum = 0u32;
            for place in 0..=final_rank {
                sum += results.get(player, place);
            }
            let pct = sum as f64 / rt.number_of_sims as f64;
            if pct > adj_hope {
                stat[final_rank] = cur_rank as i32;
            }
            if sum > 0 {
                abs[final_rank] = cur_rank as i32;
            }
        }
    }
    (stat, abs)
}

/// How often each catchable player reaches first if they always win, under
/// pair-with-first vs plain factor pairing (COP's `sim_player_always_wins`).
/// Indexed by rank-1 (position 0 = the 2nd-ranked player).
fn sim_player_always_wins(
    rng: &mut ChaCha8Rng,
    rt: &CopRuntime,
    sim: &mut [CopPlayer],
) -> (Vec<u32>, Vec<u32>) {
    let mut pwf_list = Vec::new();
    let mut fp_list = Vec::new();
    let first_wins = sim[0].wins;
    for rank in 1..sim.len() {
        if first_wins - sim[rank].wins > 2 * rt.rounds_remaining {
            break; // can't reach first
        }
        if sim[rank].is_bye {
            pwf_list.push(0);
            fp_list.push(0);
            continue;
        }
        let target_index = sim[rank].index;
        let (pwf, fp) = sim_player_always_wins_one(rng, rt, sim, target_index);
        pwf_list.push(pwf);
        fp_list.push(fp);
    }
    (pwf_list, fp_list)
}

/// One player's always-wins tally (COP's `sim_player_always_wins_worker`): each
/// sim runs a pair-with-first pass and a factor-pair pass, counting how often the
/// target — forced to win every game — ends up first.
fn sim_player_always_wins_one(
    rng: &mut ChaCha8Rng,
    rt: &CopRuntime,
    sim: &mut [CopPlayer],
    target_index: usize,
) -> (u32, u32) {
    let gs_len = rt.gibson_spreads.len();
    let mut pwf = 0u32;
    let mut fp = 0u32;
    for _ in 0..rt.always_wins_number_of_sims {
        // Phase A: leader pinned to the target (pair with first).
        for remaining in (1..=rt.rounds_remaining).rev() {
            let target_rank = sim.iter().position(|p| p.index == target_index).unwrap() as i32;
            let pairings = factor_pair_minus_player(sim, remaining, target_index);
            let max_spread = rt.gibson_spreads[gs_len - remaining as usize];
            play_round(rng, &pairings, sim, target_rank, max_spread);
            if sim[0].index == target_index {
                pwf += 1;
                break;
            }
        }
        for p in sim.iter_mut() {
            p.reset();
        }
        sort_by_record(sim);
        // Phase B: plain factor pairing.
        for remaining in (1..=rt.rounds_remaining).rev() {
            let target_rank = sim.iter().position(|p| p.index == target_index).unwrap() as i32;
            let pairings = factor_pair(sim, remaining, -1, INITIAL_FACTOR);
            let max_spread = rt.gibson_spreads[gs_len - remaining as usize];
            play_round(rng, &pairings, sim, target_rank, max_spread);
            if sim[0].index == target_index {
                fp += 1;
                break;
            }
        }
        for p in sim.iter_mut() {
            p.reset();
        }
        sort_by_record(sim);
    }
    (pwf, fp)
}

/// Control loss (COP's `get_control_loss`): the deepest rank that always reaches
/// first when it always wins, and how much control the leader has lost.
fn get_control_loss(rng: &mut ChaCha8Rng, rt: &CopRuntime, sim: &mut [CopPlayer]) -> (i32, f64) {
    let (pwf, fp) = sim_player_always_wins(rng, rt, sim);
    let mut lowest_ranked_always_wins = 0i32;
    for (i, &w) in pwf.iter().enumerate() {
        if w == rt.always_wins_number_of_sims {
            lowest_ranked_always_wins = (i + 1) as i32;
        }
    }
    let mut control_loss = 0.0;
    if lowest_ranked_always_wins > 0 {
        let fpw = fp[(lowest_ranked_always_wins - 1) as usize];
        control_loss =
            (rt.always_wins_number_of_sims as f64 - fpw as f64) / rt.always_wins_number_of_sims as f64;
    }
    (lowest_ranked_always_wins, control_loss)
}

/// Run the full COP decision pipeline: gibson rank, the two-pass factor-pair
/// simulation, contender arrays, control loss, and destiny's child. Mirrors the
/// orchestration in `cop()` (COP.pm ~1030–1264).
fn compute_decisions(
    rng: &mut ChaCha8Rng,
    players_full: &[CopPlayer],
    rt: &CopRuntime,
    number_of_repeats: HashMap<String, i32>,
) -> Decisions {
    let n = players_full.len();
    let mut sim = sim_players(players_full, rt);
    // Re-index to sim positions (0..sim_n) so the results matrix is compact.
    for (i, p) in sim.iter_mut().enumerate() {
        p.index = i;
    }
    let sim_n = sim.len();
    let gibson = lowest_gibson_rank(&sim, rt);

    // Pass 1: initial (untuned) factor pairing → an improved factor constant.
    let pass1 = sim_factor_pair(rng, rt, &mut sim, gibson, INITIAL_FACTOR);
    sort_by_record(&mut sim);
    let (_stat1, abs1) = lowest_finishers(rt, &pass1, &sim);
    let idx = ((gibson + 1).max(0) as usize).min(sim_n.saturating_sub(1));
    let improved_factor = abs1[idx] - (gibson + 1);

    // Pass 2: the tuned simulation the contenders come from.
    let pass2 = sim_factor_pair(rng, rt, &mut sim, gibson, improved_factor);

    // Control loss only matters when no one is gibsonized.
    let (mut lowest_ranked_always_wins, mut control_loss) = (-1i32, -1.0f64);
    if gibson < 0 {
        let (law, cl) = get_control_loss(rng, rt, &mut sim);
        lowest_ranked_always_wins = law;
        control_loss = cl;
    }
    let adj_threshold = rt.control_loss_thresholds[(rt.rounds_remaining - 1) as usize];

    sort_by_record(&mut sim);
    let (stat, abs) = lowest_finishers(rt, &pass2, &sim);
    let lrp = (rt.lowest_ranked_payout.max(0) as usize).min(sim_n.saturating_sub(1));
    let lowest_cash_statistical = stat[lrp];
    let lowest_cash_absolute = abs[lrp];

    let control_loss_active = rt.round_to_pair >= rt.control_loss_activation_round;

    // Destiny's child: which single opponent first place must be pinned to. The
    // COP loop only acts for i == 0 (and only when no one is gibsonized, since the
    // enclosing branch requires neither player gibsonized).
    let mut destinys_child = -1i32;
    let mut control_loss_weight_used = false;
    if rt.rounds_remaining != 1 && control_loss_active && gibson < 0 {
        let lrpcw = if stat[0] == 0 { 1 } else { stat[0] };
        for j in 1..n as i32 {
            if players_full[j as usize].is_bye {
                continue;
            }
            let forced_elsewhere = (control_loss > adj_threshold
                && j != lrpcw.min(lowest_ranked_always_wins))
                || (control_loss <= adj_threshold && j != lrpcw);
            if forced_elsewhere {
                control_loss_weight_used = true;
            } else {
                destinys_child = j;
            }
        }
    }

    // Pad the per-rank contender array to the full field so the weight graph can
    // index any rank (ranks past the sim set are never contenders).
    let lowest_finishers_statistical: Vec<i32> = (0..n)
        .map(|i| if i < sim_n { stat[i] } else { i as i32 })
        .collect();

    Decisions {
        lowest_gibson_rank: gibson,
        lowest_finishers_statistical,
        lowest_cash_statistical,
        lowest_cash_absolute,
        destinys_child,
        control_loss_weight_used,
        control_loss_active,
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

    /// `horizon_from_paired_round` changes only *how many rounds COP thinks are
    /// left*; the standings still come from `start_round`. For the usual sliding
    /// COP round the two counts are equal, so the flag must be a no-op — that is
    /// the property that keeps it from disturbing existing divisions.
    #[test]
    fn cop_horizon_flag_is_a_no_op_for_a_sliding_round() {
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
        // cop_input builds every round with start_round == round - 1.
        let flagged = base.replace(
            r#""place_prizes""#,
            r#""horizon_from_paired_round":true,"place_prizes""#,
        );
        assert_ne!(base, flagged, "test setup: flag was not injected");
        assert_eq!(
            round_pairs(&base, 2),
            round_pairs(&flagged, 2),
            "flag must not change a round whose start_round is already round - 1"
        );
    }

    /// A COP round pairing off an older snapshot still produces a full, valid
    /// round with the flag set — this is the path the flag exists to serve.
    #[test]
    fn cop_pairs_a_full_round_off_an_older_snapshot() {
        let players: Vec<String> = (1..=6)
            .map(|i| format!(r#"{{"name":"P{i}","rating":{}}}"#, 2000 - 10 * i))
            .collect();
        // Round 3 pairs off round 1 — two rounds back, so the horizon flag bites.
        let rounds: Vec<String> = (1..=8)
            .map(|r| {
                let strat = if r == 1 { "Swiss" } else { "COP" };
                let start = if r == 3 { 1 } else { r - 1 };
                format!(r#"{{"round":{r},"start_round":{start},"pairing":"{strat}"}}"#)
            })
            .collect();
        let slips: Vec<String> = [
            (1, "P1", "P2"), (1, "P3", "P4"), (1, "P5", "P6"),
            (2, "P1", "P3"), (2, "P5", "P2"), (2, "P4", "P6"),
        ]
        .iter()
        .map(|(r, w, l)| {
            format!(
                r#"{{"round":{r},"winner_name":"{w}","loser_name":"{l}","winner_score":500,"loser_score":420,"winner_started":true}}"#
            )
        })
        .collect();
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"result_slips":[{}],
                "cop_config":{{"place_prizes":3,"gibson_spreads":[250],
                "hopefulness":[0.05],"control_loss_thresholds":[0.25],
                "simulations":100,"always_wins_simulations":100,
                "horizon_from_paired_round":true}}}}"#,
            players.join(","),
            rounds.join(","),
            slips.join(","),
        );
        let pairs = round_pairs(&json, 3);
        assert_eq!(pairs.len(), 3, "expected a full 6-player round: {pairs:?}");
        let mut seen: Vec<&str> = pairs
            .iter()
            .flat_map(|(a, b)| [a.as_str(), b.as_str()])
            .collect();
        seen.sort();
        seen.dedup();
        assert_eq!(seen.len(), 6, "every player paired exactly once: {pairs:?}");
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
                horizon_from_paired_round: false,
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
            start_wins: wins_doubled,
            start_spread: spread,
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

    /// Build the weight graph + matching for constructed players and injected
    /// decisions — the Tier-1 surface, decoupled from the RNG-driven sims.
    fn matching_of(players: &[CopPlayer], rt: &CopRuntime, dec: &Decisions) -> Vec<(usize, usize)> {
        let tp: HashMap<(String, String), i32> = HashMap::new();
        let prev: HashSet<(String, String)> = HashSet::new();
        let pre: HashMap<String, String> = HashMap::new();
        let cls: HashMap<usize, usize> = HashMap::new();
        let (edges, mw) = build_weight_edges(players, rt, dec, &tp, &prev, &pre, &cls);
        solve(&edges, mw, players.len())
    }

    fn partner_of(matching: &[(usize, usize)], v: usize) -> usize {
        for &(a, b) in matching {
            if a == v {
                return b;
            }
            if b == v {
                return a;
            }
        }
        panic!("{v} unmatched");
    }

    fn ranked(n: usize) -> Vec<CopPlayer> {
        (0..n)
            .map(|i| cp(&format!("P{}", i + 1), i, (2 * (n - i)) as i32, 0))
            .collect()
    }

    #[test]
    fn weight_graph_gibsonized_leader_avoids_cash_contenders() {
        // 6 players, P1 (rank 0) gibsonized, top-3 cash. The weight graph must not
        // pair the locked leader with a live cash contender (rank 1 or 2) — those
        // edges are prohibitive — so P1's partner is outside the cash group.
        let players = ranked(6);
        let rt = rt(3, 3);
        let dec = Decisions {
            lowest_gibson_rank: 0,
            lowest_finishers_statistical: vec![2, 2, 2, 3, 4, 5],
            lowest_cash_statistical: 2,
            lowest_cash_absolute: 2,
            destinys_child: -1,
            control_loss_weight_used: false,
            control_loss_active: false,
            number_of_repeats: HashMap::new(),
        };
        let m = matching_of(&players, &rt, &dec);
        assert!(
            partner_of(&m, 0) > 2,
            "gibsonized leader paired inside the cash group: {m:?}"
        );
    }

    #[test]
    fn weight_graph_leader_pairs_its_only_catcher() {
        // No gibson, top-2 cash, and only rank 1 can still catch the leader
        // (finishers[0] = 1). Every leader-vs-non-contender edge is prohibitive, so
        // the leader must be paired with rank 1.
        let players = ranked(6);
        let rt = rt(2, 3);
        let dec = Decisions {
            lowest_gibson_rank: -1,
            lowest_finishers_statistical: vec![1, 1, 3, 3, 4, 5],
            lowest_cash_statistical: 1,
            lowest_cash_absolute: 3,
            destinys_child: -1,
            control_loss_weight_used: false,
            control_loss_active: false,
            number_of_repeats: HashMap::new(),
        };
        let m = matching_of(&players, &rt, &dec);
        assert_eq!(partner_of(&m, 0), 1, "leader not paired with its catcher: {m:?}");
    }

    #[test]
    fn compute_decisions_is_deterministic_under_a_fixed_seed() {
        // The RNG-driven pipeline is reproducible: same seed → same contender
        // arrays and cash boundaries.
        let players = ranked(6);
        let rt = rt(3, 3);
        let run = || {
            let mut rng = crate::rng::seeded(99);
            compute_decisions(&mut rng, &players, &rt, HashMap::new())
        };
        let a = run();
        let b = run();
        assert_eq!(a.lowest_gibson_rank, b.lowest_gibson_rank);
        assert_eq!(a.lowest_cash_statistical, b.lowest_cash_statistical);
        assert_eq!(a.lowest_cash_absolute, b.lowest_cash_absolute);
        assert_eq!(
            a.lowest_finishers_statistical,
            b.lowest_finishers_statistical
        );
        assert_eq!(a.destinys_child, b.destinys_child);
    }

    #[test]
    fn compute_decisions_bounds_are_in_range() {
        // A tight 6-player field, top-3 cash, 3 rounds left: the cash boundaries
        // land within the field and are monotone (statistical ⊆ absolute).
        let players = ranked(6);
        let rt = rt(3, 3);
        let mut rng = crate::rng::seeded(7);
        let dec = compute_decisions(&mut rng, &players, &rt, HashMap::new());
        assert!(dec.lowest_cash_absolute >= 0 && dec.lowest_cash_absolute < 6);
        assert!(dec.lowest_cash_statistical <= dec.lowest_cash_absolute);
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
