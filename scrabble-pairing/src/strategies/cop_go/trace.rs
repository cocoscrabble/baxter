//! A structured account of what the port computed, for comparing each stage
//! against the Go oracle's log (`tools/cop-go-oracle/compare.py --stages`).
//! Not used when pairing.

use serde::Serialize;

use super::precomp::{get_precomp_data, PrecompData};
use super::rand::Rand;
use super::request::Request;
use super::standings::{SimResults, Standings};
use super::verify::{verify, PairError};

#[derive(Serialize)]
pub struct SimTrace {
    pub factor: i32,
    /// Player index (0-based) at each starting rank.
    pub players: Vec<usize>,
    /// `final_ranks[starting rank][final rank]`.
    pub final_ranks: Vec<Vec<u64>>,
    pub total_sims: usize,
}

/// The "Precomp Data" table, by rank.
#[derive(Serialize)]
pub struct PrecompTrace {
    pub gibsonized: Vec<bool>,
    pub gibson_groups: Vec<i32>,
    pub highest_rank_hopefully: Vec<usize>,
    pub highest_rank_absolutely: Vec<usize>,
    /// Present when the control-loss search ran (keyed by rank).
    pub vs_first: Option<std::collections::BTreeMap<usize, usize>>,
    pub vs_factor: Option<std::collections::BTreeMap<usize, usize>>,
    /// Player index, or null.
    pub destinys_child: Option<usize>,
}

#[derive(Serialize, Default)]
pub struct Trace {
    pub seed: u64,
    /// Set when the request is refused (upstream's `verifyreq`); nothing else is.
    pub error: Option<PairError>,
    pub initial: Option<SimTrace>,
    pub improved: Option<SimTrace>,
    pub precomp: Option<PrecompTrace>,
}

fn precomp_trace(pd: &PrecompData) -> PrecompTrace {
    PrecompTrace {
        gibsonized: pd.gibsonized_players.clone(),
        gibson_groups: pd.gibson_groups.clone(),
        highest_rank_hopefully: pd.highest_rank_hopefully.clone(),
        highest_rank_absolutely: pd.highest_rank_absolutely.clone(),
        vs_first: pd.vs_first_wins.clone(),
        vs_factor: pd.all_control_losses.clone(),
        destinys_child: (pd.destinys_child >= 0)
            .then(|| pd.standings.player_index(pd.destinys_child as usize)),
    }
}

fn sim_trace(factor: i32, standings: &Standings, sims: &SimResults) -> SimTrace {
    SimTrace {
        factor,
        players: (0..standings.num_players()).map(|r| standings.player_index(r)).collect(),
        final_ranks: sims.final_ranks.clone(),
        total_sims: sims.total_sims,
    }
}

pub fn trace(req: &Request) -> Trace {
    let seed = req.effective_seed();
    if let Err(e) = verify(req) {
        return Trace { seed, error: Some(e), ..Default::default() };
    }
    let mut rng = Rand::new(seed);
    let pd = get_precomp_data(req, &mut rng);
    let base = &pd.baseline;
    Trace {
        seed,
        error: None,
        initial: Some(sim_trace(base.initial_factor, &pd.standings, &base.initial)),
        improved: base.improved.as_ref().map(|s| sim_trace(base.max_factor, &pd.standings, s)),
        precomp: Some(precomp_trace(&pd)),
    }
}

/// Parse a `PairRequest` (protobuf JSON) and return its trace as JSON.
pub fn trace_json(request: &str) -> Result<String, serde_json::Error> {
    let req: Request = serde_json::from_str(request)?;
    serde_json::to_string(&trace(&req))
}
