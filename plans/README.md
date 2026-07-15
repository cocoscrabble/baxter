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
  Rust crate via a PyO3 extension. Phases 1–3 implemented (binding, adapter,
  parity); default engine still `python` pending burn-in.
- `PLAN_EVENT_LOG.md` — replayable append-only tournament event log.
  **Implemented** (all phases).
- `PLAN_ROUND_ROBIN.md` — fully general fixed pairings for round robins.
  **Not yet started**; sequenced after the Rust cutover (solver written once,
  in Rust).
