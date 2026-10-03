use std::collections::BTreeSet;

use scrabble_pairing::pair_json;
use serde_json::{json, Value};

fn opponents(round: &Value) -> BTreeSet<Vec<String>> {
    round["pairings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|pair| {
            let mut names = vec![
                pair["first"].as_str().unwrap().to_owned(),
                pair["second"].as_str().unwrap().to_owned(),
            ];
            names.sort();
            names
        })
        .collect()
}

#[test]
fn consecutive_rotations_repeat_the_opponent_order_for_even_and_odd_fields() {
    for n in [4, 5, 6] {
        let cycle = if n % 2 == 0 { n - 1 } else { n };
        for (strategy, k) in [("RoundRobin", 1), ("DoubleRoundRobin", 2)] {
            let length = cycle * k;
            let players: Vec<_> = (0..n)
                .map(|i| json!({"name": format!("P{i}"), "rating": 2000-i}))
                .collect();
            let rounds: Vec<_> = (1..=2*length).map(|r| json!({"round": r, "start_round": if r <= length { 1 } else { length+1 }, "pairing": strategy})).collect();
            let out: Value = serde_json::from_str(
                &pair_json(&json!({"players": players, "round_pairings": rounds}).to_string())
                    .unwrap(),
            )
            .unwrap();
            let out = out.as_array().unwrap();
            assert_eq!(out.len(), 2 * length);
            for round in out {
                assert!(round["error"].is_null(), "{strategy}, {n} players: {round}");
                assert_eq!(round["pairings"].as_array().unwrap().len(), (n + 1) / 2);
            }
            let mut seen = BTreeSet::new();
            for r in 0..cycle {
                let pairs = opponents(&out[r * k]);
                for pair in &pairs {
                    assert!(seen.insert(pair.clone()), "pair repeated within one cycle");
                }
                for half in 0..k {
                    assert_eq!(opponents(&out[r * k + half]), pairs);
                    assert_eq!(
                        opponents(&out[length + r * k + half]),
                        pairs,
                        "opponent order changed in cycle two"
                    );
                }
            }
            let field = if n % 2 == 0 { n } else { n + 1 };
            assert_eq!(seen.len(), field * (field - 1) / 2);
        }
    }
}
