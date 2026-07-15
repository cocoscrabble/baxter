//! The serialized I/O boundary: the types a caller (de)serializes to drive the
//! engine from JSON, without any knowledge of the host application.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::round_pairing::RoundPairing;

#[derive(Debug, Clone, Deserialize)]
pub struct PlayerData {
    pub name: String,
    pub rating: i32,
    /// A withdrawn entrant: excluded from all future pairing, but their played
    /// results still count for opponents. `#[serde(default)]` keeps older corpus
    /// cases (without the field) parsing.
    #[serde(default)]
    pub dropped: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResultSlipData {
    pub round: i32,
    pub winner_name: String,
    pub loser_name: String,
    pub winner_score: i32,
    pub loser_score: i32,
    pub winner_started: bool,
}

/// Everything a tournament needs to be paired. The full input to `pair`.
#[derive(Debug, Clone, Deserialize)]
pub struct PairingInput {
    pub players: Vec<PlayerData>,
    #[serde(default)]
    pub result_slips: Vec<ResultSlipData>,
    #[serde(default)]
    pub round_pairings: Vec<RoundPairing>,
    /// Round number -> list of unordered (name1, name2) pairs forced that round.
    /// JSON object keys are strings; serde_json parses them back to `i32`.
    #[serde(default)]
    pub fixed_pairings: HashMap<i32, Vec<(String, String)>>,
    /// Seed for the random strategies. Defaults to 0, so a run is fully
    /// reproducible; the caller supplies entropy if it wants variety.
    #[serde(default)]
    pub seed: u64,
}

/// One pairing in the output: player names plus how many times these two have
/// already met (the `repeats` count the display layer shows).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutPairing {
    pub first: String,
    pub second: String,
    pub repeats: i32,
}

/// The pairings produced for one round. On an invalid condition (an unknown
/// strategy, or a field too small for the chosen format) `pairings` is empty and
/// `error` carries the reason.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RoundResult {
    pub round: i32,
    pub pairings: Vec<OutPairing>,
    #[serde(default)]
    pub error: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_input_including_fixed_pairings() {
        let input: PairingInput = serde_json::from_str(
            r#"{
                "players": [{"name": "Alice", "rating": 1800}],
                "result_slips": [],
                "round_pairings": [{"round": 1, "start_round": 0, "pairing": "Swiss"}],
                "fixed_pairings": {"2": [["Alice", "Bob"]]},
                "seed": 12345
            }"#,
        )
        .unwrap();
        assert_eq!(input.players.len(), 1);
        assert_eq!(input.players[0].name, "Alice");
        assert_eq!(
            input.fixed_pairings[&2],
            vec![("Alice".into(), "Bob".into())]
        );
        assert_eq!(input.seed, 12345);
    }

    #[test]
    fn optional_fields_default() {
        let input: PairingInput = serde_json::from_str(r#"{"players": []}"#).unwrap();
        assert!(input.result_slips.is_empty());
        assert!(input.round_pairings.is_empty());
        assert!(input.fixed_pairings.is_empty());
        assert_eq!(input.seed, 0);
    }
}
