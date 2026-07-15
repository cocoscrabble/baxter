# Plan: cut Baxter over to the Rust pairing library

Companion to `PLAN.md` and `PLAN_ROUND_ROBIN.md`; see "Sequencing" for how the
three interact. References pinned at the commit containing the two plan files
(parent `09b01d3`). Written for a fresh session.

## Goal

Replace the Python pairing *computation* with the `scrabble-pairing` Rust crate
called as a PyO3 extension module, while keeping the ORM-facing layer
(`PairingData` assembly, standings display, publish/regenerate lifecycle,
presenters) in Python. End state: the Python strategy implementations are
deleted, the "apply pairing fixes to both engines" policy is retired, and new
engine work (e.g. the round-robin solver) is written once, in Rust.

## Current state

### The crate (`scrabble-pairing/`)

- Standalone, wasm-clean (seeded ChaCha8 RNG, no OS entropy; vendored
  rustworkx max-weight matching; no rayon). Parity-passing against a
  Python-generated corpus (`tests/corpus/cases.json`, exact comparison for
  deterministic strategies, invariants for random ones).
- Serialized boundary (`src/lib.rs:20`): `pair_json(&str) -> Result<String>`
  wrapping `pair(&PairingInput) -> Vec<RoundResult>`. Input
  (`src/model.rs:28`): `players`, `result_slips`, `round_pairings`,
  `fixed_pairings` (JSON object keys are *strings*, parsed to i32), `seed`
  (default 0). Output: per round `{round, pairings: [{first, second,
  repeats}], error}` — starts balancing is encoded in first/second order,
  identical to the Python engine.

### Python engine consumers (what must keep working)

| Consumer | Uses | Fate |
|---|---|---|
| `generate_pairings.py:12-13` | `pair(pd)`, `standings_after_round` (seed_rank fallback) | **The cutover point** — `pair()` call swapped; standings stay Python |
| `views.py:58-59`, `pairings_view.py` | `PairingData`, `standings_after_round`, `PairingError`, `STRATEGY_TYPES` | stays Python (display layer) |
| `fixed_pairings.py` | `PairingData` (block computation), `PairingError` | stays Python (lifecycle layer) |
| `forms.py:8` | `STRATEGY_TYPES` | stays (becomes a static list) |
| `simulate.py:23` | **`pair_round` per-round**, incremental `Repeats`/`Starts` | needs porting or retirement (Phase 5, see decision 4) |
| `scripts/export_pairing_corpus.py` | Python engine as oracle | retired at deletion; corpus freezes as Rust regression fixtures |

### Semantic differences to bridge (the adapter's job)

1. **Errors**: Python `pair()` raises `PairingError`, aborting
   `regenerate_pairings` atomically (views/fixed_pairings catch and surface
   `str(e)`). Rust `pair()` never fails the run — a bad round yields
   `RoundResult{pairings: [], error: Some(msg)}` and continues. **Decided**:
   the adapter raises `PairingError(first error message)` when any round
   carries an error, preserving the all-or-nothing regenerate semantics.
   (Per-round error surfacing — pairing the good rounds and flagging the bad
   one — is a possible later refinement; the per-round information is already
   in the boundary, so nothing needs re-plumbing.)
   - Known behavioral delta: an *unknown strategy* is silently an empty round
     in Python but an error in Rust. Unreachable through the forms
     (choices come from `STRATEGY_TYPES`); the Rust behavior is better — accept
     it, note it in the adapter docstring.
2. **Randomness**: Python's Random/RandomNoRepeats (and the blossom random
   tiebreak) use the unseeded global `random`; Rust takes an explicit `seed`
   (default 0). After cutover random strategies are reproducible per seed.
   **Seed policy (decided)**: persist a per-division seed —
   `DivisionSettings.pairing_seed`, initialized randomly on creation — and
   pass it on every engine call. Lazy draft regeneration
   (`_autogenerate_pairable_rounds`) then stops reshuffling random rounds on
   every page render (an improvement over today's global RNG). The seed is
   re-rolled only by an explicit TD action: add a "reshuffle" affordance on
   the pairings page that re-rolls the seed and calls
   `regenerate_pairings`; until that button exists the seed simply stays
   stable.
3. **Tie-breaking determinism**: the corpus uses distinct ratings everywhere
   (`export_pairing_corpus.py` gives P01..Pn strictly descending ratings).
   Equal ratings / equal records exercise sort stability differences between
   the engines. Both sorts are stable, but the *input order* must match:
   Python seeds from `division.entrants.all()` order. Add tied-rating and
   tied-record cases to the corpus **before** burn-in (Phase 3).
4. **Bye-name matching**: Python `.lower() == "bye"`, Rust
   `eq_ignore_ascii_case`. Identical for ASCII; only a non-ASCII lookalike
   name could diverge (ignorable, but note it).

### Deployment

Dokku deploys via the repo `Dockerfile` (`python:3.14-slim` + uv + node). The
extension wheel must be built in the image — add a Rust build stage (Phase 1).

## Architecture

### Binding crate: `scrabble-pairing-py/`

A *separate* crate, sibling to `scrabble-pairing/`, so the core crate stays
wasm-clean (PyO3 links libpython and can't target wasm):

- `Cargo.toml`: `pyo3` (latest; 3.14 support), features `["extension-module",
  "abi3-py312"]` — abi3 so one wheel covers 3.12+ including 3.14 and future
  upgrades; `scrabble-pairing = { path = "../scrabble-pairing" }`.
- `pyproject.toml` with `maturin` build backend.
- One exported function to start:
  `fn pair_json(input: &str) -> PyResult<String>` delegating to
  `scrabble_pairing::pair_json`, wrapped in `py.allow_threads` (frees the GIL
  during pairing; trivial and correct since the core takes/returns owned
  strings). JSON overhead at ≤ ~40 players is noise; do **not** build a typed
  conversion layer.

Keep the two crates independent (no workspace) unless dependency
version-pinning between them gets annoying; a path dependency suffices.

### Python adapter: `tournaments/pairing/engine.py`

```python
def pair_with_engine(pd: PairingData) -> list[tuple[int, list[DisplayPairing]]]
```

- Dispatches on `settings.PAIRING_ENGINE` (`"python" | "rust" | "shadow"`,
  via python-decouple, default `"python"` until Phase 4).
- **rust path**: serialize `pd` → input dict (`entrants` →
  `players: [{name, rating}]`; `result_slips` field-for-field;
  `round_pairings` as `{round, start_round, pairing}` — shapes already match
  the corpus exporter, reuse its serialization helpers by moving them into
  `engine.py` or a shared module; `fixed_pairings` keys **stringified**; plus
  `seed` per decision 1) → `scrabble_pairing_py.pair_json` → parse → if any
  round has `error`, `raise PairingError(msg)` → else build
  `[(round, [DisplayPairing(Player(first), Player(second), repeats), ...])]`.
  Verify first (it's true today) that `regenerate_pairings` consumes only
  `.first.name` / `.second.name` / `.repeats` from the output, so bare
  `Player(name)` objects are sufficient.
- **shadow path**: compute the Python result, also run the rust path, compare
  (exact for deterministic strategies; skip rounds whose strategy is
  Random/RandomNoRepeats/SwissPlusRandom — determine from
  `pd.round_pairings`), `logger.error` any divergence with the serialized
  input for repro, return the **Python** result. Never let a rust-side
  exception break shadow mode: catch, log, return Python result.
- `generate_pairings.regenerate_pairings` (line ~150) changes one line:
  `pair(pd)` → `pair_with_engine(pd)`.

## Phases

### Phase 1 — Binding crate, wheel build, Docker

- Create `scrabble-pairing-py/` as above.
- Wire into uv: add `scrabble-pairing-py` to `[project.dependencies]` and
  `[tool.uv.sources] scrabble-pairing-py = { path = "scrabble-pairing-py" }`
  in `pyproject.toml`. `uv sync` then builds the wheel via maturin (requires a
  local Rust toolchain — already a dev prerequisite for the crate).
  - Dev-loop note for `CLAUDE.md`: after editing Rust, rebuild with
    `uv sync --reinstall-package scrabble-pairing-py` (uv does not watch the
    crate source); add a `Makefile` target.
- Dockerfile: add a builder stage (`FROM rust:slim AS wheels` + pip/uvx
  maturin, or install rustup in the existing image before `uv sync`) that
  builds the abi3 wheel; copy it into the runtime stage and point the uv
  source at it (or run `uv sync` after the wheel is available). Preserve layer
  caching: copy `scrabble-pairing*/` sources after the dependency layers.
- Smoke test (`tournaments/tests/test_rust_engine.py`): import the module,
  pair a tiny Swiss input, assert round/pairing shape.
- **Verify**: `uv run python manage.py test tournaments.tests` (all green,
  nothing uses the module yet), `docker build .` succeeds.

### Phase 2 — Adapter, engine flag, shadow mode

- `tournaments/pairing/engine.py` per Architecture; `PAIRING_ENGINE` in
  `baxter/settings.py` (decouple, default `"python"`); the one-line switch in
  `regenerate_pairings`.
- `DivisionSettings.pairing_seed` (BigInteger, randomly initialized) +
  migration; `PairingData.for_division` carries it into the input dict's
  `seed`. The reshuffle button (see semantic difference 2) can land here or
  as a fast-follow — it's UI sugar, not a cutover dependency.
- Refactor `scripts/export_pairing_corpus.py` to import the
  PairingData→input-dict serializer from `engine.py` instead of hand-rolling
  it (one serializer, no drift).
- **Tests**:
  - Serializer round-trip: PairingData → dict → the exact JSON shape in
    `model.rs` (stringified fixed-pairing keys, defaulted fields).
  - Adapter: error round → `PairingError`; output-shape mapping; shadow-mode
    divergence logging (monkeypatch the rust call to return a mutated result).
  - The full existing pairing test surface under the rust engine:
    `PAIRING_ENGINE=rust uv run python manage.py test tournaments.tests`.
    Make the flag readable from the environment at test time so CI can run
    the suite twice (document both invocations in `CLAUDE.md`).

### Phase 3 — Parity hardening + burn-in

- Extend the corpus with the known gaps **before** trusting shadow mode:
  tied ratings (several players at the same rating), tied records/spread in
  standings-driven strategies, an odd field with fixed pairings, division
  entrants whose DB order differs from rating order.
- Fix any divergences (expected location: sort tie-breaks; fix by making the
  intended key explicit in *both* engines rather than relying on stability).
- Run `PAIRING_ENGINE=shadow` in dev and on prod for a few real/fake
  tournaments (drive one full fake tournament end-to-end: create, publish,
  enter results, add fixed pairings, finish). Dokku logs go to stdout, so
  divergence logs are visible via `dokku logs`.
- Exit criterion: zero divergence logs across the burn-in set.

### Phase 4 — Flip the default

- `PAIRING_ENGINE` default → `"rust"`. Python engine remains reachable via
  env var as the escape hatch for one release cycle.
- Run the full suite both ways in CI until Phase 5.

### Phase 5 — Delete the Python engine, retire the dual policy

- **Port `simulate.py` (decided — keep it)**: drive it through `engine.py` by
  feeding accumulated `result_slips` back into whole-tournament `pair()` calls
  per simulated round instead of `pair_round` + incremental
  `Repeats`/`Starts`; `test_simulate.py` and `scripts/run_simulate.py` then
  exercise the rust engine end-to-end.
- Delete: `pairing/swiss.py`, `pairing/quads.py`, `pairing/basic.py`,
  `pairing/roundrobin.py` (post-extraction), the strategy dispatch +
  `pair_round`/`pair`/`bye_pairing` machinery in `pairing/pair.py`, and the
  `"python"`/`"shadow"` engine paths.
- Keep (they are app logic, not engine): `pairing/base.py`'s
  `PairingData`/DTOs, `Results`/`standings_after_round`/`seedings` (standings
  *display* and `seed_rank`), `Starts` only if still referenced (it shouldn't
  be — verify), `PairingError`; `pairing/round_pairing.py` in full
  (blocks/normalization/`RP`); `STRATEGY_TYPES` moves to `round_pairing.py`
  as a static list matching the crate's `RP` enum (add a test that the crate
  accepts every listed name, via a `pair_json` probe per strategy).
- Freeze `tests/corpus/cases.json` as committed Rust regression fixtures;
  retire `export_pairing_corpus.py` (or keep it runnable against the frozen
  Python engine at a git tag — simplest is delete and rely on the frozen
  corpus + native Rust tests).
- Update `CLAUDE.md` and project memory: the both-engines policy ends here;
  engine changes are Rust-only from this point.

## Sequencing against the other plans

**Decided: cutover first.** Note this adds nothing to deployment complexity —
the Dockerfile build stage is a one-time change inherent to the cutover
whenever it happens, the RR plan never touches deployment, and while the
engine default is `python` (Phases 1–3) a broken wheel build fails at
`docker build` time, never at runtime. The only cost is longer image builds
(cargo compile), mitigated by layer ordering.

The RR solver is the largest engine change on the books, and
post-cutover it is written once in Rust (that plan's Phase 4 collapses into
its Phases 2–3; its Python-side items reduce to the `fixed_pairings.py`
validation wiring, which is app-layer and stays). The cost is that new solver
tests are native Rust tests rather than oracle-checked corpus cases — the
corpus oracle can't cover code the Python engine never had, which is true
regardless of ordering.

`PLAN.md` Phase 3 (late entrants / dropouts) also touches engine behavior
(standings derivation, RR guard): same argument — land it after cutover,
Rust-only, or accept doing it twice. `PLAN.md` Phases 1–2 and 4 are
engine-independent except 1a (the Swiss loop guard), which is **already fixed
in Rust** (swiss.rs:192) and should still be fixed in Python immediately if
any pre-cutover window remains (it is a live prod hang).

The Phase 1 extraction in `PLAN_ROUND_ROBIN.md` (roundrobin module split) is
worth doing on the Rust side regardless; the Python-side split only matters if
the RR work lands before the cutover.

## Decisions

Resolved by the owner (2026-07-13), folded into the phases above:

1. **Seed policy**: persisted per-division `pairing_seed`, re-rolled only by
   an explicit reshuffle action.
2. **Sequencing**: cutover first, then `PLAN_ROUND_ROBIN.md` (Rust-only
   solver) and `PLAN.md` Phase 3.
3. **Error semantics**: raise `PairingError` on any Rust error round for now;
   per-round surfacing is a possible later refinement.
4. **`simulate.py`**: keep and port to the JSON boundary.

Still open:

- **Escape-hatch lifetime.** How long the Python fallback stays after the
  default flips (proposed: one release cycle / one real tournament run on
  rust, then Phase 5).

## Verification

- Python: `uv run python manage.py test tournaments.tests` under both
  `PAIRING_ENGINE=python` and `=rust` (Phases 2–4), rust-only after Phase 5.
- Rust: `cargo test` in `scrabble-pairing/` (corpus parity while the oracle
  lives, frozen fixtures after) and `scrabble-pairing-py/` (binding smoke).
- `docker build .` per phase that touches packaging.
- Hands-on fake tournament under `shadow` (Phase 3) and `rust` (Phase 4).
- One commit per phase; `jj describe` then `jj new`; confirm scope before
  committing a mixed working copy.
