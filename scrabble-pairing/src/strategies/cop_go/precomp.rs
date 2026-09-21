//! Upstream's `pkg/pair/copdata`: everything COP works out before it weighs a
//! single pairing. (Stage 1 of the port: the baseline simulations.)

use super::rand::Rand;
use super::request::Request;
use super::standings::{SimResults, Standings};

/// The simulations `GetPrecompData` opens with: an initial sim with a factor
/// ceiling of every remaining round, then — only if the initial results show a
/// tighter ceiling that actually shrinks the leading gibson group's factor — a
/// re-sim with that tighter ceiling.
pub struct BaselineSims {
    pub initial_factor: i32,
    pub initial: SimResults,
    pub max_factor: i32,
    /// `None` when no improvement was made ("No factor improvement made.").
    pub improved: Option<SimResults>,
}

impl BaselineSims {
    /// The results the rest of COP runs on.
    pub fn effective(&self) -> &SimResults {
        self.improved.as_ref().unwrap_or(&self.initial)
    }
}

pub fn baseline_sims(req: &Request, standings: &mut Standings, rng: &mut Rand) -> BaselineSims {
    let initial_factor = req.rounds_remaining();
    let initial = standings
        .sim_factor_pair_all(req, rng, req.division_sims as usize, initial_factor, -1, None, -1)
        .expect("an initial sim with no previous factors always runs");
    let n = standings.num_players();

    // The factor is tightened relative to the highest non-gibsonized rank.
    let highest_nongibsonized = initial.gibsonized_players.iter().position(|&g| !g).unwrap_or(0);

    // If 1st played the (1 + N)th but the (1 + N)th never reached 1st, they
    // should never have played 1st: count how far down anyone reached it.
    let mut max_factor = 0;
    for rank in highest_nongibsonized + 1..n {
        if initial.final_ranks[rank][highest_nongibsonized] > 0 {
            max_factor += 1;
        } else {
            break;
        }
    }

    let group = initial.gibson_groups[highest_nongibsonized];
    let in_group = initial.gibson_groups[highest_nongibsonized..]
        .iter()
        .take_while(|&&g| g == group)
        .count();

    // Only re-sim when the tighter bound shrinks the group's max factor.
    let improved = if (max_factor * 2) < in_group as i32 {
        standings.sim_factor_pair_all(
            req, rng, req.division_sims as usize, max_factor, -1,
            Some(&initial.segment_round_factors), -1,
        )
    } else {
        None
    };
    BaselineSims { initial_factor, initial, max_factor, improved }
}

use std::collections::{BTreeMap, HashMap};

use super::standings::BYE_PLAYER_INDEX;

/// Combined 1st-place chance of the leader and 2nd above which everyone else
/// needs only half as many sims to count as hopeful for 1st or 2nd.
const RUNAWAY_LEADERS_WIN_PCT_THRESHOLD: f64 = 0.80;

/// `IsLastQuarter`.
pub fn is_last_quarter(round_pairings_remaining: i32, rounds: i32) -> bool {
    round_pairings_remaining * 4 <= rounds
}

/// A pairing's key in the times-played count (`GetPairingKey`): a bye (a
/// self-pairing, or the bye player) is `(player, BYE)`; otherwise the higher
/// index first.
pub fn pairing_key(player: usize, opp: usize) -> (usize, usize) {
    if player == opp || opp == BYE_PLAYER_INDEX {
        (player, BYE_PLAYER_INDEX)
    } else {
        (player.max(opp), player.min(opp))
    }
}

/// `PrecompData`: indexed by player index where noted, else by rank.
pub struct PrecompData {
    pub standings: Standings,
    pub pairing_counts: HashMap<(usize, usize), usize>,
    /// By player index.
    pub repeat_counts: Vec<usize>,
    pub highest_rank_hopefully: Vec<usize>,
    pub highest_rank_absolutely: Vec<usize>,
    pub lowest_rank_absolutely: Vec<usize>,
    /// Signed like upstream's ints: a slot below rank 0's own place is -1.
    pub lowest_possible_hope_nth: Vec<i32>,
    /// Rank promoted to fix an odd hopeful-to-cash window, or -1.
    pub hopeful_to_cash_promoted_rank_idx: i32,
    /// Rank of the destiny's child, or -1.
    pub destinys_child: i32,
    pub gibson_groups: Vec<i32>,
    pub gibsonized_players: Vec<bool>,
    pub complete_pairings: usize,
    pub baseline_final_ranks: Vec<Vec<u64>>,
    pub baseline_total_sims: usize,
    /// For the trace: what the control-loss search found, by rank.
    pub vs_first_wins: Option<BTreeMap<usize, usize>>,
    pub all_control_losses: Option<BTreeMap<usize, usize>>,
    pub baseline: BaselineSims,
}

struct HopefulRankData {
    highest_rank_hopefully: Vec<usize>,
    highest_rank_absolutely: Vec<usize>,
    lowest_rank_absolutely: Vec<usize>,
    lowest_possible_hope_nth: Vec<i32>,
    hopeful_to_cash_promoted_rank_idx: i32,
}

/// `computeHopefulRankData`: hopeful/absolute rank boundaries from a
/// final-rank simulation.
fn compute_hopeful_rank_data(
    req: &Request,
    num_players: usize,
    complete_pairings: usize,
    final_ranks: &[Vec<u64>],
    total_sims: usize,
    gibsonized: &[bool],
) -> HopefulRankData {
    let min_wins = (total_sims as f64 * req.hopefulness_threshold).round() as u64;
    let half_min_wins = (min_wins as f64 / 2.0).round() as u64;
    // Only with the leader not gibsonized: a locked leader is gibsonization at
    // work, not a runaway race.
    let runaway = num_players >= 2 && total_sims > 0 && !gibsonized[0] && {
        let leader = final_ranks[0][0] as f64 / total_sims as f64;
        let second = final_ranks[1][0] as f64 / total_sims as f64;
        leader + second > RUNAWAY_LEADERS_WIN_PCT_THRESHOLD
    };

    let mut hopefully = vec![0usize; num_players];
    let mut absolutely = vec![0usize; num_players];
    let mut lowest = vec![0usize; num_players];
    for p in 0..num_players {
        let mut sum = 0u64;
        let mut hopeful_rank = num_players - 1;
        let mut absolute_rank = num_players - 1;
        for rank in 0..num_players {
            let cell = final_ranks[p][rank];
            if sum == 0 && cell > 0 {
                absolute_rank = rank;
            }
            sum += cell;
            let threshold = if runaway && rank <= 1 { half_min_wins } else { min_wins };
            if sum >= threshold {
                hopeful_rank = rank;
                break;
            }
        }
        hopefully[p] = hopeful_rank;
        absolutely[p] = absolute_rank;
        lowest[p] = (0..num_players).rev().find(|&r| final_ranks[p][r] > 0).unwrap_or(0);
    }

    let round_pairings_remaining = req.rounds - complete_pairings as i32;
    let mut promoted = -1;
    if is_last_quarter(round_pairings_remaining, req.rounds) {
        if let Some(extended) =
            hopeful_to_cash_extension_rank_idx(&hopefully, gibsonized, req.place_prizes as usize)
        {
            hopefully[extended] = req.place_prizes as usize - 1;
            promoted = extended as i32;
        }
    }
    let lowest_possible_hope_nth = compute_lowest_possible_hope_nth(&hopefully);
    HopefulRankData {
        highest_rank_hopefully: hopefully,
        highest_rank_absolutely: absolutely,
        lowest_rank_absolutely: lowest,
        lowest_possible_hope_nth,
        hopeful_to_cash_promoted_rank_idx: promoted,
    }
}

/// `computeLowestPossibleHopeNth`: for each place N, the worst-ranked player
/// still hopeful for Nth or better.
fn compute_lowest_possible_hope_nth(hopeful_ranks: &[usize]) -> Vec<i32> {
    let n = hopeful_ranks.len();
    let mut out = vec![0i32; n];
    let mut prev_place = 0usize;
    for (rank, &place) in hopeful_ranks.iter().enumerate() {
        if rank as i32 > out[place] {
            out[place] = rank as i32;
        }
        for slot in out.iter_mut().take(place).skip(prev_place + 1) {
            *slot = rank as i32 - 1;
        }
        prev_place = place;
    }
    for slot in out.iter_mut().skip(prev_place + 1) {
        *slot = n as i32 - 1;
    }
    out
}

/// `hopefulToCashExtensionRankIdx`: an odd number of non-gibsonized players in
/// the natural hopeful-to-cash window leaves one without an in-window opponent,
/// so the window is extended by a rank. Returns that rank.
fn hopeful_to_cash_extension_rank_idx(
    hopefully: &[usize],
    gibsonized: &[bool],
    place_prizes: usize,
) -> Option<usize> {
    let boundary = compute_lowest_possible_hope_nth(hopefully)[place_prizes - 1];
    let count = (0..=boundary).filter(|&r| !gibsonized[r as usize]).count();
    let extended = (boundary + 1) as usize;
    (count % 2 == 1 && extended < hopefully.len()).then_some(extended)
}

/// `ExtractPrepairedPlayers`: the partly paired round's games (player → opponent,
/// a bye as a self-pairing), how many of them are byes, and that round's index
/// (-1 when there is none).
pub fn extract_prepaired_players(req: &Request) -> (HashMap<usize, usize>, usize, i32) {
    let mut prepaired = HashMap::new();
    let mut forced_byes = 0;
    let mut round_idx = -1i32;
    let removed: std::collections::HashSet<i32> = req.removed_players.iter().copied().collect();
    if let Some(last) = req.division_pairings.last() {
        if last.contains(&-1) {
            round_idx = req.division_pairings.len() as i32 - 1;
            for (p, &o) in last.iter().enumerate() {
                if o < p as i32 || removed.contains(&(p as i32)) || removed.contains(&o) {
                    continue;
                }
                prepaired.insert(p, o as usize);
                prepaired.insert(o as usize, p);
                if p == o as usize {
                    forced_byes += 1;
                }
            }
        }
    }
    (prepaired, forced_byes, round_idx)
}

/// `ComputeTopDownByeRankIdx`: with top-down byes on and an unforced bye
/// needed, the highest-ranked player with the fewest byes so far; else -1.
pub fn compute_top_down_bye_rank_idx(
    req: &Request,
    standings: &Standings,
    pairing_counts: &HashMap<(usize, usize), usize>,
) -> i32 {
    if !req.top_down_byes {
        return -1;
    }
    let n = standings.num_players();
    let (_, forced_byes, _) = extract_prepaired_players(req);
    if (n - forced_byes).is_multiple_of(2) {
        return -1;
    }
    let mut bye_rank = -1i32;
    let mut least = req.rounds as usize + 1;
    for rank in 0..n {
        let byes = pairing_counts
            .get(&pairing_key(standings.player_index(rank), BYE_PLAYER_INDEX))
            .copied()
            .unwrap_or(0);
        if byes < least {
            least = byes;
            bye_rank = rank as i32;
        }
    }
    bye_rank
}

/// `GetPrecompData`.
pub fn get_precomp_data(req: &Request, rng: &mut Rand) -> PrecompData {
    let mut standings = Standings::from_request(req);
    let baseline = baseline_sims(req, &mut standings, rng);
    let n = standings.num_players();

    // Leading rounds with every player paired.
    let complete_pairings = req
        .division_pairings
        .iter()
        .take_while(|round| !round.contains(&-1))
        .count();

    let base = baseline.effective();
    let hrd = compute_hopeful_rank_data(
        req, n, complete_pairings, &base.final_ranks, base.total_sims, &base.gibsonized_players,
    );

    let mut pairing_counts: HashMap<(usize, usize), usize> = HashMap::new();
    let mut repeat_counts = vec![0usize; req.all_players as usize];
    for round in req.division_pairings.iter().take(complete_pairings) {
        for (p, &o) in round.iter().enumerate() {
            let o = o as usize;
            if o > p {
                continue;
            }
            let key = pairing_key(p, o);
            let count = pairing_counts.entry(key).or_insert(0);
            if *count > 0 {
                repeat_counts[p] += 1;
                if p != o {
                    repeat_counts[o] += 1;
                }
            }
            *count += 1;
        }
    }

    let mut destinys_child = -1;
    let mut vs_first_wins = None;
    let mut all_control_losses = None;
    if complete_pairings as i32 >= req.control_loss_activation_round
        && !base.gibsonized_players[0]
        && baseline.initial_factor > 1
        && baseline.max_factor > 0
    {
        let top_down_bye_rank = compute_top_down_bye_rank_idx(req, &standings, &pairing_counts);
        // A leader sitting out on the top-down bye has no opponent to be forced
        // onto: top-down byes take precedence and control loss is skipped.
        if top_down_bye_rank != 0 {
            let cl = standings
                .sim_factor_pair_all(
                    req,
                    rng,
                    req.control_loss_sims as usize,
                    baseline.max_factor,
                    hrd.lowest_possible_hope_nth[0],
                    None,
                    top_down_bye_rank,
                )
                .expect("a control-loss sim with no previous factors always runs");
            destinys_child = cl.highest_control_loss_rank_idx;
            vs_first_wins = cl.vs_first_wins;
            all_control_losses = cl.all_control_losses;
        }
    }

    let (gibson_groups, gibsonized_players, baseline_final_ranks, baseline_total_sims) = (
        base.gibson_groups.clone(),
        base.gibsonized_players.clone(),
        base.final_ranks.clone(),
        base.total_sims,
    );
    PrecompData {
        standings,
        pairing_counts,
        repeat_counts,
        highest_rank_hopefully: hrd.highest_rank_hopefully,
        highest_rank_absolutely: hrd.highest_rank_absolutely,
        lowest_rank_absolutely: hrd.lowest_rank_absolutely,
        lowest_possible_hope_nth: hrd.lowest_possible_hope_nth,
        hopeful_to_cash_promoted_rank_idx: hrd.hopeful_to_cash_promoted_rank_idx,
        destinys_child,
        gibson_groups,
        gibsonized_players,
        complete_pairings,
        baseline_final_ranks,
        baseline_total_sims,
        vs_first_wins,
        all_control_losses,
        baseline,
    }
}

impl PrecompData {
    /// `ApplyFactor3Sim`: recompute the hopeful/absolute boundaries from the
    /// Factor-3 sim once the full expansion fires. Gibson status is analytic and
    /// left alone.
    pub fn apply_factor3_sim(&mut self, req: &Request, final_ranks: &[Vec<u64>], total_sims: usize) {
        let hrd = compute_hopeful_rank_data(
            req,
            self.standings.num_players(),
            self.complete_pairings,
            final_ranks,
            total_sims,
            &self.gibsonized_players,
        );
        self.highest_rank_hopefully = hrd.highest_rank_hopefully;
        self.highest_rank_absolutely = hrd.highest_rank_absolutely;
        self.lowest_rank_absolutely = hrd.lowest_rank_absolutely;
        self.lowest_possible_hope_nth = hrd.lowest_possible_hope_nth;
        self.hopeful_to_cash_promoted_rank_idx = hrd.hopeful_to_cash_promoted_rank_idx;
    }
}
