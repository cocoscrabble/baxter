//! Upstream's `pkg/pair/verifyreq`: reject a malformed request before any
//! simulation, with the same error codes (`pb.PairError`).

use std::collections::HashSet;

use serde::Serialize;

use super::request::Request;

pub const MAX_PLAYER_COUNT: i32 = 50000;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PairError {
    /// Upstream's `pb.PairError` name, e.g. `ALL_ROUNDS_PAIRED`.
    pub code: &'static str,
    pub message: String,
}

fn err(code: &'static str, message: String) -> Result<(), PairError> {
    Err(PairError { code, message })
}

/// `verifyreq.Verify`, for a COP request.
pub fn verify(req: &Request) -> Result<(), PairError> {
    if req.valid_players < 2 {
        return err("PLAYER_COUNT_INSUFFICIENT", format!("not enough players ({})", req.valid_players));
    }
    if req.all_players > MAX_PLAYER_COUNT {
        return err("PLAYER_COUNT_TOO_LARGE", format!("too many players ({})", req.all_players));
    }
    if req.rounds < 1 {
        return err("ROUND_COUNT_INSUFFICIENT", format!("not enough rounds ({})", req.rounds));
    }
    if req.player_names.len() != req.all_players as usize {
        return err(
            "PLAYER_NAME_COUNT_INSUFFICIENT",
            format!(
                "player name count ({}) does not match number of players ({})",
                req.player_names.len(), req.all_players
            ),
        );
    }
    if let Some(i) = req.player_names.iter().position(String::is_empty) {
        return err("PLAYER_NAME_EMPTY", format!("player name is empty for player {}", i + 1));
    }
    if req.player_classes.len() != req.all_players as usize {
        return err(
            "INVALID_PLAYER_CLASS_COUNT",
            format!(
                "player class count ({}) does not match number of players ({})",
                req.player_classes.len(), req.all_players
            ),
        );
    }
    for (i, &c) in req.player_classes.iter().enumerate() {
        if c < 0 || c > req.class_prizes.len() as i32 {
            return err("INVALID_PLAYER_CLASS", format!("player class invalid for player {}: {c}", i + 1));
        }
    }
    for &prize in &req.class_prizes {
        if prize < 1 {
            return err("INVALID_CLASS_PRIZE", format!("invalid class prize {prize}"));
        }
    }

    let pairings_len = req.division_pairings.len();
    if pairings_len > req.rounds as usize {
        return err(
            "MORE_PAIRINGS_THAN_ROUNDS",
            format!("more pairings ({pairings_len}) than rounds ({})", req.rounds),
        );
    }
    let mut last_round_partially_paired = false;
    for (round_idx, round) in req.division_pairings.iter().enumerate() {
        if round.len() != req.all_players as usize {
            return err(
                "INVALID_ROUND_PAIRINGS_COUNT",
                format!(
                    "round pairings length ({}) for round {} does not match number of players ({})",
                    round.len(), round_idx + 1, req.all_players
                ),
            );
        }
        let mut seen: HashSet<usize> = HashSet::new();
        for (p, &opp) in round.iter().enumerate() {
            if opp < -1 || opp >= req.all_players {
                return err(
                    "PLAYER_INDEX_OUT_OF_BOUNDS",
                    format!("opponent ({}) for player {} in round {} is out of bounds", opp + 1, p + 1, round_idx + 1),
                );
            }
            if opp < 0 {
                if round_idx != pairings_len - 1 {
                    return err("UNPAIRED_PLAYER", format!("player ({p}) not paired in round {}", round_idx + 1));
                }
                last_round_partially_paired = true;
                continue;
            }
            let o = opp as usize;
            if p == o && !seen.insert(p) {
                return err("INVALID_PAIRING", format!("player {} is paired but also has a bye", p + 1));
            }
            if p < o {
                if seen.contains(&p) || seen.contains(&o) {
                    return err(
                        "INVALID_PAIRING",
                        format!("one of the players {} and {} is paired multiple times", p + 1, o + 1),
                    );
                }
                if round[p] != opp || round[o] != p as i32 {
                    return err(
                        "INVALID_PAIRING",
                        format!("opponents for players {} and {} are not the players themselves", p + 1, o + 1),
                    );
                }
                seen.insert(p);
                seen.insert(o);
            }
        }
    }
    let complete = pairings_len - usize::from(last_round_partially_paired);
    if complete == req.rounds as usize {
        return err(
            "ALL_ROUNDS_PAIRED",
            format!("equal pairings ({complete}) and rounds ({})", req.rounds),
        );
    }

    let results_len = req.division_results.len();
    if results_len > req.rounds as usize {
        return err("MORE_RESULTS_THAN_ROUNDS", format!("more results ({results_len}) than rounds ({})", req.rounds));
    }
    if results_len > pairings_len {
        return err(
            "MORE_RESULTS_THAN_PAIRINGS",
            format!("more results ({results_len}) than pairings ({pairings_len})"),
        );
    }
    for (round_idx, results) in req.division_results.iter().enumerate() {
        if results.len() != req.all_players as usize {
            return err(
                "INVALID_ROUND_RESULTS_COUNT",
                format!(
                    "round results length ({}) for round {} does not match number of players ({})",
                    results.len(), round_idx + 1, req.all_players
                ),
            );
        }
    }

    if req.control_loss_activation_round < 0 {
        return err(
            "INVALID_CONTROL_LOSS_ACTIVATION_ROUND",
            format!("invalid control loss activation round {} (must be nonnegative)", req.control_loss_activation_round),
        );
    }
    if req.gibson_spread < 0 {
        return err("INVALID_GIBSON_SPREAD", format!("invalid gibson spread {}", req.gibson_spread));
    }
    if !(0.0..=1.0).contains(&req.control_loss_threshold) {
        return err(
            "INVALID_CONTROL_LOSS_THRESHOLD",
            format!("invalid control loss threshold {:.6}", req.control_loss_threshold),
        );
    }
    if req.hopefulness_threshold <= 0.0 || req.hopefulness_threshold > 1.0 {
        return err(
            "INVALID_HOPEFULNESS_THRESHOLD",
            format!("invalid hopefulness threshold {:.6}", req.hopefulness_threshold),
        );
    }
    if req.division_sims < 1 {
        return err("INVALID_DIVISION_SIMS", format!("invalid division sims {}", req.division_sims));
    }
    if req.control_loss_sims < 1 {
        return err("INVALID_CONTROL_LOSS_SIMS", format!("invalid control loss sims {}", req.control_loss_sims));
    }
    if req.place_prizes > req.valid_players || req.place_prizes < 1 {
        return err("INVALID_PLACE_PRIZES", format!("invalid place prizes {}", req.place_prizes));
    }

    for &r in &req.removed_players {
        if r < 0 || r >= req.all_players {
            return err("INVALID_REMOVED_PLAYER", format!("invalid removed player {r}"));
        }
    }
    if (req.all_players - req.valid_players) as usize != req.removed_players.len() {
        return err(
            "INVALID_VALID_PLAYER_COUNT",
            format!(
                "total players {} minus removed players {} does not equal valid players {}",
                req.all_players, req.removed_players.len(), req.valid_players
            ),
        );
    }
    Ok(())
}
