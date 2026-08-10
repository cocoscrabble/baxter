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

/// Tuning + prize configuration for the COP strategy (per division). Mirrors the
/// TSH config values COP.pm reads; the per-round arrays are forward-filled to the
/// round count inside the engine, so a single-element array (the common case,
/// produced by the Django adapter from a scalar setting) is fine.
#[derive(Debug, Clone, Deserialize)]
pub struct CopConfig {
    /// Number of place prizes (1-indexed count). COP's 0-indexed
    /// `lowest_ranked_payout` is `place_prizes - 1`.
    pub place_prizes: i32,
    /// Max winning spread per round, by rounds-remaining (forward-filled).
    #[serde(default)]
    pub gibson_spreads: Vec<i32>,
    /// Contender threshold: a player counts as a contender for a rank when they
    /// reach it in more than this fraction of sims. By rounds-remaining.
    #[serde(default)]
    pub hopefulness: Vec<f64>,
    /// Control-loss thresholds, by rounds-remaining (forward-filled).
    #[serde(default)]
    pub control_loss_thresholds: Vec<f64>,
    /// 0-indexed round at/after which control loss (destiny control) is enforced.
    #[serde(default)]
    pub control_loss_activation_round: i32,
    /// Monte Carlo iteration counts (unused until sims land in Phase 2).
    #[serde(default)]
    pub simulations: u32,
    #[serde(default)]
    pub always_wins_simulations: u32,
    /// Penalize giving a player a second bye.
    #[serde(default)]
    pub disallow_repeat_byes: bool,
    /// Count the rounds still to play from the round being paired rather than
    /// from `start_round`.
    ///
    /// COP reads its standings from `start_round` and, by default, derives the
    /// horizon from the same place — self-consistent, and identical either way
    /// for the usual sliding COP round where `start_round == round - 1`. They
    /// diverge only when a round pairs off an *older* snapshot (`pair_from > 1`),
    /// where counting from `start_round` overstates the rounds left and inflates
    /// the contention analysis. Set this to count `total_rounds - round + 1`
    /// instead: standings stay where they were asked for, but "how much is still
    /// to play" reflects the round actually being paired.
    #[serde(default)]
    pub horizon_from_paired_round: bool,
}

/// Tuning knobs for the Swiss family (Swiss and SwissPlusRandom).
///
/// The Google Sheets script these strategies were ported from exposes two of
/// these as `swiss_weight` and `swiss_distance`, and uses `swiss_distance` for
/// *two* different jobs: the max candidate distance inside `pair_candidates`,
/// and the size of the Swiss-paired top slice in SwissPlusRandom. They are split
/// here so each can be set independently; set both to the same value to
/// reproduce the sheet.
#[derive(Debug, Clone, Deserialize)]
pub struct SwissConfig {
    /// How heavily a repeat pairing is penalized relative to standings distance.
    #[serde(default = "default_swiss_weight")]
    pub swiss_weight: i32,
    /// Don't pair candidates this many places apart or more (strict `<`).
    #[serde(default = "default_max_distance")]
    pub max_distance: i32,
    /// SwissPlusRandom: the top this many players are paired Swiss, the rest
    /// RandomNoRepeats.
    #[serde(default = "default_spr_split")]
    pub spr_split: usize,
}

fn default_swiss_weight() -> i32 {
    30
}

fn default_max_distance() -> i32 {
    11
}

fn default_spr_split() -> usize {
    10
}

impl Default for SwissConfig {
    fn default() -> Self {
        SwissConfig {
            swiss_weight: default_swiss_weight(),
            max_distance: default_max_distance(),
            spr_split: default_spr_split(),
        }
    }
}

/// Everything a tournament needs to be paired. The full input to `pair`.
#[derive(Debug, Clone, Deserialize)]
pub struct PairingInput {
    pub players: Vec<PlayerData>,
    #[serde(default)]
    pub result_slips: Vec<ResultSlipData>,
    #[serde(default)]
    pub round_pairings: Vec<RoundPairing>,
    /// COP tuning/prize config for this division. Present only when a round uses
    /// the COP strategy; `#[serde(default)]` keeps every other caller parsing.
    #[serde(default)]
    pub cop_config: Option<CopConfig>,
    /// Swiss tuning for this division. Absent means the built-in defaults, so
    /// every existing caller pairs exactly as before.
    #[serde(default)]
    pub swiss_config: SwissConfig,
    /// Round number -> list of unordered (name1, name2) pairs forced that round.
    /// JSON object keys are strings; serde_json parses them back to `i32`.
    #[serde(default)]
    pub fixed_pairings: HashMap<i32, Vec<(String, String)>>,
    /// Round number -> the already-published pairings of every non-draft round.
    /// The round-robin solver pins these so an in-progress (partially played)
    /// round's unplayed-but-printed games are honored, not recomputed. Draft
    /// rounds are absent (they are free to re-pair). `#[serde(default)]` keeps
    /// older callers and corpus cases parsing.
    #[serde(default)]
    pub published_pairings: HashMap<i32, Vec<(String, String)>>,
    /// Round number -> players sitting that round out entirely: paired into no
    /// game and given no bye, but *not* withdrawn — they still appear in
    /// standings and their played results still count. The host uses this to
    /// reserve players a playoff bracket owns, so the ordinary field keeps
    /// pairing around them; it is deliberately generic (a director excusing a
    /// player for one round is the same shape). `#[serde(default)]` keeps older
    /// callers and the frozen corpus cases parsing.
    #[serde(default)]
    pub inactive_players: HashMap<i32, Vec<String>>,
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
