# Code Review Fix Plan (2026-07-13)

Plan for fixing the issues found in the architecture/code review. Written for
implementation in a fresh session — file/line references are as of commit
09b01d3. Each phase is a separate jj commit (keep scopes unmixed; `jj new`
after each describe).

## Background: findings being fixed

1. **Swiss pairing infinite loop** (confirmed by repro): `tournaments/pairing/swiss.py:161`
   — `while len(groups.bottom) < 6: groups.merge_bottom()` spins forever when the
   field collapses to one group still under 6 players (e.g. 4 players in two
   win-groups after round 1). Reachable from a page render: `_autogenerate_pairable_rounds`
   runs lazily when the Pair Rounds tab renders, so opening the tab hangs the worker.
   The Rust port already has the fix (`scrabble-pairing/src/strategies/swiss.rs:195`
   breaks when `groups.length() <= 1`); it was never backported to Python.
2. **Entrants grid save destroys pairings/results**: `EntrantsGrid` uses the default
   `EditGrid.persist` (`editgrid/grids.py:154`) = delete-all + bulk_create. Deleting
   an Entrant ORM-cascades to `Pairing` (first/second, CASCADE) and `ResultSlip`
   (winner/loser, CASCADE), so ANY save of the Edit Entrants grid — including a
   no-op save or adding one late entrant — silently wipes every pairing and result
   in the division.
3. **ResultsGrid save resets submission timestamps**: same wipe-and-recreate persist;
   `created_at` (auto_now_add) is reset on every slip. Results export uses it as
   `submitted_on`; detail pages order by `-created_at`.
4. **Division create 500s on soft-deleted name collision**: `views.py:334-346` —
   `unique_together ["tournament", "name"]` spans soft-deleted rows but
   `DivisionCreateView` checks via the active-only manager → IntegrityError.
5. **`publish_rounds` not atomic** (`generate_pairings.py:54`): status flips to
   PUBLISHED before `materialize_byes`; a crash in between leaves a round that can
   never reach FINISHED without manual entry.
6. **`SECRET_KEY` insecure default** (`baxter/settings.py:28`); no secure-cookie /
   proxy-SSL-header settings for production.
7. **`PairingData.for_division` bare `except Exception`** (`pairing/base.py:109-112`).
8. Minor: `Repeats.get` defaultdict key bloat (`base.py:256`); unvalidated
   `int(data["round"])` 500s in datastar endpoints (`views.py:534,545,559,573`);
   magic `11` distance cap (`swiss.py:132`); DTO int-coercion inconsistency
   (`dto.py`); in-method `PermissionDenied` import (`views.py:338`).
9. Design gaps (owner-requested): support **late entrants** and **mid-tournament
   dropouts**; a **general preserve-unchanged-rows mechanism** for grid saves.
10. Hardening: anonymous visitors can edit any existing result by pk
    (`resultslip_edit`); engine keys on player names, so a duplicate name in a
    division silently corrupts `entrant_by_name` (`generate_pairings.py:154`).

**Both-engines policy**: until Baxter is cut over to the Rust crate, every pairing
engine change lands in both `tournaments/pairing/` and `scrabble-pairing/`.

---

## Phase 1 — Small correctness fixes (no schema changes)

### 1a. Swiss infinite loop — `tournaments/pairing/swiss.py:161`
- In `_pair_swiss_players`, mirror the Rust guard: inside the merge loop, break
  when `groups.length == 1` (merging has collapsed everything into one group
  that is still under 6). Keep the outer `if groups.length > 1` check.
- Regression tests in `tournaments/tests/test_pairing_algorithms.py`: Swiss
  round 2+ with fields of 2, 3, 4, and 5 players (the repro was 4 players over
  two win-groups; it hangs today).
- No Rust change needed. Verify parity on small Swiss fields; add a small-field
  case to the parity corpus (`scripts/export_pairing_corpus.py` /
  `scrabble-pairing/tests/parity.rs`) if none exists.

### 1b. Narrow the bare except — `pairing/base.py:109-112`
- `for_division` catches only `DivisionSettings.DoesNotExist` (matches every
  other call site). Malformed blobs are a write-time validation concern
  (`_validate_blocks`), not something to swallow at read time.

### 1c. Division create vs soft-deleted name — `views.py:334-346`
- Before creating, check `Division.all_objects.filter(tournament=..., name=name)`
  (same check `DivisionRenameView` does). Soft-deleted match → flash error
  ("that name belongs to a deleted division — restore or rename it") instead of
  500. Active match → keep current behavior (silent no-op or flash "exists").
- Move the `PermissionDenied` import to the top of the file.
- Test: create division, soft-delete it, POST same name → 200 + error message,
  no IntegrityError.

### 1d. Make `publish_rounds` atomic — `generate_pairings.py:54-71`
- Wrap status update + `materialize_byes` + `update_status` loop in
  `transaction.atomic()`.

### 1e. `Repeats.get` key bloat — `pairing/base.py:256`
- `return self.matches.get(key, 0)` instead of indexing the defaultdict
  (`pair_no_repeats_blossom` probes O(n²) pairs and permanently inserts each).
  No behavior change. Rust `Repeats::get` is already non-mutating — verify only.

### 1f. Datastar int parsing — `views.py`
- Helper `_read_int(data, key)` returning `(value, None)` or
  `(None, JsonResponse 400)`. Use in `PublishRoundView`, `UnpublishRoundView`,
  `AddFixedPairingView`, `RemoveFixedPairingView` (`views.py:534, 545, 559-561,
  573-574`). Malformed signals → 400 instead of 500.

### 1g. Name the Swiss distance cap — `swiss.py:132`
- Replace literal `11` with `MAX_PAIRING_DISTANCE = 10` (compare `<=`), comment
  distinguishing it from `SWISS_DISTANCE` (the SwissPlusRandom split point).
  Rust already names it `MAX_DISTANCE`.

### 1h. DTO coercion consistency — `dto.py`
- Give `ResultSlipDTO.from_json` / `EntrantDTO.from_json` the explicit
  `int(...)`/try-except style `FixedPairingDTO` uses, so string-typed numbers
  behave identically across grids.

### 1i. Settings hardening — `baxter/settings.py`
- Remove the `SECRET_KEY` default (missing `.env` fails at startup). Document in
  README/CLAUDE.md required-vars list.
- When `not DEBUG`: `SESSION_COOKIE_SECURE = True`, `CSRF_COOKIE_SECURE = True`,
  `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` (dokku nginx
  sets that header). Start without HSTS; note it as a later deploy decision.

---

## Phase 2 — Reconciling grid saves (general preserve-unchanged-rows mechanism)

Replaces wipe-and-recreate with match/update/create/delete in `editgrid`, keyed
on a per-grid natural key. Fixes the entrants cascade wipe (#2) and the
ResultsGrid `created_at` reset (#3) with one mechanism.

### 2a. Base mechanism — `editgrid/grids.py`
Add to `EditGrid`:
- `key_fields: tuple[str, ...] = ()` — model field names forming row identity
  (`("player_id",)` for entrants, `("pairing_id",)` for results). Empty tuple =
  current wipe-and-recreate, so untouched grids are unaffected.
- `update_fields: tuple[str, ...]` — the fields the grid manages (explicit
  per-grid tuple; simpler and more readable than deriving from columns).
- `can_delete(instance) -> str | None` — hook returning an error message when a
  row about to be removed must not be deleted.

New `persist` when `key_fields` is set (inside the existing `check_conflict`
transaction):
1. Load existing rows into `{key: instance}`.
2. For each prepared (unsaved) instance: key matches existing → compare
   `update_fields`; identical → untouched; different → copy values onto the
   existing instance, `save(update_fields=...)`. No match → collect for
   `bulk_create`.
3. Existing rows whose keys aren't in the payload → delete (guards already ran,
   see 2c).
- Duplicate-key rows in the payload = validation error (generic check; the
  entrant DTO already catches its case).

### 2b. Unique-constraint collision on updates
- `Entrant` has `unique_together (division, number)`; swapping two players'
  numbers violates it mid-update on SQLite (non-deferrable). Two-pass update:
  first set every to-be-updated row's colliding unique field to a temporary
  out-of-range value (negative numbers are safe — no check constraint), then
  apply final values. Gate on a new `unique_within_parent` grid attribute
  (`("number",)` for entrants); grids without it skip the dance.

### 2c. Deletion guards run before the transaction
- The "may these rows be deleted" check goes in `prepare` (already
  pre-transaction, so failures don't bump the version): compute the
  would-be-deleted set there, run `can_delete`, return errors. `persist` just
  executes.

### 2d. Apply to concrete grids — `tournaments/grids.py`
- `EntrantsGrid`: `key_fields = ("player_id",)`, `update_fields = ("number",)`,
  `unique_within_parent = ("number",)`. `can_delete`: entrant with any
  `pairings_as_first/second` or `wins/losses` rows → "NAME has pairings or
  results — mark them as dropped instead of removing them" (dropped flag arrives
  in Phase 3; until then say "cannot be removed"). Entrants with no dependents
  delete normally (registration-period fixes).
- `ResultsGrid`: `key_fields = ("pairing_id",)` (prepare already resolves the
  pairing), `update_fields = ("round", "winner_id", "winner_score", "loser_id",
  "loser_score", "winner_started")`. `created_at` never touched on update →
  export timestamps survive edits. Deleting a row stays allowed ("entered in
  error" case); `after_save` already refreshes round statuses.
- `FixedPairingsGrid` / `FixedTablesGrid`: leave on wipe-and-recreate (no
  dependents, no timestamps). `BoardTableMapGrid` is a JSON blob — unaffected.

### 2e. Tests — `editgrid/tests.py` + `tournaments/tests/test_views.py`
- Entrants: save with pairings+results present → intact; add one late row →
  only one new Entrant, existing pks unchanged; remove no-results entrant →
  deleted; remove entrant with results → 400 with guard message, nothing
  changed, version not bumped; renumber-swap two entrants → succeeds.
- Results: edit one score → other rows keep pk and `created_at`; delete a row →
  round status recomputed.
- Concurrency: stale version still 409s before any write.

---

## Phase 3 — Late entrants and mid-tournament dropouts

Phase 2 makes adding a late entrant safe at the DB level; this phase makes the
engine handle late adds and withdrawals. Every engine change lands in BOTH
engines.

### 3a. Schema — `tournaments/models.py` + migration
- `Entrant.dropped = models.BooleanField(default=False)`. A boolean suffices:
  finished rounds are never re-paired, so dropped only means "exclude from all
  pairing from now on". (Considered `withdrawn_after_round`; adds unused
  precision and a footgun if it disagrees with actual results.)

### 3b. Engine: PairingData — `pairing/base.py`
- `EntrantData` gains `dropped: bool = False`; `for_division` populates it.
- `seedings()` excludes dropped entrants.
- `standings_after_round()`:
  1. Exclude dropped players (by name, same as the bye filter). Their results
     still count for everyone else's repeats/spread; they're just unpairable.
  2. Append zero-record `Player`s for active entrants with no result slips yet.
     (Today a late entrant never appears in results-derived standings and
     silently never gets paired.) Appended at the bottom in seeding (rating)
     order among themselves. Behavior-neutral for existing tournaments (every
     entrant normally has slips for finished rounds) — parity corpus confirms.
- Bye logic needs no change: `bye_pairing` recomputes parity from filtered
  standings each round, so a dropout flipping even→odd just starts producing
  byes.

### 3c. Engine: round-robin and quad blocks
- A roster change inside a played RR block already raises `PairingError` from
  `_identify_template`; add an up-front check so a dropped entrant in an active
  RR/quad block raises a clear message ("NAME withdrew mid-round-robin — round
  robins can't re-pair around a withdrawal; convert the remaining rounds to
  another strategy or enter forfeits"). Proper forfeit handling inside RR blocks
  is out of scope.

### 3d. Regeneration trigger — `tournaments/grids.py`
- `EntrantsGrid.after_save`: if roster membership or any `dropped` flag changed,
  delete the division's DRAFT `RoundPairings` (published/finished untouched).
  The existing lazy `_autogenerate_pairable_rounds` re-pairs on the next Pair
  Rounds render. (Do NOT regenerate inside the save transaction — a
  `PairingError` would poison the grid save.) Published-but-unplayed rounds are
  handled by the existing unpublish action.

### 3e. UI
- Entrants edit grid: "Dropped" column (`kind="choice"`,
  `values={False: "", True: "Dropped"}`, `value_type="bool"`, `new_row=False`) —
  same pattern as `winner_started` in ResultsGrid. `EntrantDTO` gains the field;
  `update_fields` gains `"dropped"`.
- `DivisionEntrantsView` + standings template: annotate dropped entrants
  ("withdrew"). Dropped players still appear in standings (their results are
  real); they just carry the marker.
- `DivisionScorecardsDownloadView` skips dropped entrants. Results export
  unaffected (played games still count).

### 3f. Rust mirror + parity — `scrabble-pairing/`
- `model.rs`: add `dropped` to the entrant input with `#[serde(default)]` so the
  existing corpus still parses. Mirror `standings.rs` changes (dropped filter +
  zero-record late entrants) and the RR guard in strategies.
- Extend the corpus + `tests/parity.rs`: late-add mid-Swiss; dropout mid-Swiss
  (even→odd, bye rotation starts); dropout mid-RR (both engines report the same
  error).
- Run `cargo test` alongside the Django suite.

---

## Phase 4 — Access hardening (policy needs owner sign-off)

### 4a. Anonymous result editing — `views.py` `ResultSlipCreateView`
- Keep anonymous CREATE (player-submission flow, gated by verified-by-opponent
  checkbox). Restrict EDIT of an existing slip to: tournament editors, or the
  same browser session that created it (store created slip pks in
  `request.session`). Post-save "Edit" button and stale-page `_render_played`
  edit link keep working for the submitter. Also refuse anonymous edits once the
  round is FINISHED.

### 4b. Duplicate-name guard
- When adding an entrant (grid save or CSV import), reject a player whose name
  (case-insensitive) matches another entrant already in the division. Insurance
  against the `entrant_by_name` silent collision (`generate_pairings.py:154`).

---

## Order, verification, commits

- Work order: 1a first (hang reachable from a page render), rest of Phase 1,
  then 2, 3, 4. One jj commit per phase (or per item where natural); `jj new`
  after each describe.
- Per-phase verification: full suite
  `uv run python manage.py test tournaments.tests editgrid.tests users.tests`
  (530 tests passing at plan time). Phases 1a/3 additionally: `cargo test` in
  `scrabble-pairing/` + parity corpus.
- Hands-on pass with a fake tournament: 4-player Swiss division → Pair Rounds
  renders instead of hanging; drop a player mid-tournament → next round pairs 3
  with a bye; add a late entrant → they appear in the next pairable round.

## Open decisions (owner may override)

1. `dropped` boolean vs recording the withdrawal round (plan assumes boolean).
2. Phase 4a anonymous-edit policy (plan assumes session-based ownership +
   editor override + finished-round lockout).
