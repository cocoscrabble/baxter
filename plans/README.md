# Plans

Design and implementation plans for larger pieces of work. **New plans go
here** (`plans/*.md`), not in the repo root.

Each plan is written to be picked up in a fresh session: it states the goal,
the design decisions (and why), and a phased breakdown with per-phase
verification. Code references are pinned to a commit, since they drift.

Current plans:

- `PLAN.md` — code-review fix plan (Swiss loop, grid-save reconciliation, late
  entrants/dropouts, access hardening). **Implemented.**
- `PLAN_RUST_CUTOVER.md` — cut the pairing engine over to the `scrabble-pairing`
  Rust crate via a PyO3 extension. **Implemented** (all phases); the Python
  engine is deleted and Rust is the only engine.
- `PLAN_EVENT_LOG.md` — replayable append-only tournament event log.
  **Implemented** (all phases).
- `PLAN_ROUND_ROBIN.md` — fully general fixed pairings for round robins.
  **Implemented** (all phases). The solver lives once in Rust
  (`strategies/roundrobin.rs`): a validation layer plus a two-layer completion
  solver (template-permutation fast path → backtracking) covering round robins,
  double round robins, and Charlottesville, with `published_pairings` plumbing so
  in-progress rounds pin their printed games.
- `PLAN_DESKTOP_APP.md` — package Baxter as a downloadable offline desktop app
  (PyInstaller + waitress launcher). **Potential future work**; not started,
  not scheduled.
- `PLAN_WHAT_IF.md` — "what if" scenarios: import a historical division (JSON
  bundle or ratings CSV) into a sandbox tournament, plus an Explore tab that
  hypothetically re-pairs any round with a chosen strategy. **Not started.**
