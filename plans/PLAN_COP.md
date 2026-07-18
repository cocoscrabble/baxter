# PLAN_COP.md — Port the COP pairing algorithm to the Rust engine

**Status:** Phases 1–3 implemented (Rust engine complete + reachable from Django:
strategy registered, `DivisionSettings.cop_config` + migration, adapter plumbing).
Phases 4–5 pending (settings UI, class prizes). Pinned to commit `fd875c0`.

## Goal

Add **COP** (Cost-Optimized Pairing) as a selectable pairing strategy. COP is
Matthew O'Connor's tournament pairing algorithm used by `tsh`/Woogles: it runs
Monte Carlo simulations of the remaining rounds to identify, for each prize
rank, the set of players who can still realistically finish there ("contenders"),
then builds a weighted graph over all possible pairings and solves a
**minimum-weight perfect matching** so that contenders are paired within their
contention group, leaders aren't burned prematurely, repeats/byes are avoided,
and "destiny control" (the leader must play someone who can still catch them) is
enforced.

Source of truth for the port: `COP.pm` (Perl) from
`github.com/jvc56/tournament_pairing_algorithms` (MIT, license-compatible). We
port the **native** code path (the `cop()` sub and its helpers), not the Woogles
API path.

**A parity oracle is set up** (`tools/cop-oracle/`): the real, unmodified
`COP.pm` runs from the vendored `vendor/tsh` and is driven directly by a Perl
harness (`oracle.pl`) that takes a JSON case and emits COP's pairings + key
decisions. `vendor/` is gitignored — the oracle is a local-only dev aid for
hardening the Rust code on this machine; only `tools/cop-oracle/` (harness,
stubs, README) is tracked. See "Parity oracle" below and
`tools/cop-oracle/README.md`.

Rationale for the algorithm:
`medium.com/@matthewoconnor313/why-i-wrote-a-tournament-pairing-algorithm`.

## Why this is different from every existing strategy

Every current strategy (`swiss`, `basic`, `quads`, `roundrobin`) pairs a round
purely from standings + repeats + RNG, with data that already lives in `Ctx`.
COP needs substantially more, and this drives the whole design:

1. **It simulates the rest of the tournament.** COP's contender identification
   is a Monte Carlo over the *remaining* rounds. It needs the **total number of
   rounds** and a seeded RNG, and it is materially more expensive than any
   existing strategy (O(sims × rounds × players), run two-to-three times).
2. **It needs prize + tuning configuration** that Baxter does not model today:
   number of place prizes, gibson spread, "hopefulness" threshold, control-loss
   thresholds + activation round, and simulation counts. This is the "modify
   tournament settings" work the task calls out.
3. **It owns bye assignment.** COP decides who byes via the weight graph
   (gibsonized players take the bye; repeat byes are penalized), so it must see
   the *whole* field — the engine's pre-strategy bye injection must be bypassed
   for COP, exactly as it already is for the round-robin family
   (`pair.rs::pair_round`, the `RoundRobin | DoubleRoundRobin | Charlottesville`
   branch at `fd875c0`).
4. **It honors already-made pairings** via its own `prepaired_players`
   (prohibitive weight), which is where Baxter's `fixed_pairings` map for the
   round plugs in.

## What COP computes (faithful summary of `cop()` in COP.pm)

Inputs (after Baxter → engine translation):

- `tournament_players`: `{id, name, class, index, wins, spread}`, where **wins
  are doubled** (a win = 2, a draw = 1, a loss = 0) so everything stays integer.
- `times_played[pair]`: how many times two players have met; byes are keyed
  against the synthetic bye player (id 0).
- `previous_pairing[pair]`: whether the two met **last round** (drives
  back-to-back-repeat avoidance and destiny control).
- config (see "Settings" below): `number_of_rounds`, `round_to_pair` (0-idx),
  `number_of_rounds_remaining`, `lowest_ranked_payout` (0-idx place-prize count),
  per-class lowest payouts, `gibson_spreads[]` + `cumulative_gibson_spreads[]`,
  `hopefulness[]`, `control_loss_thresholds[]`, `control_loss_activation_round`,
  `number_of_sims`, `always_wins_number_of_sims`, `disallow_repeat_byes`,
  `prepaired_players`, `top_class`.

Pipeline:

1. **Odd field** → append a synthetic BYE player (id 0, sorts last).
2. **Sort by record**: byes last, then wins desc, spread desc, original index
   asc (`sort_tournament_players_by_record`).
3. **Truncate to sim players** (`get_sim_tournament_players`): keep everyone who
   can *technically* still cash (reach `lowest_ranked_payout` given rounds
   remaining), plus odd-index padding; stop at the first who can't. Pure
   performance + contention scoping.
4. **Gibson rank** (`get_lowest_gibson_rank`): the lowest rank locked into its
   placement — win gap to the next player `> rounds_remaining`, or `==` with
   spread gap beyond `cumulative_gibson_spreads[remaining-1]`.
5. **Two-pass factor-pair simulation** (`sim_factor_pair`):
   - `factor_pair` pairs rank *i* vs rank *i+nrl* (KOTH-style with a factor
     width `nrl` = rounds remaining, capped); gibsonized players are paired to
     the bottom.
   - `play_round` decides each game by a random spread in
     `[-max_spread, +max_spread]` (`max_spread = gibson_spreads[remaining-1]`),
     updates wins/spread, re-sorts; repeat for all remaining rounds; record the
     final rank each player lands in; reset; repeat `number_of_sims` times →
     `results[player][place]` counts.
   - Run once with `INITIAL_FACTOR` (huge), derive `improved_factor_constant`
     from the results, re-run with it.
6. **Contenders** (`get_lowest_ranked_players_who_can_finish_in_nth`): for each
   final rank *N*, the lowest-ranked current player who finished at rank ≤ *N*
   in `> hopefulness[remaining-1]` fraction of sims ("statistically") and in
   `≥ 1` sim ("absolutely"). `lowest_ranked_player_who_can_cash` = the
   finisher-in-nth for `lowest_ranked_payout`.
7. **Control loss** (`get_control_loss`, only when no one is gibsonized): via
   `sim_player_always_wins`, for each catchable player, how often they reach
   first if they always win (pair-with-first vs factor-pair). Yields
   `lowest_ranked_always_wins` and a `control_loss` fraction, and identifies
   **destiny's child** — the specific opponent first place must be pinned to.
8. **Class prizes** (`get_class_prize_pairings`): last-round KOTH pairings inside
   a class when no one in that class can cash. **Deferred to Phase 5** (Baxter
   has no in-division class concept yet — see "Open questions").
9. **Weight graph** over every pair (the big loop, COP.pm ~1331–1596): sum of
   `repeat_weight` (`⌊2·times_played·(n/3)³⌋` + extra for back-to-back / repeat
   byes), `rank_difference_weight` (`(j−i)³`, or `(j−i)` when neither can cash /
   *i* is gibsonized), `pair_with_placer_weight` (contender logic; PROHIBITIVE
   when *j* can't catch *i*), `control_loss_weight`, `gibson_weight`,
   `koth_weight` (last round), `prepaired_weight`. `PROHIBITIVE_WEIGHT = 1e6`.
10. **Min-weight matching** (`min_weight_matching`): invert weights
    (`(max_weight+1) − w`) and run **max-weight, max-cardinality** matching, then
    map back to real players; emit a warning for any pairing that exceeded the
    prohibitive weight.

Output: an unordered set of pairings (one per player). Start assignment (who
goes first) is **not** COP's job — the engine's existing `Starts` pass in
`pair.rs::pair` handles it after the strategy returns.

## Design decisions

**D1 — COP is a normal `RP` strategy, dispatched by `pair.rs`, but in the
"handle byes internally" branch.** Add `RP::Cop` (serde `rename = "COP"`).
`pair_round` routes `RP::Cop` down the same path as the round-robin family: full
field, no pre-injected bye, no `excluded` set. COP does its own bye and reads
`ctx.fixed_pairings[round]` as prepaired constraints. Its synthetic bye maps to
the engine's `Player::bye()` / `BYE_NAME` on output.

**D2 — Config travels on `PairingInput`, not `RoundPairing`.** COP's tuning is
per **division**, not per round. Add an optional `cop_config: Option<CopConfig>`
to `PairingInput` (`model.rs`) and thread it into `Ctx`. `total_rounds` is
derived from `ctx.round_pairings` (max `round`), so no new per-round field.
`number_of_rounds_remaining = total_rounds − rp.start_round` (the round being
paired plus all later rounds; verified against COP's gibson math).

**D3 — Seeded, single-threaded, deterministic.** COP.pm uses unseeded `rand()`
and OS threads. We draw every simulation decision from `ctx.rng` (the seeded
`ChaCha8Rng`) and run single-threaded. Output will not match TSH bit-for-bit
(nor should it), but it is fully reproducible — required by replay/fuzz/what-if.
Where the max-weight matching admits ties, add the same splitmix64 per-edge
tiebreak `swiss.rs` uses (`match_tiebreak`) so the chosen matching is unique and
stable.

**D4 — Scalar settings, expanded to per-round arrays in the adapter.** COP's
`gibson_spread`, `hopefulness`, `control_loss_thresholds` are arrays indexed by
rounds-remaining, but COP already forward-fills the last element
(`extend_tsh_config_array`). Baxter stores a **single value** for each (plus
`control_loss_activation_round`, `simulations`, `always_wins_simulations`,
`place_prizes`, `disallow_repeat_byes`); the Python adapter wraps each scalar in
a 1-element list and lets the Rust side forward-fill. Advanced per-round arrays
can come later without a schema change (JSONField).

**D5 — Round 1 / `start_round < 1` falls back to Swiss-initial.** COP is
meaningless with no results and errors in TSH when rounds-remaining math breaks
down. If a COP round has `start_round < 1`, pair `pair_swiss_initial` (top vs
bottom half) instead of simulating. Document this in the settings UI.

**D6 — Fail loudly via the existing error channel.** Invalid conditions
(`rounds_remaining <= 0`, missing `cop_config`, `place_prizes` unset) return
`Err(String)` from the strategy; `pair.rs` already turns that into an empty
round carrying the message, which `engine.py::_rounds_to_display` raises as
`PairingError` and the atomic regenerate rolls back. No partial pairings.

**D7 — Class prizes deferred.** The core port ships place-prizes only (the
common case). `get_class_prize_pairings` and the `class`/`top_class` weight
terms are Phase 5, gated on Baxter modeling a within-division class.

**D8 — Parity is layered against the real COP.pm, not bit-for-bit pairings.**
The oracle (`tools/cop-oracle/`) runs the unmodified `COP.pm`. Because the Rust
port uses a different RNG (ChaCha8 vs Perl `rand`) and a different matching
solver (rustworkx-core vs Perl `Graph::Matching`), identical final pairings are
the *wrong* bar. Parity is asserted in three tiers:
- **Tier 1 (exact)** — RNG-independent logic: `get_lowest_gibson_rank`,
  `get_sim_tournament_players` (which players survive the can-cash truncation,
  and their order), `factor_pair` output for a given `nrl`, and the integer
  **weight matrix** given *injected* contender/gibson/control-loss values. This
  is the bulk of COP's decision logic and where the Rust port must match to the
  integer. Requires the Rust `pair_cop` to factor weight construction into a
  function taking those decision values as inputs (dependency injection), and the
  oracle to expose the weight table (planned harness extension — parse COP's
  logged weight table, which has a fixed column format).
- **Tier 2 (statistical)** — the simulation layer: at large sim counts, the
  contender arrays / gibson rank / control-loss decision / destiny's child must
  agree on constructed cases whose boundaries are unambiguous.
- **Tier 3 (matching)** — final pairings: exact only where the weight graph has a
  strictly unique optimum (well-separated weights, or the shared splitmix64
  tiebreak from D3); otherwise assert equal *total* weight, not identical edges.

## Parity oracle (`tools/cop-oracle/`)

Already stood up and verified on this machine:

- `vendor/tsh` holds tsh; the unmodified `COP.pm` is installed at
  `vendor/tsh/lib/perl/TSH/Command/COP.pm` per its README.
- `Graph::Matching` (COP's native matching dep, pure Perl + `Carp::Assert`) is
  installed into a vendored local-lib at `vendor/perl5` via
  `cpanm --local-lib=vendor/perl5 --notest Graph::Matching`.
- `tools/cop-oracle/stubs/` provides empty `TSH::PairingCommand` and
  `TSH::Command::ShowPairings` packages that shadow the vendored (Perl-5.42-
  incompatible) real ones, so the unmodified `COP.pm` loads. COP's `cop()` never
  uses those modules; only its unused `Run()` does.
- `tools/cop-oracle/oracle.pl` reads a JSON case, calls `cop()` directly
  single-threaded with `srand(seed)` (reproducible), and emits
  `{pairings, warnings, gibson_rank, sim_player_ids}`. `example_case.json` is a
  runnable sample. Run:
  ```
  perl -Itools/cop-oracle/stubs -Ivendor/tsh/lib/perl -Ivendor/perl5/lib/perl5 \
       tools/cop-oracle/oracle.pl < case.json
  ```
  Verified: deterministic run-to-run; a gibsonized leader is detected
  (`gibson_rank:0`) and paired to the bottom with KOTH behind.

Harness work still to do (during Phases 1–2): parse COP's logged weight table so
the oracle emits the Tier-1 weight matrix and the contender/control-loss
intermediates; add a thin Rust-side test helper that shells out to `oracle.pl`
and diffs against the port. Wins in the oracle input are **doubled** (win=2,
draw=1), matching COP's internal representation and the Rust port's input.

## Rust port structure

New file `scrabble-pairing/src/strategies/cop.rs`. Suggested internal shape,
mirroring COP.pm so the port is auditable side-by-side:

```
pub struct CopConfig { /* see model.rs below */ }

struct SimPlayer { id, name, class, index, wins, spread, is_bye,
                   start_wins, start_spread }   // wins are DOUBLED

pub fn pair_cop(ctx, rp, cfg: &CopConfig, total_rounds) -> Result<Pairings,String>

// helpers, 1:1 with the Perl:
fn sort_by_record / sort_by_index
fn get_sim_players
fn lowest_gibson_rank
fn factor_pair / factor_pair_minus_player
fn play_round(rng, ...)              // random spread from ctx.rng
fn sim_factor_pair(rng, ...)         // -> TournamentResults matrix
fn lowest_finishers_in_nth          // statistical + absolute
fn control_loss / sim_player_always_wins
fn build_weight_edges(...)           // the big weight loop
fn solve_min_weight_matching(...)    // invert -> max_weight_matching_pairs
```

Reuse what exists:
- **Matching**: `matching::max_weight_matching_pairs` already does max-weight +
  max-cardinality with `i128` weights. Feed it inverted COP weights. (Weight
  magnitudes: `PROHIBITIVE ≈ 1e6`, `(j−i)³` and `repeat ≈ 2·reps·(n/3)³` fit
  comfortably in `i128` for realistic fields.)
- **Standings**: get the starting field from `ctx.standings(rp.start_round)`
  (already filters byes/dropped, appends late entrants). Convert `Player.wins`
  → doubled wins, carry `spread`. Draws: doubled-win = `2·wins + ties`.
- **Repeats / previous-round**: `ctx.repeats` gives total meetings incl. "Bye";
  "played last round" comes from the result slips whose `round == rp.start_round`.
- **RNG**: `ctx.rng` (`ChaCha8Rng`, `RngCore`).

`Ctx` (in `strategies/mod.rs`) gains `pub cop_config: Option<&'a CopConfig>`;
`pair.rs::run_strategy` gains a `RP::Cop => cop::pair_cop(ctx, rp, cfg, total)`
arm that errors cleanly if `cop_config` is `None`. `total` = `ctx.round_pairings`
max round.

## Model / JSON boundary (`model.rs`)

```rust
#[derive(Debug, Clone, Deserialize)]
pub struct CopConfig {
    pub place_prizes: i32,                 // number of place prizes (1-indexed count)
    pub gibson_spreads: Vec<i32>,          // forward-filled by rounds-remaining
    pub hopefulness: Vec<f64>,
    pub control_loss_thresholds: Vec<f64>,
    pub control_loss_activation_round: i32,// 0-indexed
    pub simulations: u32,
    pub always_wins_simulations: u32,
    #[serde(default)] pub disallow_repeat_byes: bool,
    // class_prizes: deferred (Phase 5)
}
// on PairingInput:
#[serde(default)] pub cop_config: Option<CopConfig>,
```

## Django / settings changes

1. **`tournaments/pairing/round_pairing.py`**: add `RP.COP = "COP"` to the enum
   and `STRATEGY_TYPES`, an `ABBREV` entry (`"CO": RP.COP`), and treat COP like a
   sliding strategy in `blocks_to_round_pairings` (`start_round = round − pair_from`,
   default `pair_from = 1`).

2. **`DivisionSettings` (`models.py:253`)**: add a `cop_config = models.JSONField(
   default=dict)` holding `{place_prizes, gibson_spread, hopefulness,
   control_loss_threshold, control_loss_activation_round, simulations,
   always_wins_simulations, disallow_repeat_byes}` (scalars). New migration.
   Keeping it one JSON blob (vs discrete columns) matches `round_pairings` /
   `pairing_blocks` precedent and avoids churn as tuning knobs evolve.

3. **`PairingData` (`base.py`)**: add `cop_config: dict | None = None`. In
   `for_division`, read `division.settings.cop_config` (or `None`). `what-if`
   preserves it via `copy.copy` (whatif.py:52); `simulate.py` builds
   `PairingData` directly and can leave it `None` for non-COP schedules.

4. **`engine.py::pairing_data_to_input`**: when any `round_pairings` entry is
   COP and `pd.cop_config` is present, emit a `cop_config` object, expanding each
   scalar into a 1-element list for the array fields:
   ```
   "cop_config": {
     "place_prizes": c["place_prizes"],
     "gibson_spreads": [c["gibson_spread"]],
     "hopefulness": [c["hopefulness"]],
     "control_loss_thresholds": [c["control_loss_threshold"]],
     "control_loss_activation_round": c["control_loss_activation_round"],
     "simulations": c["simulations"],
     "always_wins_simulations": c["always_wins_simulations"],
     "disallow_repeat_byes": c.get("disallow_repeat_byes", False),
   }
   ```

5. **Settings UI**: a COP config section on the division settings page, shown
   when any block uses COP. Fields: place prizes, gibson spread (default ~250),
   hopefulness (default ~0.05), control-loss threshold (default ~0.15) +
   activation round, simulations (default ~1000) and always-wins simulations
   (default ~1000), disallow-repeat-byes toggle. Validate: `place_prizes ≥ 1`,
   `simulations ≥ 1`, thresholds in `[0,1]`. Follows the existing two-step
   settings flow (memory: "round count form → pairings formset").

6. **Event log**: settings edits already route through the settings save path;
   confirm the COP config field is covered by the existing settings command and
   digest (no *new* mutating POST view is expected — the config rides on the
   existing settings save). If a new endpoint is added, it must be command-backed
   (`test_event_completeness.py`).

## Phases (each independently verifiable)

**Phase 1 — Rust core, no sims yet (scaffold + weights). DONE.**
`CopConfig` on `model.rs`; `RP::Cop` (serde `"COP"`); `Ctx.cop_config`; `pair.rs`
dispatch + bye-internal branch; `strategies/cop.rs` with the `CopPlayer` model,
record sort, real gibson rank + real can-cash sim truncation, a faithful port of
the **weight graph** (`build_weight_edges`, pure in an injected `Decisions`),
min-weight matching via `max_weight_matching_pairs`, bye + fixed-pairing
(prepaired) handling, and the Swiss-initial round-1 fallback. The simulation is
stubbed (`stub_decisions`): contender set for every prize rank = the whole cash
group, control loss disabled. The last-round KOTH path is already fully faithful
(RNG-independent). Verified: `cargo test` (10 COP tests + full suite of 54 green)
— valid full matching, one bye on an odd field, honors a `fixed_pairings` pin,
deterministic, missing-config → error. **Oracle cross-check passed**: `gibson_rank`
and `sim_player_ids` match COP.pm exactly on the gibson / tight-field /
sim-truncation cases.

**Phase 2 — Simulations + contenders. DONE.**
Ported `factor_pair`, `factor_pair_minus_player`, `play_round`, the two-pass
`sim_factor_pair`, the finishers-in-nth contender computation, `get_control_loss`
/ `sim_player_always_wins`, and the destiny's-child derivation, all off `ctx.rng`
(seeded ChaCha8). `stub_decisions` is replaced by `compute_decisions`, which
mirrors the `cop()` orchestration (COP.pm ~1030–1264) and feeds real contender /
gibson / control-loss values into `build_weight_edges`. The half-integer
factor-width cap on an odd non-gibsonized field is handled to match Perl's
fractional-index truncation.

Verified (`cargo test`, 58 green): Tier-1 weight-graph behavior via injected
decisions (a gibsonized leader is never paired with a live cash contender; a
leader whose only catcher is rank 1 is pinned to rank 1); `compute_decisions`
determinism under a fixed seed; cash boundaries in range. **Oracle cross-checks**
(`examples/cop_demo.rs` vs `tools/cop-oracle`):
- Last-round KOTH (deterministic): **exact** pairing parity —
  `(0,1),(2,3),(4,5),(6,7)` from both.
- A 2-rounds-remaining, tie-heavy 8-player field: identical `gibson_rank`, identical
  leader pairing (rank 0 vs 3), identical out-of-contention pair (6,7), and the
  same contention block {0..5}; a single internal swap differed (oracle 1-4/2-5 vs
  Rust 1-5/2-4) — the expected Tier-2 RNG-boundary effect (different RNG streams),
  not a logic divergence.

**Phase 3 — Django plumbing. DONE.**
`RP.COP` + `STRATEGY_TYPES` + `ABBREV` (`"CO"`) in `round_pairing.py` (COP is a
sliding strategy, so block expansion needed no change);
`DivisionSettings.cop_config` JSONField + migration `0033`;
`PairingData.cop_config` + `for_division`; `engine.py::_cop_config_to_input`
expands the scalar config into the engine's `CopConfig` (per-round-array fields →
1-element arrays), emitting `null` when unusable so a COP round fails loudly.
Verified: adapter shape test, empty-config → null, `pair_with_engine` pairs a full
COP round end-to-end, and a COP round with no config raises `PairingError`
(527-test suite green). **Rebuilding the PyO3 extension (`make rust-engine`) is
required** for the Python boundary to see `RP::Cop`. Note: COP is now selectable
in the schedule editor, but there's no config *form* until Phase 4 — selecting it
without setting `cop_config` errors at pair time.

**Phase 4 — Settings UI + end-to-end.**
COP config form/section; validation; make COP selectable in the schedule editor.
Verify with `/run` (or the verify skill): configure a small test division for
COP via the "create test tournament" action (memory:
`feedback_no_password_changes`), enter a few rounds of results, pair a round with
COP, confirm sane pairings and that regenerate is idempotent under a fixed seed.

**Phase 5 — Class prizes (optional/later).**
Only if/when Baxter models an in-division class. Port `get_class_prize_pairings`
and the class/`top_class` weight terms; extend `CopConfig` with `class_prizes`.

## Open questions / risks

- **Class prizes** need a within-division "class" concept Baxter lacks. Deferred
  (Phase 5); core port is place-prizes only. — *Decision needed before Phase 5,
  not before starting.*
- **Simulation cost.** Two factor-pair passes + per-player always-wins sims at
  1000 sims each can be a fraction of a second to seconds for a large division,
  single-threaded. Mitigations already in COP: sim-player truncation, always-wins
  only when no gibson. If it's too slow, lower default `simulations` and/or add
  Rayon later (must stay seed-deterministic — partition the RNG stream, don't
  share `rand()`).
- **Determinism under matching ties.** COP's integer weights collide often; add
  the splitmix64 per-edge tiebreak (D3) or pairings could flip between runs.
- **`number_of_rounds_remaining` indexing.** Re-derive carefully against COP.pm
  (`(number_of_rounds − 1) − sr0`) vs our `total − start_round`; add a unit test
  pinning gibson detection at a known boundary so an off-by-one is caught.
- **Parity oracle is layered, not bit-exact.** The real `COP.pm` oracle
  (`tools/cop-oracle/`) is in place, but different RNG + matching solver mean
  correctness rests on the three-tier comparison (D8): exact on RNG-independent
  logic + weight matrix, statistical on the sim layer, matching-parity only on
  unique-optimum cases. A read-through against `COP.pm` still backs the port.
```
