//! The Go re-port against upstream, frozen: each case's `request` is a liwords
//! fixture and the expectations were read from the Go oracle's log at the pin
//! in `tools/cop-go-oracle/go.mod`. The live comparison is
//! `tools/cop-go-oracle/compare.py --stages`; this keeps the build honest
//! without Go installed.
//!
//! Stage 1: the baseline simulations — which rely on the ported RNG, worker
//! seeding, score table and sim loop all being exact — reproduce upstream's
//! tallies to the last sim.

use scrabble_pairing::strategies::cop_go::trace::trace_json;
use serde_json::Value;

const STAGE1: &str = include_str!("data/cop_go/stage1_sims.json");

/// Cases that run hundreds of thousands of sims (one of them the only case that
/// re-sims with an improved factor) — seconds in release, far longer in debug.
const HEAVY: [&str; 2] = ["almost_gibsonized", "july4th2026_random_start_run305_after_round24"];

#[test]
fn baseline_sims_match_upstream_exactly() {
    check_stage1(false);
}

#[test]
#[cfg_attr(debug_assertions, ignore = "hundreds of thousands of sims; run with --release")]
fn heavy_baseline_sims_match_upstream_exactly() {
    check_stage1(true);
}

fn check_stage1(heavy: bool) {
    let cases: Vec<Value> = serde_json::from_str(STAGE1).expect("parse test data");
    let cases: Vec<&Value> = cases
        .iter()
        .filter(|c| HEAVY.contains(&c["name"].as_str().unwrap()) == heavy)
        .collect();
    assert!(!cases.is_empty());
    if heavy {
        assert!(cases.iter().any(|c| !c["improved"].is_null()), "no case re-sims");
    }
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let trace: Value = serde_json::from_str(
            &trace_json(&case["request"].to_string()).expect("trace"),
        )
        .unwrap();
        for stage in ["initial", "improved"] {
            let (ours, theirs) = (&trace[stage], &case[stage]);
            if theirs.is_null() {
                assert!(ours.is_null(), "{name}: only Rust ran the {stage} sim");
                continue;
            }
            for key in ["factor", "players", "final_ranks", "total_sims"] {
                assert_eq!(ours[key], theirs[key], "{name}: {stage} {key}");
            }
        }
    }
}
