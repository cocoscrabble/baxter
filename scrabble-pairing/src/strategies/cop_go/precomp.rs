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
