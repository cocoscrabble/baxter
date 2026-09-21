//! A structured account of what the port computed, for comparing each stage
//! against the Go oracle's log (`tools/cop-go-oracle/compare.py --stages`).
//! Not used when pairing.

use serde::Serialize;

use super::precomp::baseline_sims;
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

#[derive(Serialize, Default)]
pub struct Trace {
    pub seed: u64,
    /// Set when the request is refused (upstream's `verifyreq`); nothing else is.
    pub error: Option<PairError>,
    pub initial: Option<SimTrace>,
    pub improved: Option<SimTrace>,
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
    let mut standings = Standings::from_request(req);
    let base = baseline_sims(req, &mut standings, &mut rng);
    Trace {
        seed,
        error: None,
        initial: Some(sim_trace(base.initial_factor, &standings, &base.initial)),
        improved: base.improved.as_ref().map(|s| sim_trace(base.max_factor, &standings, s)),
    }
}

/// Parse a `PairRequest` (protobuf JSON) and return its trace as JSON.
pub fn trace_json(request: &str) -> Result<String, serde_json::Error> {
    let req: Request = serde_json::from_str(request)?;
    serde_json::to_string(&trace(&req))
}
