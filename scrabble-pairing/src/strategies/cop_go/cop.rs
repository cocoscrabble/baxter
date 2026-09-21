//! Upstream's `COPPair` / `copMethodPair`: one COP pairing, end to end.

use super::factor3::{compute_factor3_forced_pairings, Factor3};
use super::matching::{cop_min_weight_matching, MatchingTrace};
use super::precomp::{get_precomp_data, PrecompData};
use super::rand::Rand;
use super::request::Request;
use super::verify::{verify, PairError};

/// Everything a pairing produced: the answer, and the intermediates the trace
/// reports.
pub struct Outcome {
    /// Player index → opponent index (a bye is the player's own index; -1 for a
    /// removed player), or why there is none.
    pub pairings: Result<Vec<i32>, PairError>,
    pub seed: u64,
    pub precomp: Option<PrecompData>,
    /// Highest hopeful and absolute ranks as precomp first found them — what
    /// upstream logs — before a Factor 3 expansion recomputes them.
    pub ranks_before_factor3: Option<(Vec<usize>, Vec<usize>)>,
    pub factor3: Option<Factor3>,
    pub matching: Option<MatchingTrace>,
}

pub fn cop_pair(req: &Request) -> Outcome {
    let seed = req.effective_seed();
    if let Err(e) = verify(req) {
        return Outcome {
            pairings: Err(e),
            seed,
            precomp: None,
            ranks_before_factor3: None,
            factor3: None,
            matching: None,
        };
    }
    let mut rng = Rand::new(seed);
    let mut pd = get_precomp_data(req, &mut rng);
    let ranks_before_factor3 =
        Some((pd.highest_rank_hopefully.clone(), pd.highest_rank_absolutely.clone()));
    let f3 = compute_factor3_forced_pairings(req, &mut pd, &mut rng);
    if let Some((final_ranks, total_sims)) = &f3.sim {
        pd.apply_factor3_sim(req, final_ranks, *total_sims);
    }
    let (pairings, matching) = cop_min_weight_matching(req, &pd, &f3.forced);
    Outcome {
        pairings,
        seed,
        precomp: Some(pd),
        ranks_before_factor3,
        factor3: Some(f3.decision),
        matching: Some(matching),
    }
}
