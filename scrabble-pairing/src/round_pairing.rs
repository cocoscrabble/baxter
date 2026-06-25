//! Per-round pairing configuration

use serde::Deserialize;

/// A pairing strategy. The serialized form is the variant name as a string
/// (with the `Quads_*` renames below).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize)]
pub enum RP {
    KotH,
    QotH,
    Swiss,
    RoundRobin,
    DoubleRoundRobin,
    Random,
    RandomNoRepeats,
    #[serde(rename = "Quads_Clustered")]
    QuadsClustered,
    #[serde(rename = "Quads_Distributed")]
    QuadsDistributed,
    #[serde(rename = "Quads_Equalized")]
    QuadsEqualized,
    Sixes,
    Charlottesville,
    SwissPlusRandom,
    /// Any unrecognized strategy string. The engine pairs nobody for it.
    #[serde(other)]
    Unknown,
}

impl RP {
    pub fn is_round_robin(self) -> bool {
        matches!(
            self,
            RP::RoundRobin | RP::DoubleRoundRobin | RP::Charlottesville
        )
    }

    pub fn is_quad(self) -> bool {
        matches!(
            self,
            RP::QuadsClustered | RP::QuadsDistributed | RP::QuadsEqualized | RP::Sixes
        )
    }
}

/// One round's pairing configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct RoundPairing {
    pub round: i32,
    pub start_round: i32,
    pub pairing: RP,
}

/// Make each contiguous round-robin block share its first round as `start_round`.
///
/// A round-robin schedule rotates off a single fixed ordering (the standings as
/// of `start_round`), so every round in the block must point at the same one.
/// Normalise inputs where we have a run of round-robin rounds not following this convention.
pub fn normalize_round_robin_start_rounds(rps: &mut [RoundPairing]) {
    let mut i = 0;
    while i < rps.len() {
        if rps[i].pairing.is_round_robin() {
            let block_pairing = rps[i].pairing;
            let block_start = rps[i].round;
            while i < rps.len() && rps[i].pairing == block_pairing {
                rps[i].start_round = block_start;
                i += 1;
            }
        } else {
            i += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deserializes_strategy_strings() {
        let rp: RoundPairing =
            serde_json::from_str(r#"{"round":3,"start_round":2,"pairing":"Swiss"}"#).unwrap();
        assert_eq!(rp.pairing, RP::Swiss);

        let q: RoundPairing =
            serde_json::from_str(r#"{"round":1,"start_round":0,"pairing":"Quads_Clustered"}"#)
                .unwrap();
        assert_eq!(q.pairing, RP::QuadsClustered);

        let u: RoundPairing =
            serde_json::from_str(r#"{"round":1,"start_round":0,"pairing":"Nonsense"}"#).unwrap();
        assert_eq!(u.pairing, RP::Unknown);
    }

    #[test]
    fn normalizes_rr_block_start_rounds() {
        let mut rps: Vec<RoundPairing> = serde_json::from_str(
            r#"[
                {"round":1,"start_round":0,"pairing":"RoundRobin"},
                {"round":2,"start_round":1,"pairing":"RoundRobin"},
                {"round":3,"start_round":2,"pairing":"RoundRobin"},
                {"round":4,"start_round":3,"pairing":"Swiss"}
            ]"#,
        )
        .unwrap();
        normalize_round_robin_start_rounds(&mut rps);
        // The RR block (rounds 1-3) all point at round 1; the Swiss round is left.
        assert_eq!(rps[0].start_round, 1);
        assert_eq!(rps[1].start_round, 1);
        assert_eq!(rps[2].start_round, 1);
        assert_eq!(rps[3].start_round, 3);
    }
}
