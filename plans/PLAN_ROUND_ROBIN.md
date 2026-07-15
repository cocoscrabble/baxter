# Plan: Fully general fixed pairings for round robins

Companion to `PLAN.md` but independent of it — this can be implemented in any
order relative to those phases. File/line references are pinned at commit
`09b01d3`. Written for a fresh session: read the Background before touching code.

**Both-engines policy applies throughout**: until Baxter is cut over to the Rust
crate, every behavioral change lands in both `tournaments/pairing/` (Python) and
`scrabble-pairing/` (Rust), with parity-corpus coverage. Phases 1–3 are
Python-first; Phase 4 mirrors them in Rust before Phase 5 starts.

> **Sequencing update (2026-07-13)**: the owner decided the Rust cutover
> (`PLAN_RUST_CUTOVER.md`) lands **before** phases 2–5 of this plan. Read this
> plan through that lens: the solver and validation layer are implemented once,
> in Rust (`strategies/roundrobin.rs`); Phase 4 collapses into Phases 2–3; the
> Python-side items reduce to the app-layer wiring in `fixed_pairings.py` and
> the `PairingData`/`published_pairings` plumbing, which stay Python. New tests
> are native Rust tests plus DB-backed lifecycle tests through the adapter —
> the parity corpus can't oracle-check solver behavior the Python engine never
> had. Phase 1 (module extraction) is still worth doing on the Rust side; the
> Python-side split only matters if any of this lands pre-cutover.

## Goal

A tournament director should be able to fix any set of pairings in any rounds of
a round-robin block, and the engine should find *some* valid round robin
honoring all of them — adjusting both the order of rounds and the assignment of
players to schedule slots — failing **only when no valid round robin containing
all the requested fixed pairings exists**. Failures must produce specific,
actionable error messages. Field size is small (~20 is the practical ceiling
for a RR), so a complete search is affordable.

## Background

### Current algorithm (`tournaments/pairing/basic.py:61-226`)

The circle-method construction has three degrees of freedom; today only one is
searched:

1. **Player → slot assignment**: frozen to seeding order (`_rr_players`,
   basic.py:78).
2. **The 1-factorization structure**: frozen to the circle method's specific
   round templates (`_rr_templates`, basic.py:89).
3. **Position → template permutation**: the only searched dimension
   (`_rr_permutation`, basic.py:130 — greedy transpositions; played rounds are
   fixed points, each fixed pairing drags its whole template to the requested
   position).

The greedy permutation is *complete at its own level* (no permutation-level case
fails spuriously), so all spurious failures come from the frozen dimensions 1–2.

### Confirmed failure mode

6 players seeded P1–P6, fixed pairings **P1–P2 and P3–P4 both in round 1**:
`PairingError: Fixed pairings conflict`, even though `{P1P2, P3P4, P5P6}` is a
legal RR round. The two pairs sit in different circle templates (t4 and t0)
under the seeding slot assignment, and permuting whole templates can never put
them in the same round. Repro script (promote to a test in Phase 3):
one-pair-per-round always works; same-round combinations and colliding
multi-round sets fail today.

```python
pd = PairingData(result_slips=[], entrants=six_entrants, repeats=Repeats(),
                 round_pairings=five_rr_rounds,
                 fixed_pairings={1: [("P1", "P2"), ("P3", "P4")]})
pair_round(pd, pd.round_pairings[0])   # raises PairingError today
```

### The right problem statement

A round robin on E players (E even; phantom `Bye` appended for odd fields) is a
**1-factorization of K_E**: E−1 pairwise-disjoint perfect matchings. Fixed
pairings and played rounds are **pre-colored edges** ("{a,b} plays at position
r"). The general question is: *can this partial (E−1)-edge-coloring of K_E be
completed?* Two consequences:

- **Dynamic slot reassignment alone is not enough.** Relabeling players over the
  circle schedule reaches only circle-isomorphic 1-factorizations; K₈ already
  has 6 non-isomorphic ones (only one is the circle). Meeting the "only give up
  when genuinely impossible" bar requires searching matchings directly. The
  direct search subsumes slot reassignment, so we skip that intermediate layer.
- **Completion is NP-complete in general** (Colbourn), but at E ≤ ~22 with a
  handful of pins, deterministic backtracking with matching-existence pruning
  is milliseconds.

**Partial blocks**: a RR block may be shorter than E−1 rounds
(`blocks_to_round_pairings` allows any length). The solver must only produce
matchings for the block's actual positions — k disjoint matchings containing
the pins, which for k < E−1 is strictly easier than full completion. Only a
full-length block requires a complete 1-factorization.

### Design constraints (non-negotiable)

- **Determinism.** The engine pairs lazily per round: `_rr_block_pairings` is
  recomputed independently for every calendar round and every page render, so
  the solver must be RNG-free with canonical ordering (players by seeding, fixed
  pairs sorted, matchings enumerated lexicographically). Every call must
  reconstruct the identical block schedule.
- **Stability.** When the current fast path suffices, output must be
  byte-identical to today (existing tournaments, existing tests, parity corpus).
  When the full solver runs, prefer solutions close to the circle schedule so
  adding one fixed pairing perturbs as few rounds as possible.
- **Played rounds are hard pins.** A round with result slips pins its entire
  matching (already true today via `_identify_template` — generalize to pinning
  the matching itself, no template identification needed).
- **Partially-played rounds pin their *published* matching, not just their
  played games.** Today this is safe by accident of the template structure:
  one played pair identifies the round's whole template, the template is
  pinned, and templates partition all pairs — so the unplayed-but-published
  games of an in-progress round can never be duplicated elsewhere. The general
  solver loses that implication: pinning only played edges would let it
  complete an in-progress position differently from the pairings already
  printed on slips, and a later round could then duplicate a published game.
  In-progress rounds are exactly the ones `add_fixed_pairing`'s revert logic
  keeps (`_rounds_to_regenerate` excludes any round with results), so their
  published remainder is a hard constraint. Fix: `PairingData` gains the
  published pairings of non-draft rounds and the solver pins them (see
  Phase 3).

### Related defects to fix along the way

- **Charlottesville is a trap** (fixed in Phases 2 + 5): `RP.is_round_robin`
  (round_pairing.py:23) includes Charlottesville, but `_ROUND_ROBIN_FAMILY`
  (pair.py:87) and `_RR_FAMILY` (fixed_pairings.py:32) do not — so a fixed
  pairing on a Charlottesville round takes the exclude-and-pair-the-rest path,
  which corrupts the rotation schedule silently.
- **`_already_played` guard** (fixed_pairings.py:91) only fires for RR/DRR
  blocks; it stays, and the solver's validation subsumes it with a better
  message.

### Existing surfaces to preserve

- `add_fixed_pairing` (fixed_pairings.py:73) already does
  add → regenerate → rollback-on-PairingError, surfacing `str(e)` to the TD.
  Improving error specificity automatically improves that flow; no new UI work.
- `views.py:623` catches `PairingError` at render and shows the message.
- Existing test `test_conflicting_fixed_pairings_raise`
  (test_pairings.py:1278) uses P1–P2 and P1–P3 in the same round — genuinely
  impossible, stays valid. All other tests in `RoundRobinFixedPairingTests` and
  `RoundRobinUnplayedRoundsTests` must keep passing unchanged.

---

## Phase 1 — Extract round-robin code into its own module

Pure move, zero behavior change, its own commit.

**Python**: create `tournaments/pairing/roundrobin.py`; move from `basic.py`:
`_pair_rr`, `_is_bye_name`, `_rr_players`, `_rr_templates`,
`_identify_template`, `_rr_permutation`, `_rr_block_pairings`,
`pair_round_robin`, `pair_double_round_robin`, and `pair_charlottesville`
(it's RR-family and Phase 5 rewrites it here). `basic.py` keeps KotH, QotH,
Random, RandomNoRepeats.

Update importers:
- `tournaments/pairing/pair.py:13-21` — split the import.
- `tournaments/tests/test_pairing_algorithms.py:10` — split the import.
- `tournaments/fixed_pairings.py:30` comment references `pairing.basic` — update.

**Rust** (structural parity, also no behavior change): split the RR half of
`scrabble-pairing/src/strategies/basic.rs` (lines ~51–246: `pair_round_robin`,
`pair_double_round_robin`, `rr_players`, `rr_template_of_pair`,
`identify_template`, `place_template`, `rr_permutation`, `rr_block_pairings`,
plus `pair_charlottesville` at ~249) into `strategies/roundrobin.rs`; add
`pub mod roundrobin;` to `strategies/mod.rs`; update the dispatch arms in
`pair.rs:32-40`.

**Verify**: `uv run python manage.py test tournaments.tests` and
`cargo test` in `scrabble-pairing/` — all green, no test edits needed.

## Phase 2 — Validation layer with specific errors

New function in `roundrobin.py`, called at the top of `_rr_block_pairings`
before any solving:

```python
def validate_block_pins(players, block_positions, played, fixed) -> None:
    """Raise PairingError with a specific message on any cheaply detectable
    conflict. `fixed` is [(position, frozenset({a, b}))...]; `played` is
    {position: set_of_pairs}."""
```

Checks, each with its own message naming the players/rounds involved:

1. A fixed pairing names a non-entrant or a player twice (defensive; the grid
   validates ids, but `PairingData` is name-keyed).
2. Same player fixed against two different opponents at one position
   ("P1 is fixed against both P2 and P3 in round 1").
3. The same pair fixed at two different positions in one block (they meet once
   per RR cycle).
4. A fixed pair that already **played** in this block at a different position
   ("P1 and P2 already played in round 2").
5. More than E/2 pairs fixed at one position.
6. A player with more distinct fixed opponents across the block than the block
   has positions available to them.
7. **DRR-specific**: two conflicting pins mapping to the same position via
   `position_of` ("rounds 5 and 6 are the two halves of one double-round-robin
   slot and cannot have different fixed pairings").
8. **Interim Charlottesville guard** (removed in Phase 5): in
   `fixed_pairings.py`, reject `add_fixed_pairing` when the round's strategy is
   Charlottesville — "Fixed pairings are not yet supported for Charlottesville
   blocks" — instead of silently corrupting the rotation. Implement by checking
   the round's `RoundPairing.pairing` in `add_fixed_pairing`.

These messages replace the two generic strings in `_rr_permutation` for every
case they catch; the permutation/solver errors remain as the fallback for
conflicts only the search can detect.

**Tests** (`test_pairings.py`, extend `RoundRobinFixedPairingTests`): one per
check above, asserting on message substrings; Charlottesville rejection test in
the `fixed_pairings` lifecycle tests.

## Phase 3 — General completion solver (the core)

### Entry point and data flow

Rewrite `_rr_block_pairings` around a block-level solver:

```python
def solve_block(players, num_positions, played, fixed) -> list[set[frozenset]]:
    """Return one matching per block position, pairwise edge-disjoint,
    containing all pins. Deterministic. Raises PairingError when impossible."""
```

- `players`: seeding order, phantom Bye appended for odd fields (unchanged).
  Index players 0..E−1; an edge is `frozenset({i, j})`.
- `num_positions`: the block's actual position count (`len(block_rounds) // k`,
  clamped to ≤ E−1) — supports partial blocks (see Background).
- `played`: {position → pinned edges}, built from **result slips ∪ the
  published pairings of every non-draft round in the block** (replaces
  `_identify_template`; a played round pins its matching whether or not it is
  a circle template, which also un-breaks blocks where entrants were edited
  after results — today that raises "did the entrants change?"). Including
  published pairings is what makes partially-played (in-progress) rounds safe:
  their unplayed remainder must be honored, not recomputed (see Design
  constraints). Published-but-unplayed rounds inside the block are reverted to
  draft by `add_fixed_pairing` before regeneration, so on that path they are
  free; on pure render paths pinning them is harmless and prevents drift.
  Bye games are included as (player, bye-index) edges.
- **`PairingData` change**: add a field carrying published pairings by round
  for non-draft `RoundPairings` (e.g.
  `published_pairings: dict[int, list[tuple[str, str]]]`, default empty),
  populated in `for_division` from the block's non-DRAFT rounds. Callers that
  construct `PairingData` by hand (tests, corpus exporter) are unaffected by
  the default.
- `fixed`: [(position, edge)], sorted canonically.
- The per-round caller computes `solve_block` for the whole block and reads off
  `assignment[position_of(rp.round)]` — same shape as today. Optionally memoize
  on `PairingData` keyed by block identity (nice-to-have, not correctness).

### Algorithm

1. **Layer 0**: `validate_block_pins` (Phase 2).
2. **Layer 1 — fast path**: the existing template permutation, kept verbatim.
   All pins template-consistent under the seeding slot assignment → return the
   permuted circle schedule. Guarantees byte-identical output for every case
   that works today. If a played/published round's pins don't identify a
   single circle template (entrants edited mid-event, or a Layer 2 schedule
   from an earlier fixed-pairing change), **fall through to Layer 2 instead of
   raising** — template mismatch is a fast-path miss, not an error.
3. **Layer 2 — backtracking completion**, only when Layer 1 raises:
   - Maintain `used_edges`. Process positions: fully played first (consume
     their edges, no choice), then positions by descending pin count, then
     free positions in index order.
   - At each position, enumerate perfect matchings of the available graph
     (edges ∉ `used_edges`, both endpoints not already pinned at this
     position, superset of this position's pins). **Candidate order**: any
     still-unused circle template compatible with the pins first (stability
     bias), then general lexicographic enumeration (recursively match the
     lowest-index unmatched player with each admissible partner in index
     order), skipping templates already yielded.
   - **Prune** after each choice: (a) every remaining position's pins are still
     disjoint from `used_edges`; (b) the next constrained position's available
     graph has a perfect matching containing its pins — one
     `networkx.max_weight_matching(maxcardinality=True)` call (dependency
     already present via `pairing.base.blossom`). The leftover of K_E minus
     perfect matchings is always regular, and regular graphs of degree ≥ E/2
     are 1-factorable (proven 1-factorization conjecture), so dead ends are
     shallow; pruning (b) kills them fast.
   - **Node budget**: cap expansions (e.g. 200k). Exhaustive failure raises
     "No valid round robin contains all these fixed pairings." Budget
     exhaustion raises a distinct message ("Could not schedule these fixed
     pairings — try removing one") and logs; at E ≤ 22 this should be
     unreachable, but never lie about having proved impossibility.
   - **No RNG anywhere.**

### DRR

Unchanged convention: positions are consecutive round *pairs* sharing a
matching with mirrored starts (`k=2` plumbing stays). Pins from either half map
through `position_of`. Do **not** generalize DRR to independent halves in this
plan; check 7 in Phase 2 reports the convention-level conflicts. (A fully
general DRR is an edge-coloring of the doubled multigraph — noted as a possible
future relaxation, decide only if a TD actually hits it.)

### Byes and dropouts

The phantom Bye is a vertex, so a fixed (X, "Bye") edge — already supported —
flows through the solver unchanged and now composes with other fixed pairings
in the same round. Keep `test_fixed_bye_for_odd_field` green. A mid-block
dropout (PLAN.md Phase 3) keeps its vertex; no solver change needed.

### Tests (Python)

Extend `RoundRobinFixedPairingTests`:

- The Background repro: P1–P2 and P3–P4 both in round 1 (6 players) → solves;
  all-play-all preserved; both pairs in round 1.
- A fully pinned round: 3 fixed pairs = complete matching in round 1, plus a
  fixed pair in round 3.
- Fixed bye + fixed pairing in the same round of an odd field.
- Mid-event: play rounds 1–2 (default schedule), then add two same-round fixed
  pairings for round 4 → played rounds untouched, block still complete.
- **In-progress round**: enter results for 2 of 3 games in round 2 (round stays
  published with one unplayed pairing), then add a fixed pairing for round 4 →
  round 2's unplayed published game is preserved verbatim and never duplicated
  in another round; the solver treats it as a hard pin. DB-backed test (needs
  `RoundPairings` status + published `Pairing` rows), so it lives with the
  lifecycle tests (`RoundRobinFixedPairingLifecycleTests`).
- DRR versions of the same-round case; DRR half-slot conflict raises (check 7).
- Genuinely impossible: keep `test_conflicting_fixed_pairings_raise`; add
  "pair already played" and "same pair, two rounds".
- **Determinism**: two full `pair()` runs produce identical schedules; each
  per-round `pair_round` call agrees with the block solution (no duplicate
  meetings across separately computed rounds).
- **Stress/completeness**: for E in {6, 8, 10}, build a random 1-factorization
  (seeded RNG *in the test only*), sample 3–6 (pair, round) pins from it, and
  assert the solver succeeds — this catches false negatives, including
  non-circle-isomorphic targets at E=8+.
- **Partial block**: 8 players, 4-round RR block, pins that fit in 4 rounds but
  would collide in a hypothetical full template permutation.
- Perf sanity: 20 players, full block, ~8 fixed pairs across 4 rounds solves
  well under a second.

Also verify `test_pairing_algorithms.py` and the fixed-pairings lifecycle tests
pass unchanged, and run a hands-on pass with a fake tournament (RR division,
add same-round fixed pairings via the UI, confirm the pairings page and the
error flash for an impossible set).

## Phase 4 — Mirror in Rust + parity corpus

Port Phases 2–3 to `scrabble-pairing/src/strategies/roundrobin.rs`:

- `validate_block_pins` equivalent with the same error strings (check how
  `parity.rs` treats errors — if the corpus only compares success output, keep
  message parity anyway for UI consistency after cutover).
- `solve_block`: same layering (fast path → backtracking), same canonical
  orderings, **identical deterministic output** to Python — these cases are
  `deterministic: true` in the corpus, so outputs are compared exactly. The
  matching-existence prune uses the vendored rustworkx max-weight matching
  (`src/matching.rs`), maxcardinality mode.
- `PairingInput` gains the published-pairings-by-round field with
  `#[serde(default)]` so existing corpus cases and callers parse unchanged.
- Regenerate the corpus via `scripts/export_pairing_corpus.py` after the Python
  side lands, adding cases: same-round double fixed pairing (the repro), fully
  pinned round, mid-event pins with played rounds, an in-progress round with a
  published unplayed game plus a later fixed pairing, DRR same-round, partial
  block, fixed bye + fixed pair, and one impossible set (expect error on both
  sides — extend the corpus schema with an `expect_error` flag if it lacks
  one).

**Verify**: `cargo test` in `scrabble-pairing/`, plus the Python suite (corpus
export script runs against it).

## Phase 5 — Charlottesville solver

Charlottesville (basic.py:237 → `roundrobin.py` after Phase 1) is a **bipartite
round robin**: snake-split the field into groups G1/G2 of size m = E/2; each
player meets every member of the other group. Fixed-pairing support is the same
completion problem on K_{m,m} — partial Latin-rectangle completion — solved by
the same backtracking restricted to cross-group edges:

- **Group split stays frozen to the snake seeding** (the split *is* the
  format); the searched degrees of freedom are which cross-group matching lands
  in which round. Reuse `solve_block` with the available-edge set restricted to
  cross-group pairs and templates = the current rotation's rounds; the
  enumerate/prune machinery is shared, so factor it to take an edge-universe
  parameter rather than assuming K_E.
- Blocks are typically m rounds but may be shorter — same partial-block rule.
- **Validation additions**: fixed pairing with both players in the same group →
  "P1 and P4 are in the same Charlottesville group and never play each other";
  same-group checks precede the generic ones. Bipartite Hall-style check is
  subsumed by the matching prune.
- Odd field: phantom Bye joins the short group (Charlottesville already appends
  a Bye, basic.py:242); a fixed bye is a cross-group edge like any other.
- **Wiring**: add `RP.Charlottesville` to `_ROUND_ROBIN_FAMILY` (pair.py:87)
  and `_RR_FAMILY` (fixed_pairings.py:32) so fixed pairings route through the
  solver and block-level regenerate/revert logic instead of
  exclude-and-pair-the-rest; make `round_robin_block_rounds` recognize
  Charlottesville blocks; **remove the Phase 2 interim rejection**.
- Played Charlottesville rounds pin their matchings exactly as in RR.
- **Tests**: meets-in-requested-round, same-round pair of fixed pairings,
  same-group rejection message, mid-event with played rounds, odd field with
  fixed bye, all-cross-group-meetings-exactly-once invariant, determinism.
- **Rust mirror + corpus cases** in the same phase (Charlottesville exists in
  the crate at basic.rs:252 → roundrobin.rs).

## Verification & commit conventions

- Python: `uv run python manage.py test tournaments.tests` (530 passing at plan
  time; expect net additions each phase).
- Rust: `cargo test` in `scrabble-pairing/` (includes `parity.rs`).
- Hands-on after Phases 3 and 5: fake tournament → RR (and Charlottesville)
  division → add fixed pairings through the grid, including an impossible set,
  and confirm the flash messages.
- One commit per phase; `jj describe` then `jj new` after each. Confirm scope
  before committing if the working copy has unrelated changes.

## Open decisions for the owner

1. **Node budget size and behavior** — proposed 200k expansions with a distinct
   "try removing one" message; acceptable, or should exhaustion be treated as
   a hard bug (assert + log) given E ≤ ~22?
2. **Error-message parity between engines** — exact-string parity (simplest for
   post-cutover UI stability) vs. corpus-level success-only parity. Proposed:
   exact strings.
3. **DRR mirrored-halves convention** — this plan keeps it and reports
   half-slot conflicts as errors. Relaxing DRR to independently scheduled
   halves is out of scope; flag if that's ever actually wanted.
