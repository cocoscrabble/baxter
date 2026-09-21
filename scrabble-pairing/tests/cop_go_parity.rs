//! The Go re-port against upstream, frozen: each case's `request` is a liwords
//! fixture and the expectations were read from the Go oracle's log at the pin
//! in `tools/cop-go-oracle/go.mod`. The live comparison is
//! `tools/cop-go-oracle/compare.py --stages`; this keeps the build honest
//! without Go installed.
//!
//! Stage 1: the baseline simulations — which rely on the ported RNG, worker
//! seeding, score table and sim loop all being exact — reproduce upstream's
//! tallies to the last sim.
//! Stage 2: the "Precomp Data" table (gibson status and groups, hopeful and
//! absolute ranks, the control-loss search) and the destiny's child.
//! Stages 3-4: the pairings themselves — policies, Factor 3, matching, retry
//! and assembly — exactly upstream's. The `scenario_*` cases are requests
//! upstream's own tests pair (`tools/cop-go-oracle/dump-scenarios.sh`), chosen
//! for rules no real-tournament fixture reaches. (The live comparison also diffs every
//! weight of every pair; freezing those tables is not worth their size.)

use scrabble_pairing::strategies::cop_go::trace::trace_json;
use serde_json::Value;

const STAGES: &str = include_str!("data/cop_go/stages.json");

/// Cases that run hundreds of thousands of sims (among them the only case that
/// re-sims with an improved factor, and the only one with a destiny's child)
/// — seconds in release, far longer in debug.
const HEAVY: [&str; 5] = [
    "albany_after_round16",
    "almost_gibsonized",
    "july4th2026_random_start_run305_after_round24",
    "scenario_leader_vs_third",
    "scenario_top4_lock",
];

#[test]
fn stages_match_upstream_exactly() {
    check(false);
}

#[test]
#[cfg_attr(debug_assertions, ignore = "hundreds of thousands of sims; run with --release")]
fn heavy_stages_match_upstream_exactly() {
    check(true);
}

fn check(heavy: bool) {
    let cases: Vec<Value> = serde_json::from_str(STAGES).expect("parse test data");
    let cases: Vec<&Value> = cases
        .iter()
        .filter(|c| HEAVY.contains(&c["name"].as_str().unwrap()) == heavy)
        .collect();
    assert!(!cases.is_empty());
    if heavy {
        assert!(cases.iter().any(|c| !c["improved"].is_null()), "no case re-sims");
        assert!(
            cases.iter().any(|c| !c["precomp"]["destinys_child"].is_null()),
            "no case has a destiny's child"
        );
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
        check_precomp(name, &trace["precomp"], &case["precomp"]);
        if case["error"].is_null() {
            assert_eq!(trace["pairings"], case["pairings"], "{name}: pairings");
        } else {
            // Upstream refuses this round after matching; so must we, alike.
            assert_eq!(trace["error"]["code"], case["error"], "{name}: error");
        }
    }
}

/// The log prints one row per real player; in an odd field upstream's gibson
/// arrays also carry the sim's dummy bye player, so compare the printed rows.
fn check_precomp(name: &str, ours: &Value, theirs: &Value) {
    let rows = theirs["highest_rank_hopefully"].as_array().unwrap().len();
    for (key, want) in theirs.as_object().unwrap() {
        let got = match &ours[key] {
            Value::Array(items) => Value::Array(items.iter().take(rows).cloned().collect()),
            other => other.clone(),
        };
        assert_eq!(&got, want, "{name}: precomp {key}");
    }
}
