//! A fixed pairing in a COP round holds, on a real position where it used not to.
//!
//! `data/cop_pins_albany_r16.json` is liwords' own "Albany after round 16" test
//! position (upstream's `pkg/pair/testutils`), converted to engine input by
//! `tools/cop-go-oracle/convert.py`: 30 players, round 17 of 27, with two games
//! already paired — Joey Krafchick vs David Postal, and Noah Kalus's bye. When a
//! pin was only a weight, the matching found breaking the first one cheaper
//! (Krafchick is leading, and the pinned game carries prohibitive weights of its
//! own), and the round went out without it. Upstream keeps it; so must we.

use scrabble_pairing::model::PairingInput;
use scrabble_pairing::pair;

const ALBANY_R16: &str = include_str!("data/cop_pins_albany_r16.json");

#[test]
fn albany_round_17_keeps_both_pinned_games() {
    let mut input: PairingInput = serde_json::from_str(ALBANY_R16).expect("parse test data");
    // Upstream's 1000 sims take minutes in a debug build; a pin must hold
    // whatever the sims say, and this count still broke it before the fix.
    let cfg = input.cop_config.as_mut().expect("COP config");
    cfg.simulations = 50;
    cfg.always_wins_simulations = 50;
    let out = pair(&input);
    let round = out.iter().find(|r| r.round == 17).expect("round 17 paired");
    assert!(round.error.is_none(), "round 17 errored: {:?}", round.error);

    let has = |a: &str, b: &str| {
        round.pairings.iter().any(|p| {
            (p.first == a && p.second == b) || (p.first == b && p.second == a)
        })
    };
    assert!(has("Joey Krafchick", "David Postal"), "pin broken: {:?}", round.pairings);
    assert!(has("Noah Kalus", "Bye"), "pinned bye broken: {:?}", round.pairings);

    // Everyone still plays exactly once (the withdrawn player not at all).
    let mut seen = std::collections::HashSet::new();
    for p in &round.pairings {
        for name in [&p.first, &p.second] {
            if name != "Bye" {
                assert!(seen.insert(name.clone()), "{name} paired twice");
            }
        }
    }
    let active = input.players.iter().filter(|p| !p.dropped).count();
    assert_eq!(seen.len(), active, "someone was left out of the round");
}
