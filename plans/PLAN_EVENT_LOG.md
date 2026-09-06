# Plan: replayable tournament event log

Companion to `PLAN.md`, `PLAN_ROUND_ROBIN.md`, and `PLAN_RUST_CUTOVER.md`.
Code references pinned at `09b01d3` (the plan-file commits since add no code).
Written for a fresh session.

## Goal

Every state-changing action on a tournament — starting with its initial
configuration — is captured as an ordered, append-only event log that can be
**replayed** to reconstruct the tournament. Two payoffs:

1. **Simulation testing for all of Baxter**, not just the pairing engine: a
   fuzzer generates random-but-valid action sequences, invariants are checked
   after every step, and any failure *is* a replayable, committable repro.
2. **An audit log for real tournaments**: when something goes wrong in prod,
   download the log and replay it locally to reproduce the exact state at any
   point, with full information about who did what, when.

## Design

### Events capture intent, not effect

An event records a command's *inputs* (the validated arguments), never the
resulting rows. Derived state — generated pairings, materialized byes, round
statuses, standings — is recomputed at replay through the same code paths.
That's what makes replay a test: same inputs, is the derived state the same?
It also keeps the log small and the schema stable. (One deliberate exception:
simulated-result events record their randomly generated scores — see
Decisions, item 2.)

Consequence: replay fidelity requires derived state to be deterministic.
The pairing engine's randomness is the one source today; the per-division
`pairing_seed` from `PLAN_RUST_CUTOVER.md` (decided) fixes it. **Sequence this
plan's replay phases after that seed lands** (cutover Phase 2). Before then,
recording works fine but replays of Random-strategy rounds won't be
bit-faithful.

### Identity: natural keys, never pks

Payloads must survive replay into a fresh database, and editgrid's
wipe-and-recreate churns pks even in place. So payloads are pk-free:

- players by **name** (the engine is already name-keyed — same precedent)
- entrants by player name; seeding order by entrant `number`
- rounds by number; pairings by `(round, name1, name2)`
- results by their pairing's natural key
- divisions by name (rename events update the mapping as the log progresses;
  the replayer tracks current-name → division)

Grid DTOs gain a `to_portable`/`from_portable` pair per grid (entrant pk ↔
player name, etc.) — a few lines each for the five grids. Bonus: pk-free
payloads are human-readable, which the audit page wants anyway.

### The command layer (the crux)

Baxter's mutations are scattered across CBVs, editgrid `persist`, and helper
modules. The log requires a funnel — but a *logical* one, not a physical one.
Command bodies **stay in their natural domain modules**, per the project
convention (no services layer; helpers live where they belong):
`add_fixed_pairing`/`remove_fixed_pairing(s)` in `fixed_pairings.py`,
`publish_rounds`/`unpublish_rounds` in `generate_pairings.py`,
`simulate_match`/`simulate_round` in `match_simulation.py`, grid saves in the
grid flow. What makes a function a command is the `@records_event` decorator,
which also **registers it** (`event_type → callable`) for replay dispatch.

`tournaments/commands.py` is then a thin hub (~50–100 lines): the registry,
imports of the domain modules so decoration runs, and homes for the few
commands that have no module today (tournament/division CRUD currently inline
in `views.py`). It deliberately does not grow with the catalog; if its own
CRUD handful ever feels crowded, splitting is trivial since nothing couples
commands to one file. Views become thin: parse/validate → call command →
respond.

Each command runs inside `@records_event("event_type")`:

1. opens a transaction (or joins the caller's),
2. sets a contextvar marking "inside command X",
3. executes the wrapped function,
4. **appends the event in the same transaction** — log and state cannot
   diverge; failed commands leave no event.

`regenerate_pairings` and `materialize_byes` are *not* commands — they are
derived consequences and must not be logged (replay re-derives them).

### Event model

```python
class TournamentEvent(models.Model):
    tournament   = FK(Tournament, CASCADE, related_name="events")
    seq          = PositiveIntegerField()          # unique per tournament
    created_at   = DateTimeField(auto_now_add=True)
    actor        = FK(User, SET_NULL, null=True)   # null = anonymous
    actor_session = CharField(blank=True)          # hashed session key for anon
    division     = FK(Division, SET_NULL, null=True)  # convenience filter only;
                                                   # payload carries the name
    event_type   = CharField(choices=EVENT_TYPES)
    schema_version = PositiveSmallIntegerField(default=1)
    payload      = JSONField()
    digest       = CharField(blank=True)           # state digest after apply
    class Meta:
        unique_together = [["tournament", "seq"]]
```

- **Append-only**: no update/delete code paths, ever. Deleting a tournament
  cascades its log (acceptable; the export exists for post-mortems).
- **`seq` allocation**: `select_for_update` on the Tournament row inside the
  command transaction. Note the SQLite-dev vs Postgres-prod gotcha: verify
  behavior under both (SQLite serializes writes anyway; Postgres needs the
  lock).
- **`digest`**: sha256 of a canonical serialization of division state after
  the command (entrants, per-round status + published pairings as sorted name
  pairs + tables, results, standings tuple — no pks, no timestamps). Cheap at
  this scale, and it turns replay verification into "compare digests after
  each event", pinpointing the exact divergent event.

### Event catalog (from the mutation surface at `urls.py` / `views.py`)

| Event | Source | Payload sketch |
|---|---|---|
| `tournament_created` | TournamentCreateView | name, location, start_date, owner, editors |
| `tournament_updated` | TournamentUpdateView | changed fields + editors |
| `tournament_deleted` | TournamentDeleteView | — |
| `division_created` | DivisionCreateView | name, pairing_seed (randomly initialized at creation — must be recorded or a replayed division draws a different seed and random-strategy rounds diverge; the replayer sets it explicitly) |
| `division_renamed` / `division_deleted` / `division_restored` | respective views | old/new name |
| `division_settings_saved` | DivisionSettingsEditView, RoundPairings edit/preview accept | blocks / round_pairings JSON |
| `entrants_saved` | DivisionEntrantsEditView (grid) | portable rows `[{number, player}]` |
| `entrants_bulk_imported` | BulkImportEntrantsView | names list |
| `player_created` | CreatePlayerView (when invoked in-tournament context) | name, rating, provisional |
| `results_saved` | DivisionEditResultsView (grid) | portable rows |
| `result_added` / `result_edited` | ResultSlipCreateView | round, pairing names, winner, scores, verified flag |
| `fixed_pairings_saved` / `fixed_tables_saved` / `board_tables_saved` / `fixtures_saved` | grids | portable rows |
| `fixed_pairing_added` / `fixed_pairing_removed` / `fixed_pairings_removed` | AddFixedPairingView etc. | round, names / kept keys |
| `rounds_published` / `round_published` / `round_unpublished` | Publish*/Unpublish* views | round numbers |
| `pairing_seed_rerolled` | future reshuffle action | new seed |
| `match_simulated` / `round_simulated` | Simulate*View (dev tools mutate real state — log them) | round / pairing key + generated scores |

Out of scope (not tournament state): presence heartbeats, exports/downloads,
scorecards, player registry import (`PlayerImportView` — global, not
tournament-scoped), login/auth.

**Catalog is enforced, not aspirational** — see the completeness guard.

### Completeness guard

Two mechanisms so no mutation path silently escapes the log:

1. **Static**: a test walks `urlpatterns`, finds every view with a `post`
   method, and asserts it is either decorated as command-calling or in an
   explicit allowlist (presence, previews, exports). Adding a mutating view
   without an event type fails CI.
2. **Dynamic**: in DEBUG/tests, `pre_save`/`pre_delete` signal receivers on
   tournament-scoped models assert the "inside command" contextvar is set.
   Any ORM write outside a command raises immediately in development. (Not
   installed in prod — zero overhead there.)

### Replay harness

`tournaments/replay.py` + management command `replay_tournament`:

- Input: exported JSONL (header record: schema versions, git rev, recorded-at;
  then events in seq order) or live DB rows.
- Applies each event through the same command functions, bypassing HTTP.
  Actors map to stand-in users (created on demand); players resolve by name
  against the target DB, created with recorded rating when missing (the
  entrants/roster events carry what's needed).
- `--upto SEQ` stops after a prefix — "state of the tournament just before it
  went wrong". `--verify` compares the recorded digest after every event and
  reports the first divergence.
- Timestamps are not reproduced (excluded from digests); `created_at` on
  replayed rows is replay time. Recorded times live in the event log itself.

**Compatibility policy**: digest-exact replay is guaranteed only for logs
recorded by the same code version (header carries the git rev). Older logs
replay best-effort; when an event schema changes, bump `schema_version` and
add a small upgrade-on-read function per event type. Intent-level payloads
make this rare.

### Simulation testing

`fuzz_tournament` management command driving the command layer directly:

- Seeded RNG; weighted random ops respecting validity (add/edit entrants —
  including mid-tournament, save settings blocks, publish, enter/edit
  results, add/remove fixed pairings, unpublish, rename, simulate rounds).
  Every accepted op is a logged event, so **a failing run's artifact is its
  event log** — committable as a regression fixture and minimizable by
  bisection (drop events, re-run) as a stretch goal.
- Invariants after every op: no player paired twice in a round; published
  pairings immutable while published; round statuses consistent with slip
  counts; standings consistent with results; every entrant paired or byed in
  each published round; **and the meta-invariant: replaying the log so far
  into a fresh DB reproduces the digest**.
- CI runs a handful of fixed seeds (fast, deterministic); long random runs are
  manual/background. Hand-rolled fuzzer first; Hypothesis stateful testing is
  a possible later upgrade, not a dependency to take now.
- Rebuild `create_fake_tournament` (`fake_tournament.py`) on top of commands:
  fake tournaments are then born with a full event log and double as replay
  fixtures. (`simulate.py` — the engine-level simulator from the cutover plan
  — stays separate: it tests the engine, this tests Baxter.)

### Audit surface

- Per-tournament "Activity" page (owner/editors only): human-readable event
  list (actor, time, description rendered from type + payload), newest first,
  with a JSONL download link. Payloads are name-based, so rendering is
  straightforward.
- Anonymous actions (e.g. `ResultSlipCreateView` today) log with
  `actor=null` + hashed session key — this log **is** the audit trail that
  PLAN.md Phase 4's anonymous-edit hardening wants; implement recording
  before or with that phase.

### Pre-existing tournaments

Tournaments predating the log get a one-time synthesized
`state_snapshot` event (full portable state: config, entrants, settings,
results, statuses) as seq 1 when their next event is recorded (or via a
backfill command). Replay starts from the snapshot. New tournaments never
need snapshots; logs are a few hundred events at most, so no compaction.

## Phases

### Phase 1 — Model + recorder plumbing

`TournamentEvent` model + migration; `tournaments/events.py` with
`record_event()` (seq allocation, same-transaction append) and the
`@records_event` decorator + contextvar; digest function
(`division_digest()`) with unit tests (stable across pk renumbering — build
the same state twice with different pks, digests must match); the dynamic
DEBUG-mode signal guard (installed but permissive-logging until Phase 2
completes, then strict).

### Phase 2 — Command instrumentation

Decorate the existing mutation surface in place per the catalog (domain
modules keep their functions; `@records_event` + registration); create the
thin `tournaments/commands.py` hub with the registry and the extracted
tournament/division CRUD commands; extract `ResultSlipForm.save`'s write into
a command the form calls (replay can't drive a form); portable payloads for
the five grids (`to_portable`/`from_portable` next to the DTOs in `dto.py`);
wire `BaseEditGridView.post` to log via a grid hook so editgrid stays
domain-agnostic (the tournaments-side subclass supplies the event recording,
not `editgrid/`); anonymous actor capture; static completeness test over
`urlpatterns`; flip the dynamic guard to strict. Largest phase — commit per
view-cluster (tournament CRUD, division CRUD, grids, results, publishing,
fixed pairings) to keep diffs reviewable.

### Phase 3 — Export + audit page

JSONL export view (+ management command `export_event_log` for shell/dokku
use); the Activity page with rendered events and download; hashed session
keys for anonymous rows.

### Phase 4 — Replay harness (after the pairing seed exists)

`tournaments/replay.py`, `replay_tournament` command with `--upto`/
`--verify`; stand-in actor/player resolution; schema-version upgrade hook
(empty registry to start); tests: record a scripted tournament through
commands, export, replay into a clean DB, digests match end-to-end; replay a
`--upto` prefix and assert intermediate state.

### Phase 5 — Fuzzer + invariants

`fuzz_tournament` with seeded op generator and the invariant suite; CI job
with fixed seeds; failing-log artifact writing; rebuild
`create_fake_tournament` on commands. Stretch: bisection minimizer for
failing logs.

### Phase 6 — Adoption extras

`state_snapshot` synthesis/backfill for pre-existing tournaments; include the
event log in the registry-sync export bundle (`tournament_export.py`) so
audit history travels with the tournament.

## Sequencing

- Phases 1–3 (record + audit) have no dependencies — they can land any time
  and start accruing audit value immediately.
- Phase 4+ (replay fidelity) wants the persisted `pairing_seed`
  (`PLAN_RUST_CUTOVER.md` Phase 2). Recommended order: cutover Phases 1–2 →
  this plan fully → remaining cutover phases benefit from fuzzing under
  `PAIRING_ENGINE=shadow` (the fuzzer becomes a shadow-mode burn-in driver —
  free synergy).
- PLAN.md Phase 3 (dropouts) and future features add event types; the
  completeness guard forces that to happen at design time rather than as an
  afterthought.

## Decisions

Resolved by the owner (2026-07-14):

1. **Retention**: keep event logs forever. They're small; deleting a
   tournament still cascades its log (the export exists for post-mortems),
   but there is no other pruning path.
2. **Simulate-tools logging**: log `match_simulated`/`round_simulated`. They
   only occur in fake divisions inside real tournaments, and we may need to
   debug something users discover while playing with the simulations. Record
   the randomly generated scores **in the payload** — this is the one
   deliberate exception to intent-not-effect: the simulation RNG is unseeded,
   so replay must *apply* the recorded scores, never re-simulate.
3. **Audit page placement**: nav entry (owners/editors), per the
   recommendation.

## Verification

- `uv run python manage.py test tournaments.tests editgrid.tests users.tests`
  per phase; new tests as listed in each phase.
- Hands-on per phase 2/3/4: run a fake tournament end-to-end, check the
  Activity page, export, replay into a scratch DB (`--verify`), fuzz with a
  fixed seed.
- One commit per phase (Phase 2 per view-cluster); `jj describe` then
  `jj new`; confirm scope before committing a mixed working copy.
