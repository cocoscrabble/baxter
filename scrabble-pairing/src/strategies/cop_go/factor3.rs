//! Upstream's `computeFactor3ForcedPairings`: in the second-to-last round,
//! whether to force 1v4, 2v5, 3v6 instead of the usual factor pairing.

use serde::Serialize;

use super::precomp::{compute_top_down_bye_rank_idx, PrecompData};
use super::rand::Rand;
use super::request::Request;

/// The minimum gain in win-outright probability at least one of 4th/5th/6th
/// must get from Factor 3 for it to fire, so it cannot fire only to help the
/// leader while taking destiny control from the field.
const F3_MIN_WIN_CHANCE_GAIN: f64 = 0.02;

/// What Factor 3 decided, for the trace.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub enum Factor3 {
    /// Did not fire; why (upstream's log reason, abridged).
    Skipped(&'static str),
    /// 2nd or 3rd would lose control of their destiny: only 1st v that player.
    ControlLoss { player: usize },
    /// The full expansion: 1v4, 2v5, 3v6.
    Expansion,
}

pub struct Factor3Result {
    pub decision: Factor3,
    /// Forced (player, player) pairs.
    pub forced: Vec<(usize, usize)>,
    /// The Factor-3 sim, only when the full expansion fires (for
    /// `PrecompData::apply_factor3_sim`).
    pub sim: Option<(Vec<Vec<u64>>, usize)>,
}

fn skipped(reason: &'static str) -> Factor3Result {
    Factor3Result { decision: Factor3::Skipped(reason), forced: Vec::new(), sim: None }
}

pub fn compute_factor3_forced_pairings(req: &Request, pd: &mut PrecompData, rng: &mut Rand) -> Factor3Result {
    if req.rounds_remaining() != 2 {
        return skipped("not 2 rounds remaining");
    }
    let n = pd.standings.num_players();
    if n < 6 {
        return skipped("fewer than 6 players");
    }
    // Top-down byes take precedence: a bye inside the top 6 cancels Factor 3.
    let tdb = compute_top_down_bye_rank_idx(req, &pd.standings, &pd.pairing_counts);
    if (0..6).contains(&tdb) {
        return skipped("top-down bye in the top 6");
    }

    // Penultimate round: 0v3, 1v4, 2v5, then factor M/2 for the rest; final
    // round KOTH. Rounded up to even so the sims can pair (a dummy bye player
    // is added for an odd field).
    let len = n + n % 2;
    let mut f3 = vec![vec![0usize; len]; 2];
    for i in 0..3 {
        f3[0][2 * i] = i;
        f3[0][2 * i + 1] = i + 3;
    }
    let rem_factor = (len - 6) / 2;
    for i in 0..rem_factor {
        f3[0][6 + 2 * i] = 6 + i;
        f3[0][6 + 2 * i + 1] = 6 + i + rem_factor;
    }
    for i in 0..len / 2 {
        f3[1][2 * i] = 2 * i;
        f3[1][2 * i + 1] = 2 * i + 1;
    }

    let (final_ranks, total_sims) =
        pd.standings.run_sims_with_pairings(rng, req.division_sims as usize, 2, &f3);
    if total_sims == 0 {
        return skipped("zero sims completed");
    }

    // Does 2nd or 3rd lose control under factor 3? Each wins out either way;
    // the difference is playing 1st (vs_first) or their factor-3 opponent.
    let control_sims = req.control_loss_sims as usize;
    let threshold = req.control_loss_threshold * control_sims as f64;
    let rounds_remaining = req.rounds_remaining();
    let p0 = pd.standings.player_index(0);
    for rank in [1, 2] {
        let p = pd.standings.player_index(rank);
        let vs_first = pd.standings.run_force_winner(rng, control_sims, rounds_remaining, &f3, p, true);
        let vs_factor3 = pd.standings.run_force_winner(rng, control_sims, rounds_remaining, &f3, p, false);
        if (vs_first as f64 - vs_factor3 as f64) >= threshold {
            return Factor3Result {
                decision: Factor3::ControlLoss { player: p },
                forced: vec![(p0, p)],
                sim: None,
            };
        }
    }

    // 4th/5th/6th must each be hopeful for 1st/2nd/3rd-or-better.
    let min_wins = (total_sims as f64 * req.hopefulness_threshold).round() as u64;
    let at_least = |row: &[u64], target: usize| row[..=target].iter().sum::<u64>();
    if at_least(&final_ranks[3], 0) < min_wins
        || at_least(&final_ranks[4], 1) < min_wins
        || at_least(&final_ranks[5], 2) < min_wins
    {
        return skipped("hopefulness threshold not met");
    }

    // And at least one of them must gain a real shot at winning outright.
    if pd.baseline_total_sims > 0 {
        let base = |r: usize| pd.baseline_final_ranks[r][0] as f64 / pd.baseline_total_sims as f64;
        let f3win = |r: usize| final_ranks[r][0] as f64 / total_sims as f64;
        let gain = (3..6).map(|r| f3win(r) - base(r)).fold(f64::NEG_INFINITY, f64::max);
        if gain < F3_MIN_WIN_CHANCE_GAIN {
            return skipped("no meaningful win-chance gain");
        }
    }

    let p = |r| pd.standings.player_index(r);
    Factor3Result {
        decision: Factor3::Expansion,
        forced: vec![(p(0), p(3)), (p(1), p(4)), (p(2), p(5))],
        sim: Some((final_ranks, total_sims)),
    }
}
