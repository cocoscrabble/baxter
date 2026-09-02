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
- `PLAN_COP.md` — port the COP (Cost-Optimized Pairing) algorithm from `COP.pm`
  (Perl, `jvc56/tournament_pairing_algorithms`) into the `scrabble-pairing` Rust
  crate as a new `COP` strategy: Monte Carlo contender simulation + weighted
  min-cost matching, plus new `DivisionSettings` fields (prizes, gibson spread,
  hopefulness, control-loss, sim counts). **Not started**; 5 phases, class prizes
  deferred.
- `PLAN_PLAYOFFS.md` — configurable 2/4/8-player championship playoffs with
  per-stage best-of-N series (issue #44), in postscript and concurrent timing
  modes, with a full placement bracket (third place, and 5th–8th for a top-8) so
  no eliminated player is left idle. The bracket is *derived* from the confirmed
  seed snapshot plus the division's results, so unnecessary games are never
  generated and replay reproduces placements. **Implemented** (all five phases):
  `tournaments/playoff.py` (derivation + lifecycle), playoff generation inside
  `generate_pairings.py`, the three playoff commands, the bracket and setup
  pages, and a generic `inactive_players` field on the Rust engine that lets a
  round be paired around reserved players.
- `PLAN_WHAT_IF.md` — "what if" scenarios: import a historical division (JSON
  bundle or ratings CSV) into a sandbox tournament, plus an Explore tab that
  hypothetically re-pairs any round with a chosen strategy. **Implemented** (all
  four phases): `whatif_import.py` + the `division_imported` command, the import
  view, and the Explore tab (`whatif.py`, `DivisionExploreView`) with a
  side-by-side actual-vs-what-if comparison.
- `PLAN_COCO_PROGRAM.md` — **program-level**: how Baxter and the central
  player/ratings DB (`../ratings`, the `cocodb` app) fit together. Holds the
  governing principles (Baxter never needs the central DB while a tournament is
  live; provisional players never enter it), the shared identity model
  (canonical zero-padded CoCo numbers, `T-` placeholders that block the export),
  the interchange contract (roster pull + native-file push), and live
  non-binding rating projections using the ratings project's own calculator.
  Read this before the two Baxter plans below; the ratings-side counterpart is
  `../ratings/plans/baxter-integration.md`. **Not started.**
- `PLAN_PLAYER_IDENTITY.md` — make `player_number` the key everywhere outside
  the database, replacing the player's *name*: unique (case-insensitive)
  numbers, the pairing/engine boundary and every event payload re-keyed,
  schema-version 2 payloads with v1 upgraders, a digest backfill, duplicate
  names finally made legal, and display disambiguation only where a name
  actually clashes. The `scrabble-pairing` crate needs no changes — it already
  treats `name` as an opaque key. **Not started**; 7 phases, 1–3 indivisible.
  **Prerequisite for `PLAN_ENTRANTS.md`.**
- `PLAN_ENTRANTS.md` — entrant management redesign (issue #47, folding in #6
  and #42): a WESPA rating alongside the CoCo one with a cascade, the rating
  *snapshotted* on the entrant so a mid-tournament sync can't reshuffle seeds,
  tentative/paid/playing-up registration state, a dedicated registration page
  beside the existing grid, an embeddable public entrants fragment for the CoCo
  site, and a `PlayerSource` seam standing in for the not-yet-built playerdb and
  the not-yet-known WESPA source. **Not started**; 5 phases plus an optional
  sixth. Depends on `PLAN_PLAYER_IDENTITY.md`, which lands first.
- `PLAN_PASSWORD_RESET.md` — self-service password reset by email. Gated on a
  **phase 0 go/no-go**: publish the domain's missing SPF and DKIM records,
  then prove the production host can send mail as `cocoscrabble.org` and have
  it land in an inbox — all before any code is written, since outbound mail
  under the domain's name is unauthenticated today and a reset flow that
  silently fails to deliver is worse than none. Then email as a unique/required
  identity field, transport config via Ansible, Django's four reset views, and
  a rate limit. **Not started**; phase 0 is a manual investigation, phases 1–5
  are ordinary work. (Password *change* for a signed-in user already exists and
  needs none of this.)
