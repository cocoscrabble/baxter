//! Per-round pairing configuration

use serde::Deserialize;

/// A pairing strategy. The serialized form is the variant name as a string
/// (with the `Quads_*` renames below).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize)]
pub enum RP {
    KotH,
    QotH,
    Swiss,
    SwissNoRepeats,
    SwissMinRepeats,
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
    #[serde(rename = "COP")]
    Cop,
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

/// Repair legacy per-round sources while preserving explicit RR block starts.
/// A schedule-editor block begins with `start_round == round`, including when
/// two blocks use the same strategy consecutively. Legacy `round - 1` sources
/// still normalize to one rotation.
pub fn normalize_round_robin_start_rounds(rps: &mut [RoundPairing]) {
    let mut i = 0;
    while i < rps.len() {
        if !rps[i].pairing.is_round_robin() {
            i += 1;
            continue;
        }
        let block_pairing = rps[i].pairing;
        let block_start = rps[i].round;
        rps[i].start_round = block_start;
        i += 1;
        while i < rps.len()
            && rps[i].pairing == block_pairing
            && rps[i].round == rps[i - 1].round + 1
            && rps[i].start_round != rps[i].round
        {
            rps[i].start_round = block_start;
            i += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_adjacent_explicit_rr_blocks() {
        for pairing in [RP::RoundRobin, RP::DoubleRoundRobin, RP::Charlottesville] {
            let mut rps: Vec<RoundPairing> = (1..=6)
                .map(|round| RoundPairing {
                    round,
                    start_round: if round <= 3 { 1 } else { 4 },
                    pairing,
                })
                .collect();
            normalize_round_robin_start_rounds(&mut rps);
            assert_eq!(
                rps.iter().map(|r| r.start_round).collect::<Vec<_>>(),
                vec![1, 1, 1, 4, 4, 4]
            );
        }
    }

    #[test]
    fn deserializes_strategy_strings() {
        let rp: RoundPairing =
            serde_json::from_str(r#"{"round":3,"start_round":2,"pairing":"Swiss"}"#).unwrap();
        assert_eq!(rp.pairing, RP::Swiss);

        let no_repeats: RoundPairing =
            serde_json::from_str(r#"{"round":4,"start_round":3,"pairing":"SwissNoRepeats"}"#)
                .unwrap();
        assert_eq!(no_repeats.pairing, RP::SwissNoRepeats);

        let minimal: RoundPairing =
            serde_json::from_str(r#"{"round":5,"start_round":4,"pairing":"SwissMinRepeats"}"#)
                .unwrap();
        assert_eq!(minimal.pairing, RP::SwissMinRepeats);

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
