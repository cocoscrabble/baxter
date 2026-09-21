//! Upstream's `PairRequest` (liwords `api/proto/ipc/pair.proto`), the shape the
//! ported core works on.
//!
//! Deserializes from the protobuf JSON the Go oracle reads, so the core can be
//! compared against upstream on the very same request. Baxter's own adapter
//! (`mod.rs`) builds one from a `PairingInput`.

use serde::{Deserialize, Deserializer};

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Request {
    pub player_names: Vec<String>,
    pub player_classes: Vec<i32>,
    #[serde(deserialize_with = "rounds_of_pairings")]
    pub division_pairings: Vec<Vec<i32>>,
    #[serde(deserialize_with = "rounds_of_results")]
    pub division_results: Vec<Vec<i32>>,
    pub class_prizes: Vec<i32>,
    pub gibson_spread: i32,
    pub control_loss_threshold: f64,
    pub hopefulness_threshold: f64,
    pub all_players: i32,
    pub valid_players: i32,
    pub rounds: i32,
    pub place_prizes: i32,
    pub division_sims: i32,
    pub control_loss_sims: i32,
    pub control_loss_activation_round: i32,
    pub allow_repeat_byes: bool,
    pub removed_players: Vec<i32>,
    #[serde(deserialize_with = "int64")]
    pub seed: i64,
    pub top_down_byes: bool,
    /// Baxter's cap on total division sims, standing in for upstream's 6s
    /// re-simulation clock (`plans/PLAN_COP_GO_PORT.md`, decision 3). Not in
    /// upstream's request; 0 means `DEFAULT_MAX_SIMS`.
    pub max_sims: i32,
}

/// The re-sim cap when a request does not set one.
pub const DEFAULT_MAX_SIMS: i32 = 1_000_000;

impl Request {
    pub fn max_sims(&self) -> usize {
        (if self.max_sims > 0 { self.max_sims } else { DEFAULT_MAX_SIMS }) as usize
    }

    /// The seed COPPair runs on: `seed`, or when that is 0 a 64-bit FNV-1 hash
    /// of the names back to back plus the number of rounds in
    /// `division_pairings` (Go's `fnv.New64`, which is FNV-1, not 1a).
    pub fn effective_seed(&self) -> u64 {
        if self.seed != 0 {
            return self.seed as u64;
        }
        let mut h: u64 = 0xCBF2_9CE4_8422_2325;
        for name in &self.player_names {
            for &byte in name.as_bytes() {
                h = h.wrapping_mul(0x0000_0100_0000_01B3);
                h ^= byte as u64;
            }
        }
        h.wrapping_add(self.division_pairings.len() as u64)
    }

    /// `pkgstnd.GetRoundsRemaining`.
    pub fn rounds_remaining(&self) -> i32 {
        self.rounds - self.division_results.len() as i32
    }
}

#[derive(Deserialize)]
struct PairingsRow {
    #[serde(default)]
    pairings: Vec<i32>,
}

#[derive(Deserialize)]
struct ResultsRow {
    #[serde(default)]
    results: Vec<i32>,
}

fn rounds_of_pairings<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Vec<i32>>, D::Error> {
    let rows: Vec<PairingsRow> = Deserialize::deserialize(d)?;
    Ok(rows.into_iter().map(|r| r.pairings).collect())
}

fn rounds_of_results<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Vec<i32>>, D::Error> {
    let rows: Vec<ResultsRow> = Deserialize::deserialize(d)?;
    Ok(rows.into_iter().map(|r| r.results).collect())
}

/// Protobuf JSON writes an int64 as a string; accept a number too.
fn int64<'de, D: Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Int64 {
        Str(String),
        Num(i64),
    }
    match Int64::deserialize(d)? {
        Int64::Num(n) => Ok(n),
        Int64::Str(s) => s.parse().map_err(serde::de::Error::custom),
    }
}
