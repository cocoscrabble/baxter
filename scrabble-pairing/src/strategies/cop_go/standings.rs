//! Upstream's `pkg/pair/standings`: packed records and the Monte Carlo
//! simulations COP's contention analysis rests on. Ported line for line; the
//! notes here are only where the port departs from upstream or where
//! upstream's behaviour is easy to misread.
//!
//! **A record is one `u64`**: wins-minus-losses (offset) in the top 16 bits,
//! spread (offset) in the next 32, the player index in the low 16. Sorting the
//! numbers descending ranks the field, and an exact wins-and-spread tie goes to
//! the *higher* player index. A win adds 1 to the wins field and a loss takes 1
//! away, so a draw — or a zero-score bye — changes nothing, and
//! `wins_times_two` recovers `2W + T` by adding the rounds played.
//!
//! **Departures from upstream:**
//! - No clocks. Upstream stops a sim phase after 6s; here the re-sim loop is
//!   bounded by `Request::max_sims` instead, and the other phases by their
//!   (fixed) counts. Where upstream's run is not clock-bound, the two agree
//!   exactly (`plans/PLAN_COP_GO_PORT.md`, decision 3).
//! - A fixed worker count (`SIM_WORKERS`) instead of `runtime.NumCPU()`, so the
//!   answer does not depend on the machine.

use std::collections::{BTreeMap, HashMap};

use super::cephes::beta_quantile;
use super::rand::Rand;
use super::request::Request;
use super::score_diffs::SCORE_DIFFERENCES;

pub const BYE_PLAYER_INDEX: usize = 0xFFFF;

/// Simulation goroutines upstream would run with `NumSimWorkersOverride = 4`
/// (the oracle's default). Part of the algorithm: each worker has its own RNG
/// stream, so this changes the draws.
pub const SIM_WORKERS: usize = 4;

const PLAYER_WINS_OFFSET: u32 = 48;
const INITIAL_WINS_VALUE: i64 = 1 << (64 - PLAYER_WINS_OFFSET - 1);
const PLAYER_SPREAD_OFFSET: u32 = 16;
const INITIAL_SPREAD_VALUE: i64 = 1 << (PLAYER_WINS_OFFSET - PLAYER_SPREAD_OFFSET - 1);
const PLAYER_INDEX_MASK: u64 = 0xFFFF;
const SIM_CONFIDENCE: f64 = 0.99;
const RESIM_BATCH_SIZE: usize = 10000;
const CONTROL_LOSS_WIN_ALL_VS_FIRST_ROUNDS_THRESH: i32 = 4;

fn record(wins: i64, spread: i64) -> u64 {
    assert!(wins >= 0 && spread >= 0, "wins and spread must be non-negative");
    ((wins as u64) << PLAYER_WINS_OFFSET) | ((spread as u64) << PLAYER_SPREAD_OFFSET)
}

fn index_of(record: u64) -> usize {
    (record & PLAYER_INDEX_MASK) as usize
}

fn wins_value(record: u64) -> i64 {
    ((record >> PLAYER_WINS_OFFSET) & 0xFFFF) as i64
}

fn spread_value(record: u64) -> u64 {
    (record >> PLAYER_SPREAD_OFFSET) & 0xFFFF_FFFF
}

#[derive(Debug, Clone)]
pub struct Standings {
    records: Vec<u64>,
    records_backup: Vec<u64>,
    possible_results: std::sync::Arc<Vec<u64>>,
    tie_results: usize,
    rounds_played: i64,
}

/// What `SimFactorPairAll` reports.
#[derive(Debug, Clone, Default)]
pub struct SimResults {
    /// `final_ranks[starting rank][final rank]` = number of sims.
    pub final_ranks: Vec<Vec<u64>>,
    pub pairings: Vec<Vec<usize>>,
    pub gibson_groups: Vec<i32>,
    pub gibsonized_players: Vec<bool>,
    pub highest_control_loss_rank_idx: i32,
    pub control_loss_via_lock_fallback: bool,
    pub control_loss_lock_run_end_rank_idx: i32,
    /// `None` when the control-loss search did not run (upstream's nil map).
    pub all_control_losses: Option<BTreeMap<usize, usize>>,
    pub vs_first_wins: Option<BTreeMap<usize, usize>>,
    pub segment_round_factors: Vec<usize>,
    pub total_sims: usize,
}

impl Standings {
    /// `CreateInitialStandings`.
    pub fn from_request(req: &Request) -> Self {
        let all = req.all_players as usize;
        let mut records: Vec<u64> = (0..all)
            .map(|i| record(INITIAL_WINS_VALUE, INITIAL_SPREAD_VALUE) + i as u64)
            .collect();

        let mut possible_results = vec![0u64; SCORE_DIFFERENCES.len()];
        let mut tie_results = 0;
        for (i, &diff) in SCORE_DIFFERENCES.iter().enumerate() {
            if diff == 0 {
                tie_results += 1;
                continue;
            }
            let spread = diff.min(req.gibson_spread as u64);
            possible_results[i] = record(1, spread as i64);
        }

        for (round_idx, round_results) in req.division_results.iter().enumerate() {
            for (player_idx, &score) in round_results.iter().enumerate() {
                let opp_idx = req.division_pairings[round_idx][player_idx];
                let p_score = score as i64;
                if player_idx as i32 == opp_idx {
                    // Bye
                    if p_score > 0 {
                        records[player_idx] = records[player_idx].wrapping_add(record(1, p_score));
                    } else if p_score < 0 {
                        records[player_idx] = records[player_idx].wrapping_sub(record(1, -p_score));
                    }
                } else if (player_idx as i32) < opp_idx {
                    let opp = opp_idx as usize;
                    let spread = p_score - round_results[opp] as i64;
                    if spread > 0 {
                        let r = record(1, spread);
                        records[player_idx] = records[player_idx].wrapping_add(r);
                        records[opp] = records[opp].wrapping_sub(r);
                    } else if spread < 0 {
                        let r = record(1, -spread);
                        records[opp] = records[opp].wrapping_add(r);
                        records[player_idx] = records[player_idx].wrapping_sub(r);
                    }
                }
            }
        }

        let removed: std::collections::HashSet<i32> = req.removed_players.iter().copied().collect();
        let valid: Vec<u64> = records
            .into_iter()
            .enumerate()
            .filter(|(i, _)| !removed.contains(&(*i as i32)))
            .map(|(_, r)| r)
            .collect();

        let mut s = Standings {
            records_backup: vec![0; valid.len()],
            records: valid,
            possible_results: std::sync::Arc::new(possible_results),
            tie_results,
            rounds_played: req.division_results.len() as i64,
        };
        s.sort();
        s
    }

    fn backup(&mut self) {
        self.records_backup.copy_from_slice(&self.records);
    }

    fn restore_from_backup(&mut self) {
        self.records.copy_from_slice(&self.records_backup);
    }

    pub fn num_players(&self) -> usize {
        self.records.len()
    }

    pub fn player_index(&self, rank_idx: usize) -> usize {
        index_of(self.records[rank_idx])
    }

    /// `GetPlayerWinsIntTimesTwo`: `2W + T`.
    pub fn wins_times_two(&self, rank_idx: usize) -> i64 {
        wins_value(self.records[rank_idx]) - INITIAL_WINS_VALUE + self.rounds_played
    }

    pub fn wins(&self, rank_idx: usize) -> f64 {
        self.wins_times_two(rank_idx) as f64 / 2.0
    }

    pub fn spread(&self, rank_idx: usize) -> i64 {
        spread_value(self.records[rank_idx]) as i64 - INITIAL_SPREAD_VALUE
    }

    /// `CanCatch`: can rank `j` still catch rank `i` (`i < j`, sorted)?
    pub fn can_catch(&self, rounds_remaining: i32, cume_gibson_spread: i64, i: usize, j: usize) -> bool {
        let ri = self.records[i];
        let rj = self.records[j];
        let win_diff = wins_value(ri) - wins_value(rj);
        let highest_possible = rounds_remaining as i64 * 2;
        if win_diff != highest_possible {
            return win_diff < highest_possible;
        }
        let pi = spread_value(ri);
        let pj = spread_value(rj);
        if pj >= pi {
            return true;
        }
        (pi - pj) as i64 <= cume_gibson_spread
    }

    /// `GetGibsonizedPlayers`.
    pub fn gibsonized_players(&self, req: &Request) -> Vec<bool> {
        let n = self.records.len();
        let mut gib = vec![false; n];
        let rounds_remaining = req.rounds_remaining();
        let cume = cume_gibson_spread(req);
        let places = (req.place_prizes as usize).min(n);
        for (rank, g) in gib.iter_mut().enumerate().take(places) {
            *g = !(rank > 0 && self.can_catch(rounds_remaining, cume, rank - 1, rank)
                || rank < n - 1 && self.can_catch(rounds_remaining, cume, rank, rank + 1));
        }
        gib
    }

    fn sort(&mut self) {
        self.records.sort_unstable_by(|a, b| b.cmp(a));
    }

    /// Append upstream's "evener": a dummy bye player below everyone, so an odd
    /// field can be simulated in pairs. Returns whether one was added.
    fn add_evener(&mut self, rounds_remaining: i32) -> bool {
        let n = self.records.len();
        if n.is_multiple_of(2) {
            return false;
        }
        let lowest_wins = wins_value(self.records[n - 1]);
        self.records.push(
            record(lowest_wins - (rounds_remaining as i64 + 1) * 2, INITIAL_SPREAD_VALUE)
                + BYE_PLAYER_INDEX as u64,
        );
        self.records_backup = vec![0; n + 1];
        true
    }

    fn remove_evener(&mut self) {
        let n = self.records.len() - 1;
        self.records.truncate(n);
        self.records_backup = vec![0; n];
    }

    /// `SimFactorPairAll`. `None` when `prev_segment_round_factors` shows the
    /// same sim already ran.
    #[allow(clippy::too_many_arguments)]
    pub fn sim_factor_pair_all(
        &mut self,
        req: &Request,
        rng: &mut Rand,
        sims: usize,
        max_factor: i32,
        lowest_hope_control_losser: i32,
        prev_segment_round_factors: Option<&[usize]>,
        skip_rank_idx: i32,
    ) -> Option<SimResults> {
        let rounds_remaining = req.rounds_remaining();
        let evener = self.add_evener(rounds_remaining);
        let mut out = self.evened_sim_factor_pair_all(
            req,
            rng,
            sims,
            max_factor,
            lowest_hope_control_losser,
            prev_segment_round_factors,
            skip_rank_idx,
        );
        if evener {
            let n = self.records.len();
            self.remove_evener();
            if let Some(r) = out.as_mut() {
                for row in r.final_ranks.iter_mut() {
                    row.truncate(n - 1);
                }
                r.final_ranks.truncate(n - 1);
            }
        }
        out
    }

    #[allow(clippy::too_many_arguments)]
    fn evened_sim_factor_pair_all(
        &mut self,
        req: &Request,
        rng: &mut Rand,
        sims: usize,
        max_factor: i32,
        lowest_hope_control_losser: i32,
        prev_segment_round_factors: Option<&[usize]>,
        skip_rank_idx: i32,
    ) -> Option<SimResults> {
        let n = self.records.len();
        let mut results = vec![vec![0u64; n]; n];
        let rounds_remaining = req.rounds_remaining();
        let rr = rounds_remaining.max(0) as usize;
        let mut pairings = vec![vec![0usize; n]; rr];
        let gibsonized = self.gibsonized_players(req);
        let mut gibson_groups = vec![0i32; n];
        let mut next_gibson_group = 1;
        let mut start = 0usize;
        let mut end = 0usize;
        let mut leftover_gibson: Vec<usize> = Vec::new();
        let control_loss = lowest_hope_control_losser >= 0;
        let mut pairings_start = 0usize;
        let mut segment_round_factors: Vec<usize> = Vec::new();
        let ctx = SegmentCtx { rounds_remaining: rr, max_factor, total_players: n, control_loss };

        while end <= n {
            if end == n {
                assign_pairings_for_segment(
                    &ctx, pairings_start, start, end as isize - 1, &leftover_gibson,
                    &mut pairings, &mut segment_round_factors, rng,
                );
                for g in gibson_groups.iter_mut().take(end).skip(start) {
                    *g = 0;
                }
                break;
            }
            if gibsonized[end] {
                let mut gibson_is_leftover = true;
                if end != start {
                    let in_group = end - start;
                    if in_group % 2 == 1 {
                        // Odd group: pull the gibsonized player at `end` in to even it.
                        assign_pairings_for_segment(
                            &ctx, pairings_start, start, end as isize, &[],
                            &mut pairings, &mut segment_round_factors, rng,
                        );
                        pairings_start += in_group + 1;
                        for g in gibson_groups.iter_mut().take(end + 1).skip(start) {
                            *g = next_gibson_group;
                        }
                        next_gibson_group += 1;
                        gibson_is_leftover = false;
                    } else {
                        // Even group: the gibsonized player joins the bottom group.
                        assign_pairings_for_segment(
                            &ctx, pairings_start, start, end as isize - 1, &[],
                            &mut pairings, &mut segment_round_factors, rng,
                        );
                        pairings_start += in_group;
                        for g in gibson_groups.iter_mut().take(end).skip(start) {
                            *g = next_gibson_group;
                        }
                        next_gibson_group += 1;
                        gibson_groups[end] = 0;
                    }
                }
                if gibson_is_leftover {
                    leftover_gibson.push(end);
                }
                start = end + 1;
            }
            end += 1;
        }

        // The previous simulation ran with the same parameters: nothing to rerun.
        if let Some(prev) = prev_segment_round_factors {
            if prev == segment_round_factors.as_slice() {
                return None;
            }
        }

        let rank_of = self.player_idx_to_rank_idx();
        self.backup();
        let mut highest_control_loss = -1i32;
        let mut via_lock_fallback = false;
        let mut lock_run_end = -1i32;
        let mut all_control_losses: Option<BTreeMap<usize, usize>> = None;
        let mut vs_first_wins: Option<BTreeMap<usize, usize>> = None;
        let mut total_sims = 0usize;

        if lowest_hope_control_losser < 0 {
            total_sims += self.run_parallel_sims(sims, rng, rr, &pairings, &mut results, &rank_of);
            let mut ranks_to_check = vec![req.place_prizes as usize - 1];
            if !gibsonized[0] {
                ranks_to_check.push(0);
            }
            while !sim_results_reached_threshold(
                &results, &ranks_to_check, total_sims, req.hopefulness_threshold,
            ) {
                // Upstream re-simulates until the threshold or its clock; the
                // cap stands in for the clock.
                if total_sims + RESIM_BATCH_SIZE > req.max_sims() {
                    break;
                }
                total_sims +=
                    self.run_parallel_sims(RESIM_BATCH_SIZE, rng, rr, &pairings, &mut results, &rank_of);
            }
        } else {
            // Every candidate rank from 2nd down to the lowest hopeful-for-1st
            // contender. (Upstream tried a binary search; the vs1st/vsFactor gap
            // is not monotonic across ranks, so it could miss a control loss.)
            let mut losses = BTreeMap::new();
            let mut firsts = BTreeMap::new();
            for rank in 1..=lowest_hope_control_losser as usize {
                if rank as i32 == skip_rank_idx {
                    continue; // sitting out on the top-down bye
                }
                let (vs_first, vs_factor, new_sims) = self.evaluate_player_control_loss(
                    rng, sims, rounds_remaining, &mut pairings, rank, &mut firsts, &mut losses,
                );
                total_sims += new_sims;
                // With many rounds remaining, control loss needs a perfect vs1st.
                if rounds_remaining > CONTROL_LOSS_WIN_ALL_VS_FIRST_ROUNDS_THRESH && vs_first < sims {
                    continue;
                }
                if (vs_first as f64 - vs_factor as f64) >= req.control_loss_threshold * sims as f64 {
                    highest_control_loss = rank as i32;
                    break;
                }
            }
            // With exactly 2 rounds remaining, the player just below a run of
            // ranks who win outright regardless of opponent is a candidate too.
            if highest_control_loss == -1 && rounds_remaining == 2 {
                let mut lowest_full_win = -1i32;
                for rank in 1..=lowest_hope_control_losser as usize {
                    if rank as i32 == skip_rank_idx {
                        continue;
                    }
                    let (vs_first, vs_factor, _) = self.evaluate_player_control_loss(
                        rng, sims, rounds_remaining, &mut pairings, rank, &mut firsts, &mut losses,
                    );
                    if vs_first == sims && vs_factor == sims {
                        lowest_full_win = rank as i32;
                    } else if lowest_full_win != -1 {
                        break;
                    }
                }
                if lowest_full_win != -1 {
                    let mut b = lowest_full_win + 1;
                    while b == skip_rank_idx {
                        b += 1;
                    }
                    if b <= lowest_hope_control_losser {
                        let (vs_first, vs_factor, _) = self.evaluate_player_control_loss(
                            rng, sims, rounds_remaining, &mut pairings, b as usize,
                            &mut firsts, &mut losses,
                        );
                        if vs_first > vs_factor {
                            highest_control_loss = b;
                            via_lock_fallback = true;
                            lock_run_end = lowest_full_win;
                        }
                    }
                }
            }
            all_control_losses = Some(losses);
            vs_first_wins = Some(firsts);
        }

        Some(SimResults {
            final_ranks: results,
            pairings,
            gibson_groups,
            gibsonized_players: gibsonized,
            highest_control_loss_rank_idx: highest_control_loss,
            control_loss_via_lock_fallback: via_lock_fallback,
            control_loss_lock_run_end_rank_idx: lock_run_end,
            all_control_losses,
            vs_first_wins,
            segment_round_factors,
            total_sims,
        })
    }

    /// `evaluatePlayerControlLoss`: cached vsFirst / vsFactorPair wins for a
    /// rank, and how many sims this call ran.
    #[allow(clippy::too_many_arguments)]
    fn evaluate_player_control_loss(
        &mut self,
        rng: &mut Rand,
        sims: usize,
        rounds_remaining: i32,
        pairings: &mut [Vec<usize>],
        rank_idx: usize,
        vs_first_wins: &mut BTreeMap<usize, usize>,
        all_control_losses: &mut BTreeMap<usize, usize>,
    ) -> (usize, usize, usize) {
        if let Some(&vs_first) = vs_first_wins.get(&rank_idx) {
            return (vs_first, all_control_losses[&rank_idx], 0);
        }
        let player = self.player_index(rank_idx);
        let vs_first = self.run_parallel_sim_force_winner(rng, sims, rounds_remaining, pairings, player, true);
        vs_first_wins.insert(rank_idx, vs_first);
        let vs_factor = self.run_parallel_sim_force_winner(rng, sims, rounds_remaining, pairings, player, false);
        all_control_losses.insert(rank_idx, vs_factor);
        (vs_first, vs_factor, 2)
    }

    /// `simToEndAndRecordResults`, without the clock.
    fn sim_to_end_and_record(
        &mut self,
        rounds_remaining: usize,
        rng: &mut Rand,
        pairings: &[Vec<usize>],
        results: &mut [Vec<u64>],
        rank_of: &HashMap<usize, usize>,
    ) {
        for round in 0..rounds_remaining {
            self.sim_round(rng, pairings, round, -1);
        }
        for (rank, &r) in self.records.iter().enumerate() {
            results[rank_of[&index_of(r)]][rank] += 1;
        }
        self.restore_from_backup();
    }

    /// `simRound`: play one simulated round over rank positions, then re-sort.
    fn sim_round(&mut self, rng: &mut Rand, pairings: &[Vec<usize>], round: usize, forced_winner: i32) {
        let n = self.records.len();
        let num_diffs = self.possible_results.len();
        let mut pair = 0;
        while pair < n {
            let p1 = pairings[round][pair];
            let p2 = pairings[round][pair + 1];
            let (winner, loser, result_idx) = if forced_winner >= 0 && p1 as i32 == forced_winner {
                // A forced winner never draws.
                (p1, p2, rng.intn(num_diffs - self.tie_results) + self.tie_results)
            } else if forced_winner >= 0 && p2 as i32 == forced_winner {
                (p2, p1, rng.intn(num_diffs - self.tie_results) + self.tie_results)
            } else {
                let coin = rng.intn(2);
                let (w, l) = if coin == 0 { (p1, p2) } else { (p2, p1) };
                (w, l, rng.intn(num_diffs))
            };
            let r = self.possible_results[result_idx];
            self.records[winner] = self.records[winner].wrapping_add(r);
            self.records[loser] = self.records[loser].wrapping_sub(r);
            pair += 2;
        }
        self.sort();
    }

    fn find_rank_idx(&self, player_idx: usize) -> i32 {
        self.records
            .iter()
            .position(|&r| index_of(r) == player_idx)
            .map_or(-1, |p| p as i32)
    }

    /// `simForceWinner`: `forced` wins every game; with `vs_first` they are
    /// swapped in to play rank 0 each non-final round. Returns tournament wins.
    #[allow(clippy::too_many_arguments)]
    fn sim_force_winner(
        &mut self,
        rng: &mut Rand,
        sims: usize,
        rounds_remaining: usize,
        pairings: &mut [Vec<usize>],
        forced_player: usize,
        vs_first: bool,
    ) -> usize {
        let mut wins = 0;
        for _ in 0..sims {
            for round in 0..rounds_remaining {
                let forced_rank = self.find_rank_idx(forced_player);
                let switch_idx = pairings[round]
                    .iter()
                    .position(|&r| r as i32 == forced_rank)
                    .map_or(-1, |p| p as i32);
                let (mut a, mut b) = (-1i32, -1i32);
                if vs_first && round < rounds_remaining - 1 {
                    // Move the forced winner to position 1, to play 1st. The
                    // final round is always KOTH and resolves itself.
                    a = 1;
                    b = switch_idx;
                } else if switch_idx == 1 && round < rounds_remaining - 1 {
                    // Already 1st's factor-pair opponent: swap with another
                    // pairing so they don't play 1st.
                    a = 1;
                    let target = if forced_rank == 1 { 2 } else { 1 };
                    b = pairings[round].iter().position(|&r| r == target).map_or(-1, |p| p as i32);
                }
                if a >= 0 && b >= 0 {
                    pairings[round].swap(a as usize, b as usize);
                }
                self.sim_round(rng, pairings, round, forced_rank);
                if a >= 0 && b >= 0 {
                    pairings[round].swap(a as usize, b as usize);
                }
            }
            if self.player_index(0) == forced_player {
                wins += 1;
            }
            self.restore_from_backup();
        }
        wins
    }

    /// Worker seeds, in worker order, from the main RNG — exactly as upstream
    /// draws them before launching its goroutines.
    fn worker_plan(sims: usize, rng: &mut Rand) -> Vec<(u64, usize)> {
        let workers = SIM_WORKERS.min(sims);
        if workers == 0 {
            return Vec::new();
        }
        let per = sims / workers;
        let rem = sims % workers;
        (0..workers)
            .map(|w| (rng.int63() as u64, per + usize::from(w < rem)))
            .collect()
    }

    /// `runParallelSims`. Integer tallies summed across workers do not depend
    /// on the order the workers finish in, so threads change nothing.
    fn run_parallel_sims(
        &self,
        sims: usize,
        rng: &mut Rand,
        rounds_remaining: usize,
        pairings: &[Vec<usize>],
        results: &mut [Vec<u64>],
        rank_of: &HashMap<usize, usize>,
    ) -> usize {
        let plan = Self::worker_plan(sims, rng);
        let n = self.records.len();
        let run = |(seed, count): (u64, usize)| {
            let mut local = self.clone();
            let mut wr = Rand::new(seed);
            let mut tally = vec![vec![0u64; n]; n];
            for _ in 0..count {
                local.sim_to_end_and_record(rounds_remaining, &mut wr, pairings, &mut tally, rank_of);
            }
            tally
        };
        for tally in run_workers(&plan, run) {
            for (row, add) in results.iter_mut().zip(tally) {
                for (cell, a) in row.iter_mut().zip(add) {
                    *cell += a;
                }
            }
        }
        sims
    }

    /// `runParallelSimForceWinner`: each worker gets its own pairings copy.
    fn run_parallel_sim_force_winner(
        &self,
        rng: &mut Rand,
        sims: usize,
        rounds_remaining: i32,
        pairings: &[Vec<usize>],
        forced_player: usize,
        vs_first: bool,
    ) -> usize {
        let plan = Self::worker_plan(sims, rng);
        let rr = rounds_remaining.max(0) as usize;
        let run = |(seed, count): (u64, usize)| {
            let mut local = self.clone();
            let mut wr = Rand::new(seed);
            let mut lp = pairings.to_vec();
            local.sim_force_winner(&mut wr, count, rr, &mut lp, forced_player, vs_first)
        };
        run_workers(&plan, run).into_iter().sum()
    }

    /// `RunParallelSimForceWinner` (exported upstream, for Factor 3).
    pub fn run_force_winner(
        &mut self,
        rng: &mut Rand,
        sims: usize,
        rounds_remaining: i32,
        pairings: &[Vec<usize>],
        forced_player: usize,
        vs_first: bool,
    ) -> usize {
        let evener = self.add_evener(rounds_remaining);
        self.backup();
        let wins = self.run_parallel_sim_force_winner(rng, sims, rounds_remaining, pairings, forced_player, vs_first);
        if evener {
            self.remove_evener();
        }
        wins
    }

    /// `RunSimsWithPairings`: sims over pre-built pairings (Factor 3), no
    /// re-simulation. Returns final ranks and the number of sims.
    pub fn run_sims_with_pairings(
        &mut self,
        rng: &mut Rand,
        sims: usize,
        rounds_remaining: i32,
        pairings: &[Vec<usize>],
    ) -> (Vec<Vec<u64>>, usize) {
        let evener = self.add_evener(rounds_remaining);
        let n = self.records.len();
        let mut results = vec![vec![0u64; n]; n];
        let rank_of = self.player_idx_to_rank_idx();
        self.backup();
        let total = self.run_parallel_sims(sims, rng, rounds_remaining.max(0) as usize, pairings, &mut results, &rank_of);
        if evener {
            self.remove_evener();
            for row in results.iter_mut() {
                row.truncate(n - 1);
            }
            results.truncate(n - 1);
        }
        (results, total)
    }

    fn player_idx_to_rank_idx(&self) -> HashMap<usize, usize> {
        self.records.iter().enumerate().map(|(rank, &r)| (index_of(r), rank)).collect()
    }
}

/// Run each worker, in parallel where threads exist. Results come back in
/// worker order either way.
fn run_workers<T: Send, F: Fn((u64, usize)) -> T + Sync>(plan: &[(u64, usize)], run: F) -> Vec<T> {
    #[cfg(not(target_arch = "wasm32"))]
    {
        std::thread::scope(|s| {
            let run = &run;
            let handles: Vec<_> = plan.iter().map(|&w| s.spawn(move || run(w))).collect();
            handles.into_iter().map(|h| h.join().expect("sim worker panicked")).collect()
        })
    }
    #[cfg(target_arch = "wasm32")]
    {
        plan.iter().map(|&w| run(w)).collect()
    }
}

/// `getCumeGibsonSpread`.
pub fn cume_gibson_spread(req: &Request) -> i64 {
    req.gibson_spread as i64 * req.rounds_remaining() as i64 * 2
}

struct SegmentCtx {
    rounds_remaining: usize,
    max_factor: i32,
    total_players: usize,
    control_loss: bool,
}

/// `assignPairingsForSegment`: simulation pairings for ranks `[start, end]`
/// over every remaining round. Normal sims: factor pairs then factor M/2 for
/// the rest. Control-loss sims: one factor pair (top vs Nth), the rest paired
/// randomly within half the field's rank distance. The last round is KOTH.
#[allow(clippy::too_many_arguments)]
fn assign_pairings_for_segment(
    ctx: &SegmentCtx,
    pairings_start: usize,
    start: usize,
    end: isize,
    leftover_gibson: &[usize],
    pairings: &mut [Vec<usize>],
    segment_round_factors: &mut Vec<usize>,
    rng: &mut Rand,
) {
    let num_players = (end - start as isize + 1).max(0) as usize;
    for rounds_remaining in (1..=ctx.rounds_remaining).rev() {
        let round_factor = rounds_remaining
            .min(ctx.max_factor.max(0) as usize)
            .min(num_players / 2);
        segment_round_factors.push(round_factor);
        let round = ctx.rounds_remaining - rounds_remaining;
        let row = &mut pairings[round];

        if rounds_remaining == 1 {
            for k in 0..num_players / 2 {
                row[pairings_start + 2 * k] = start + 2 * k;
                row[pairings_start + 2 * k + 1] = start + 2 * k + 1;
            }
            if num_players % 2 == 1 {
                row[pairings_start + num_players - 1] = start + num_players - 1;
            }
            for (i, &g) in leftover_gibson.iter().enumerate() {
                row[pairings_start + num_players + i] = g;
            }
            continue;
        }

        if ctx.control_loss {
            row[pairings_start] = start;
            row[pairings_start + 1] = start + round_factor;
            let remaining: Vec<usize> = (start..start + num_players)
                .filter(|&r| r != start && r != start + round_factor)
                .collect();
            let half = ctx.total_players / 2;
            let mut best = remaining.clone();
            let mut best_violations = count_distance_violations(&best, half);
            let mut shuffled = remaining;
            let mut attempt = 0;
            while attempt < 10 && best_violations > 0 {
                rng.shuffle(&mut shuffled);
                let v = count_distance_violations(&shuffled, half);
                if v < best_violations {
                    best.clone_from(&shuffled);
                    best_violations = v;
                }
                attempt += 1;
            }
            let mut i = 0;
            while i + 1 < best.len() {
                row[pairings_start + 2 + i] = best[i];
                row[pairings_start + 2 + i + 1] = best[i + 1];
                i += 2;
            }
            if best.len() % 2 == 1 {
                row[pairings_start + 2 + best.len() - 1] = best[best.len() - 1];
            }
        } else {
            for f in 0..round_factor {
                row[pairings_start + 2 * f] = start + f;
                row[pairings_start + 2 * f + 1] = start + f + round_factor;
            }
            let remaining = num_players - 2 * round_factor;
            let remaining_factor = remaining / 2;
            let rem_start = start + 2 * round_factor;
            let rem_pair_start = pairings_start + 2 * round_factor;
            for p in 0..remaining_factor {
                row[rem_pair_start + 2 * p] = rem_start + p;
                row[rem_pair_start + 2 * p + 1] = rem_start + p + remaining_factor;
            }
            if remaining % 2 == 1 {
                row[rem_pair_start + remaining - 1] = rem_start + remaining - 1;
            }
        }

        for (i, &g) in leftover_gibson.iter().enumerate() {
            row[pairings_start + num_players + i] = g;
        }
    }
}

fn count_distance_violations(players: &[usize], max_dist: usize) -> usize {
    players
        .chunks_exact(2)
        .filter(|p| p[0].abs_diff(p[1]) > max_dist)
        .count()
}

/// `clopperPearson`: the exact binomial confidence interval.
fn clopper_pearson(k: u64, n: usize, alpha: f64) -> (f64, f64) {
    let lower = if k == 0 {
        0.0
    } else {
        beta_quantile(k as f64, (n as u64 - k + 1) as f64, alpha / 2.0)
    };
    let upper = if k as usize == n {
        1.0
    } else {
        beta_quantile((k + 1) as f64, (n as u64 - k) as f64, 1.0 - alpha / 2.0)
    };
    (lower, upper)
}

/// `simResultsReachedThreshold`: every player's chance of each checked rank is
/// confidently on one side of `y` (Bonferroni over players × ranks).
fn sim_results_reached_threshold(results: &[Vec<u64>], ranks_to_check: &[usize], n: usize, y: f64) -> bool {
    let num_ranks = results.len();
    let big_n = num_ranks * ranks_to_check.len();
    let alpha_per = (1.0 - SIM_CONFIDENCE) / big_n as f64;
    for &rank in ranks_to_check {
        for row in results.iter().take(num_ranks) {
            let (lower, upper) = clopper_pearson(row[rank], n, alpha_per);
            if !(lower > y || upper < y) {
                return false;
            }
        }
    }
    true
}
