//! COP, ported from liwords' Go implementation (`plans/PLAN_COP_GO_PORT.md`):
//! `github.com/woogles-io/liwords` `pkg/pair`, the pin in
//! `tools/cop-go-oracle/go.mod`.
//!
//! The submodules are upstream's code, ported line for line and checked against
//! it stage by stage (`tools/cop-go-oracle/compare.py --stages`,
//! `tests/cop_go_parity.rs`): they work on upstream's own `PairRequest`. This
//! file is the Baxter side — it turns the engine's view of a division into
//! that request and the answer back into pairings.
//!
//! Where Baxter's model and upstream's differ, the translation is:
//!
//! - **Player index** is Baxter's input order *reversed*. Upstream breaks an
//!   exact wins-and-spread tie toward the higher index, and Baxter sends
//!   entrants in seeding order, so reversing gives the tie to the better seed.
//! - **Removed players** are the withdrawn and those sitting this round out
//!   (`inactive_players`); upstream drops removed players from its standings
//!   while their past games still count for their opponents.
//! - **History**: every round before the one being paired, from the slips and
//!   the published boards. Results only up to `start_round` — upstream counts
//!   rounds remaining from the results. A bye scores its spread, a forfeit
//!   minus its spread; a round an active player has no result for (a late
//!   entrant, say) is a loss by one — upstream has no "did not play", and
//!   that is the nearest thing to Baxter's zero. Such a round is parked on the
//!   player themselves, so it also counts in upstream's bye tally, as a bye
//!   they have in effect already had.
//! - **Fixed pairings** become upstream's partly paired round: its prepaired
//!   games, placed as given.
//! - **Config** values are per rounds remaining, picked upstream's way; place
//!   prizes are clamped to the field so a small division is not refused; control
//!   loss applies in the last `CONTROL_LOSS_DEFAULT_ROUNDS_LEFT` rounds unless
//!   the division sets its own activation round.
//! - **Seed**: `PairingInput::seed + round - 1`, so rounds differ and replay is
//!   stable.

pub mod cephes;
pub mod cop;
pub mod factor3;
pub mod matching;
pub mod precomp;
pub mod rand;
pub mod request;
pub mod score_diffs;
pub mod standings;
pub mod trace;
pub mod verify;

use std::collections::{HashMap, HashSet};

use crate::model::CopConfig;
use crate::round_pairing::RoundPairing;
use crate::standings::{Pairings, Player, BYE_NAME};
use crate::strategies::Ctx;
use request::Request;

/// The re-simulation budget, in player-rounds, when the config sets no cap.
///
/// Upstream re-simulates until confident or for 6s; here that clock becomes a
/// count, so the answer does not depend on the machine. A simulation costs
/// about players × rounds remaining, so the cap is this budget divided by that:
/// the same work for every shape of division, and more sims where they are
/// cheap. 400M is ~1.1M sims for 30 players with 12 rounds left, about 2s on
/// the 4 sim threads — see `plans/PLAN_COP_GO_PORT.md`, stage 5.
pub const SIM_BUDGET: i64 = 400_000_000;

/// The cap `SIM_BUDGET` gives a division, never below its initial sim count.
pub fn default_max_sims(valid_players: i32, rounds_remaining: i32, division_sims: i32) -> i32 {
    let cost = (valid_players.max(1) as i64) * (rounds_remaining.max(1) as i64);
    ((SIM_BUDGET / cost).min(i32::MAX as i64) as i32).max(division_sims)
}

fn is_bye(name: &str) -> bool {
    name.eq_ignore_ascii_case(BYE_NAME)
}

/// Rounds left when control loss switches on, unless the config says otherwise.
///
/// Control loss from the first round is expensive and rarely fires — with more
/// than four rounds left it needs a perfect vs-1st record — and early on it can
/// collide with the forced contender bye (the leader's only allowed opponent
/// must also take the bye), which upstream fails as overconstrained. Where
/// upstream's own fixtures enable it at all, it is with 3–8 rounds left, most
/// often 4 — also the threshold its control-loss rules use internally.
pub const CONTROL_LOSS_DEFAULT_ROUNDS_LEFT: i32 = 4;

/// The value for this round from a per-rounds-remaining array (index 0 is the
/// final round; the last entry repeats).
fn per_round<T: Copy>(values: &[T], rounds_remaining: i32, default: T) -> T {
    let idx = (rounds_remaining - 1).max(0) as usize;
    values.get(idx).or(values.last()).copied().unwrap_or(default)
}

/// Build upstream's request for pairing `rp`. Returns it with the names, by
/// player index.
pub fn build_request(ctx: &Ctx, rp: &RoundPairing, cfg: &CopConfig) -> Result<(Request, Vec<String>), String> {
    let names: Vec<String> = ctx
        .players
        .iter()
        .rev()
        .filter(|p| !is_bye(&p.name))
        .map(|p| p.name.clone())
        .collect();
    let n = names.len();
    let index: HashMap<&str, usize> = names.iter().enumerate().map(|(i, s)| (s.as_str(), i)).collect();
    let removed: HashSet<usize> = ctx
        .players
        .iter()
        .filter(|p| p.dropped || ctx.excluded.contains(&p.name))
        .filter_map(|p| index.get(p.name.as_str()).copied())
        .collect();

    let total_rounds = ctx.round_pairings.iter().map(|r| r.round).max().unwrap_or(0);
    let (round, start) = (rp.round, rp.start_round);

    // Games per round, from results and published boards: (a, b), `None` = bye.
    let mut games: HashMap<i32, Vec<(usize, Option<usize>)>> = HashMap::new();
    let mut add = |k: i32, a: &str, b: &str| -> Result<(), String> {
        let lookup = |s: &str| {
            index.get(s).copied().ok_or_else(|| format!("COP: {s} is not in this division"))
        };
        let entry = games.entry(k).or_default();
        match (is_bye(a), is_bye(b)) {
            (true, true) => {}
            (false, true) => entry.push((lookup(a)?, None)),
            (true, false) => entry.push((lookup(b)?, None)),
            (false, false) => entry.push((lookup(a)?, Some(lookup(b)?))),
        }
        Ok(())
    };
    for s in ctx.slips {
        add(s.round, &s.winner_name, &s.loser_name)?;
    }
    for (&k, pairs) in ctx.published_pairings {
        if !ctx.slips.iter().any(|s| s.round == k) {
            for (a, b) in pairs {
                add(k, a, b)?;
            }
        }
    }

    // History runs through every round before this one that has a board,
    // contiguously from the standings round.
    let mut last = start;
    while last + 1 < round && games.contains_key(&(last + 1)) {
        last += 1;
    }
    let mut division_pairings = Vec::new();
    for k in 1..=last {
        let mut row = vec![-1i32; n];
        for &(a, b) in games.get(&k).map(Vec::as_slice).unwrap_or_default() {
            match b {
                None => row[a] = a as i32,
                Some(b) => {
                    row[a] = b as i32;
                    row[b] = a as i32;
                }
            }
        }
        // Everyone else sat the round out: parked on themselves, as upstream
        // parks a removed player.
        for (i, slot) in row.iter_mut().enumerate() {
            if *slot < 0 {
                *slot = i as i32;
            }
        }
        division_pairings.push(row);
    }

    let mut division_results = Vec::new();
    for k in 1..=start {
        let row_pairings = &division_pairings[(k - 1) as usize];
        let mut row: Vec<i32> = (0..n)
            .map(|i| if row_pairings[i] == i as i32 && !removed.contains(&i) { -1 } else { 0 })
            .collect();
        for s in ctx.slips.iter().filter(|s| s.round == k) {
            let spread = s.winner_score - s.loser_score;
            match (is_bye(&s.winner_name), is_bye(&s.loser_name)) {
                (false, true) => row[index[s.winner_name.as_str()]] = spread,
                (true, false) => row[index[s.loser_name.as_str()]] = -spread,
                (false, false) => {
                    row[index[s.winner_name.as_str()]] = s.winner_score;
                    row[index[s.loser_name.as_str()]] = s.loser_score;
                }
                (true, true) => {}
            }
        }
        division_results.push(row);
    }

    // This round's fixed pairings: upstream's partly paired round.
    if let Some(pins) = ctx.fixed_pairings.get(&round).filter(|p| !p.is_empty()) {
        let mut row = vec![-1i32; n];
        for (a, b) in pins {
            let find = |s: &str| -> Result<Option<usize>, String> {
                if is_bye(s) {
                    return Ok(None);
                }
                match index.get(s) {
                    Some(&i) if !removed.contains(&i) => Ok(Some(i)),
                    _ => Err(format!(
                        "COP: the fixed pairing {a} vs {b} cannot be honored — {s} is not in this round's field"
                    )),
                }
            };
            match (find(a)?, find(b)?) {
                (Some(x), None) | (None, Some(x)) => row[x] = x as i32,
                (Some(x), Some(y)) => {
                    row[x] = y as i32;
                    row[y] = x as i32;
                }
                (None, None) => {}
            }
        }
        division_pairings.push(row);
    }

    let valid = (n - removed.len()) as i32;
    let rounds_remaining = total_rounds - start;
    let mut removed: Vec<i32> = removed.into_iter().map(|i| i as i32).collect();
    removed.sort_unstable();
    let req = Request {
        player_names: names.clone(),
        player_classes: vec![0; n],
        division_pairings,
        division_results,
        class_prizes: Vec::new(),
        gibson_spread: per_round(&cfg.gibson_spreads, rounds_remaining, 250),
        control_loss_threshold: per_round(&cfg.control_loss_thresholds, rounds_remaining, 0.30),
        hopefulness_threshold: per_round(&cfg.hopefulness, rounds_remaining, 0.02),
        all_players: n as i32,
        valid_players: valid,
        rounds: total_rounds,
        place_prizes: cfg.place_prizes.min(valid).max(1),
        division_sims: cfg.simulations as i32,
        control_loss_sims: cfg.always_wins_simulations as i32,
        control_loss_activation_round: cfg
            .control_loss_activation_round
            .unwrap_or((total_rounds - CONTROL_LOSS_DEFAULT_ROUNDS_LEFT).max(0)),
        allow_repeat_byes: !cfg.disallow_repeat_byes,
        removed_players: removed,
        seed: ctx.seed.wrapping_add((round - 1) as u64) as i64,
        top_down_byes: cfg.top_down_byes,
        max_sims: if cfg.max_simulations > 0 {
            cfg.max_simulations as i32
        } else {
            default_max_sims(valid, rounds_remaining, cfg.simulations as i32)
        },
    };
    Ok((req, names))
}

/// Upstream's request for pairing `round` of `input`, as protobuf JSON — what
/// the Go oracle reads, so any Baxter COP round can be run through upstream
/// (the `cop_request` example). Baxter's own `maxSims` is dropped: the oracle
/// rejects fields it does not know.
pub fn request_json(input: &crate::model::PairingInput, round: i32) -> Result<String, String> {
    let rp = input
        .round_pairings
        .iter()
        .find(|r| r.round == round)
        .ok_or_else(|| format!("no round {round} in the schedule"))?;
    let cfg = input.cop_config.as_ref().ok_or("no cop_config")?;
    let inactive: HashSet<String> = input
        .inactive_players
        .get(&round)
        .map(|v| v.iter().cloned().collect())
        .unwrap_or_default();
    let mut rng = crate::rng::seeded(input.seed);
    let repeats = crate::standings::Repeats::default();
    let ctx = Ctx {
        players: &input.players,
        slips: &input.result_slips,
        round_pairings: &input.round_pairings,
        excluded: &inactive,
        fixed_pairings: &input.fixed_pairings,
        published_pairings: &input.published_pairings,
        repeats: &repeats,
        rng: &mut rng,
        seed: input.seed,
        cop_config: Some(cfg),
        swiss_config: &input.swiss_config,
    };
    let (req, _) = build_request(&ctx, rp, cfg)?;
    let mut value = serde_json::to_value(&req).map_err(|e| e.to_string())?;
    value.as_object_mut().unwrap().remove("maxSims");
    value.as_object_mut().unwrap().insert("pairMethod".into(), "COP".into());
    serde_json::to_string(&value).map_err(|e| e.to_string())
}

/// Pair one round with COP.
pub fn pair_cop(ctx: &mut Ctx, rp: &RoundPairing) -> Result<Pairings, String> {
    let cfg = ctx
        .cop_config
        .ok_or_else(|| "COP round configured but no cop_config supplied".to_string())?;
    let (req, names) = build_request(ctx, rp, cfg)?;
    let answer = cop::cop_pair(&req)
        .pairings
        .map_err(|e| format!("COP: {}", e.message.trim()))?;

    let standings = ctx.standings(rp.start_round);
    let by_name: HashMap<&str, &Player> = standings.iter().map(|p| (p.name.as_str(), p)).collect();
    let player = |name: &str| by_name.get(name).map_or_else(|| Player::new(name), |p| (*p).clone());
    let mut out = Pairings::new();
    for (i, &opp) in answer.iter().enumerate() {
        if opp < 0 || (opp as usize) < i {
            continue;
        }
        let opp = opp as usize;
        if opp == i {
            out.add(player(&names[i]), Player::bye());
        } else {
            out.add(player(&names[i]), player(&names[opp]));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use crate::model::PairingInput;
    use crate::pair;

    /// `n` players, `rounds` COP rounds, round-1 results as (winner, loser,
    /// winner score, loser score), and extra top-level JSON fields.
    fn input(n: usize, rounds: i32, results: &[(&str, &str, i32, i32)], extra: &str) -> PairingInput {
        let players: Vec<String> = (1..=n)
            .map(|i| format!(r#"{{"name":"P{i}","rating":{}}}"#, 2000 - 10 * i))
            .collect();
        let schedule: Vec<String> = (1..=rounds)
            .map(|r| format!(r#"{{"round":{r},"start_round":{},"pairing":"COP"}}"#, r - 1))
            .collect();
        let slips: Vec<String> = results
            .iter()
            .map(|(w, l, ws, ls)| {
                format!(
                    r#"{{"round":1,"winner_name":"{w}","loser_name":"{l}","winner_score":{ws},"loser_score":{ls},"winner_started":true}}"#
                )
            })
            .collect();
        let json = format!(
            r#"{{"players":[{}],"round_pairings":[{}],"result_slips":[{}],
                "cop_config":{{"place_prizes":2,"gibson_spreads":[250],"hopefulness":[0.02],
                "control_loss_thresholds":[0.3],"simulations":300,"always_wins_simulations":100,
                "disallow_repeat_byes":true}}{extra}}}"#,
            players.join(","),
            schedule.join(","),
            slips.join(",")
        );
        serde_json::from_str(&json).expect("test input")
    }

    const R1: [(&str, &str, i32, i32); 3] =
        [("P1", "P2", 450, 400), ("P3", "P4", 420, 380), ("P5", "P6", 500, 300)];

    /// Round `round`'s games as sorted name pairs; fails on an engine error.
    fn games(input: &PairingInput, round: i32) -> Vec<(String, String)> {
        let out = pair(input);
        let r = out.iter().find(|r| r.round == round).expect("round paired");
        assert!(r.error.is_none(), "round {round} errored: {:?}", r.error);
        let mut games: Vec<(String, String)> = r
            .pairings
            .iter()
            .map(|p| {
                let mut g = [p.first.clone(), p.second.clone()];
                g.sort();
                (g[0].clone(), g[1].clone())
            })
            .collect();
        games.sort();
        games
    }

    fn error(input: &PairingInput, round: i32) -> String {
        let out = pair(input);
        out.into_iter().find(|r| r.round == round).and_then(|r| r.error).unwrap_or_default()
    }

    /// Everyone named once, the bye aside.
    fn paired_once(games: &[(String, String)]) -> HashSet<String> {
        let mut seen = HashSet::new();
        for (a, b) in games {
            for name in [a, b] {
                if name != "Bye" {
                    assert!(seen.insert(name.clone()), "{name} paired twice: {games:?}");
                }
            }
        }
        seen
    }

    #[test]
    fn everyone_is_paired_once() {
        let g = games(&input(6, 5, &R1, ""), 2);
        assert_eq!(paired_once(&g).len(), 6);
        assert_eq!(g.len(), 3);
    }

    #[test]
    fn an_odd_field_gets_exactly_one_bye() {
        let results = [("P1", "P2", 450, 400), ("P3", "P4", 420, 380), ("P5", "Bye", 50, 0)];
        let g = games(&input(5, 5, &results, ""), 2);
        assert_eq!(g.iter().filter(|(a, b)| a == "Bye" || b == "Bye").count(), 1, "{g:?}");
        assert_eq!(paired_once(&g).len(), 5);
    }

    #[test]
    fn the_withdrawn_and_the_inactive_sit_out() {
        let mut inp = input(6, 5, &R1, r#","inactive_players":{"2":["P3"]}"#);
        inp.players[1].dropped = true;
        let g = games(&inp, 2);
        let seen = paired_once(&g);
        assert!(!seen.contains("P2") && !seen.contains("P3"), "{g:?}");
        assert_eq!(seen.len(), 4);
    }

    #[test]
    fn pins_hold_including_several_byes() {
        let mut inp = input(6, 5, &R1, r#","fixed_pairings":{"2":[["P1","P6"],["P2","Bye"],["P4","Bye"]]}"#);
        // Upstream's control loss can bar the leader from the only opponent the
        // pins leave them (it does not look at prepaired games), which fails
        // the round as overconstrained; keep it out of a test about pins.
        inp.cop_config.as_mut().unwrap().control_loss_activation_round = Some(99);
        let g = games(&inp, 2);
        for pin in [("P1", "P6"), ("Bye", "P2"), ("Bye", "P4")] {
            assert!(g.contains(&(pin.0.to_string(), pin.1.to_string())), "{pin:?} broken: {g:?}");
        }
        assert_eq!(paired_once(&g).len(), 6);
    }

    #[test]
    fn a_first_round_pin_holds() {
        let g = games(&input(6, 5, &[], r#","fixed_pairings":{"1":[["P1","P2"]]}"#), 1);
        assert!(g.contains(&("P1".to_string(), "P2".to_string())), "{g:?}");
        assert_eq!(paired_once(&g).len(), 6);
    }

    #[test]
    fn a_pin_naming_someone_absent_is_an_error() {
        let mut inp = input(6, 5, &R1, r#","fixed_pairings":{"2":[["P1","P2"]]}"#);
        inp.players[1].dropped = true;
        assert!(error(&inp, 2).contains("P2 is not in this round's field"), "{}", error(&inp, 2));
    }

    #[test]
    fn a_player_who_sat_a_round_out_is_paired_after_it() {
        // P7 sat round 1 out, so has no result for it: a loss by one to COP.
        let mut inp = input(7, 5, &R1, r#","inactive_players":{"1":["P7"]}"#);
        // In a field this small upstream's control loss (leader must play P6)
        // and forced contender bye (P6 must take the bye) collide, and upstream
        // fails the round as overconstrained — checked against the oracle with
        // the cop_request example. Not what this test is about.
        inp.cop_config.as_mut().unwrap().control_loss_activation_round = Some(99);
        let g = games(&inp, 2);
        let seen = paired_once(&g);
        assert!(seen.contains("P7"), "{g:?}");
        assert_eq!(seen.len(), 7);
    }

    #[test]
    fn more_place_prizes_than_players_still_pairs() {
        let mut inp = input(4, 3, &R1[..2], "");
        inp.cop_config.as_mut().unwrap().place_prizes = 8;
        assert_eq!(paired_once(&games(&inp, 2)).len(), 4);
    }

    fn activation_round(inp: &PairingInput, round: i32) -> i64 {
        let req: serde_json::Value =
            serde_json::from_str(&super::request_json(inp, round).unwrap()).unwrap();
        req["controlLossActivationRound"].as_i64().unwrap()
    }

    #[test]
    fn control_loss_defaults_to_the_last_four_rounds() {
        let mut inp = input(6, 12, &R1, "");
        assert_eq!(activation_round(&inp, 2), 8);
        inp.cop_config.as_mut().unwrap().control_loss_activation_round = Some(0);
        assert_eq!(activation_round(&inp, 2), 0);
        // A schedule shorter than the default window starts it at once.
        assert_eq!(activation_round(&input(6, 3, &R1, ""), 2), 0);
    }

    #[test]
    fn the_same_input_pairs_the_same_way() {
        let inp = input(6, 5, &R1, r#","seed":42"#);
        assert_eq!(games(&inp, 2), games(&inp, 2));
    }
}
