# Entrant Management Redesign (issue #47)

Plan for reworking entrant management: WESPA ratings (#6), tentative status
(#42), playerdb/CoCo-number integration, and payment tracking. Written for
implementation in a fresh session — file/line references are as of commit
`cf93873`. Each phase is a separate jj commit (keep scopes unmixed; `jj new`
after each describe).

> **ALL PHASES IMPLEMENTED.** Issues #47, #6 and #42 are covered: entrants carry
> registration state and a pinned rating, WESPA ratings feed the cascade, and
> the public list marks tentative and playing-up entrants with an accessible
> equivalent. What is *not* here, deliberately, is any concrete WESPA fetcher or
> playerdb HTTP client — see "Explicitly out of scope".

> **Depends on `PLAN_PLAYER_IDENTITY.md`.** That plan makes `player_number` the
> key everywhere outside the database, in place of the player's name. It lands
> **first**: every payload, engine call and lookup described below assumes the
> number-keyed world, and several problems this plan would otherwise have to
> work around (a guest and a member sharing a name; a registration page picker
> that can't tell two "John Smith"s apart) simply do not arise once it has
> landed.

## Background: what exists today

- `Player` (`tournaments/models.py:354`): `name`, `player_number`, `rating`
  (a single non-null int), `is_provisional`, `is_bye`. `player_number` is the
  identity everywhere outside the database once `PLAN_PLAYER_IDENTITY.md` has
  landed, with `T-` temp numbers minted locally (`next_temp_player_number`,
  `models.py:325`).
- `Entrant` (`models.py:425`): `division`, `player`, `number`, `dropped`.
  Nothing else — no registration state at all.
- Entry surfaces:
  - `EntrantsGrid` (`tournaments/grids.py:54`) — Tabulator grid over
    `number` / `player` / `dropped`, reconciled on `player_id` so pairings and
    results survive a save.
  - `CreatePlayerView` (`views.py:1469`) — AJAX create-a-player, **not**
    command-backed (on the `EXEMPT` list in
    `tests/test_event_completeness.py:65`).
  - `BulkImportEntrantsView` + `import_entrants.py` — CSV of `name[, rating]`.
  - `PlayerImportView` + `player_sync.py` — admin-only whole-roster JSON
    upsert, global and unlogged.
- Rating is read live off `Player.rating` at pairing time
  (`pairing/base.py:30`, `:438`, `:473`; `pairing/engine.py:51`;
  `match_simulation.py:25`; `simulate.py:110`) and for display ordering
  (`views.py:638`).
- No WESPA code, no playerdb/registry HTTP client. `tournament_export.py`
  builds the outbound bundle; the transport and the id-map application were
  never built.

## Design decisions

These were settled with the owner before writing; each is a decision, not a
default, so a later session should not relitigate them without asking.

1. **Extend in place.** `Player` and `Entrant` keep their tables, PKs and event
   log. No greenfield schema, no migration of existing tournaments beyond
   backfilling new columns.

2. **Two ratings, one cascade.** `Player.rating` *stays the CoCo rating* (0
   means "no CoCo rating") — no rename, so every existing payload key, export
   field, grid DTO and replay path keeps working. `Player.wespa_rating` is a
   new nullable int. Effective rating is
   `coco if coco else wespa if wespa is not None else 0`.

   `Player.rating` is therefore what the central roster pull writes: the CoCo
   rating is that database's, and Baxter mirrors it. WESPA stays independent of
   the sync and of the central database entirely.

3. **The entrant pins the rating.** `Entrant.rating` +
   `Entrant.rating_source` snapshot the effective rating when the player is
   entered. Seeding, display and replay read the snapshot; `Player.*` is free
   to drift under a running tournament without reshuffling pairings. A
   director may hand-edit the snapshot, which sets `rating_source = manual`
   and makes it immune to any later sync.

   *Consequence worth knowing:* because entrants pin their rating, a global
   rating refresh (WESPA pull, playerdb sync) mutates no replayable tournament
   state, so it stays an unlogged global action like `PlayerImportView`.

   *Added later:* a director may also re-pin chosen entrants from the player
   table (`tournaments/entrant_sync.py`, `/entrants/refresh-ratings/`). That is
   the deliberate opposite gesture and it **is** logged — it moves state the
   digest covers — as `entrant_ratings_refreshed`, carrying the seeds it wrote
   rather than an instruction to sync, since a replay reads a player table that
   has moved on. Manual ratings are still never touched, and the entrants page
   marks the drift for editors only.

4. **Guests are not a new kind.** A guest is simply a player with no CoCo
   number and no CoCo rating: a `T-` number, `is_provisional=True`, and a WESPA
   or manually-entered rating. No `is_guest` field, no `kind` enum. Their `T-`
   number is a first-class identity like any other (see
   `PLAN_PLAYER_IDENTITY.md`), so a guest sharing a name with a member is an
   ordinary, supported case.

5. **Tentative is its own field, defaulted from payment.** `Entrant.tentative`
   is set/cleared by an organizer (issue #42). Marking an entrant paid clears
   `tentative` by default; the organizer can override in either direction.

6. **Payment is a flag plus a note.** `Entrant.paid` (bool) and
   `Entrant.payment_note` (text). No amounts, methods, or fee schedule —
   deliberately the smallest thing that supports the tentative/unpaid
   convention. Payment fields are **editor-only** and never rendered on a
   public page.

7. **Playing up is manual.** `Entrant.playing_up` (bool), ticked by the
   organizer. No rating bands on `Division` — the carat is a judgement call.

8. **Grid stays, registration page is added.** `EntrantsGrid` remains the bulk
   surface (seat numbers, dropped, quick flag edits). A new per-entrant
   registration page becomes the primary *add* flow: player search, guest
   creation, rating, tentative/paid/playing-up, note.

9. **The public list is an embeddable HTML fragment.** The CoCo website
   embeds a chrome-free fragment, so the display conventions (italic WESPA
   rating, `*` tentative, `^` playing up, legend) live in Baxter.

10. **Player creation is logged.** `CreatePlayerView` becomes command-backed
    and records the catalog's currently-unused `player_created` event, so
    replay into a fresh DB recreates the player with its full field set.

11. **External sources are seams, not clients.** The WESPA source is unknown
    and the playerdb API does not exist yet. Define one internal interface with
    a local implementation; leave both wire protocols to a later plan.
    *(Since settled: `plans/PLAN_WESPA.md` is that later plan for WESPA. The
    source turned out to be one bulk JSON document, and the CSV stub this
    decision produced has been replaced by it.)*

---

## Phase 1 — Model, migration, and the rating cascade — **IMPLEMENTED**

### 1a. Fields

`Player`:
```python
wespa_rating = models.IntegerField(null=True, blank=True)
```

`Entrant`:
```python
COCO, WESPA, MANUAL, NONE = "coco", "wespa", "manual", "none"
rating = models.IntegerField(default=0)          # snapshot, see decision 3
rating_source = models.CharField(max_length=8, choices=..., default=NONE)
# The rest of the rating seed, frozen with the rating. The live projection
# (PLAN_COCO_PROGRAM decision 6) needs all four: RatingsCalculator damps by
# career games, and deviation grows with time since last_played.
deviation = models.FloatField(default=0.0)
career_games = models.IntegerField(default=0)
last_played = models.DateField(null=True, blank=True)
tentative = models.BooleanField(default=False)
paid = models.BooleanField(default=False)
payment_note = models.TextField(blank=True, default="")
playing_up = models.BooleanField(default=False)
```

Every new field has a default, so `Division.bye_entrant()` (`models.py:189`)
keeps working untouched: the bye entrant gets `rating=0`, `rating_source=none`.

The three new seed fields come from the central roster pull
(`ratings.CurrentRating`, which holds exactly `rating`, `deviation`,
`career_games`, `last_played`). Until that pull exists they are simply zero /
null, which the calculator treats as an unrated player — so the schema can land
well before the sync does.

### 1b. The cascade, in one place

```python
class Player:
    @property
    def effective_rating(self):
        """(rating, source) — CoCo, else WESPA, else 0. See decision 2."""
```

Nothing else may re-derive this. `Player.rating == 0` is the sole test for
"no CoCo rating".

### 1c. Snapshotting helper

Add `Entrant.enter(division, player, number, **registration)` — a classmethod
that snapshots `effective_rating` into `rating`/`rating_source` unless an
explicit rating is passed (which sets `manual`). Do **not** snapshot inside
`save()`; the magic would fire on every unrelated write.

Update every entrant-creation site to go through it:
`import_entrants.py:137`, `grids.py` (`EntrantsGrid.prepare` /
`from_portable`), `fake_tournament.py`, `fuzz.py:47`, `whatif_import.py`.

### 1d. Data migration

Backfill existing entrants: `rating = player.rating`,
`rating_source = "coco" if player.rating else "none"`, all booleans `False`,
`payment_note = ""`. `Player.wespa_rating` starts `NULL` everywhere.

**Verification:** `test_models.py` — cascade for all four combinations
(CoCo+WESPA, CoCo only, WESPA only, neither); `Entrant.enter` snapshots each
source correctly; an explicit rating yields `manual`; the bye entrant is still
creatable. Run the migration against a copy of `db.sqlite3` and confirm entrant
count and ratings are unchanged. Remember the Postgres/SQLite gotcha — no new
indexes here, so this should be clean, but check `sqlmigrate` output.

### What landed

Fields, cascade, `Entrant.enter`, and migrations `0039` (schema) + `0040`
(backfill). Every entrant-creation site goes through `enter`; the edit grid,
which builds instances rather than calling `create`, pins the snapshot in
`prepare` instead — deriving it from the player, never from the client, and
leaving an existing entrant's pinned rating alone so a hand-edit survives a
grid save.

**The dev-database run earned its place in this plan.** It caught a bug the
unit tests could not: the phase 3 digest backfill (then migration `0038`)
replays through the *live* models, so adding entrant fields after it made every
replay die on `no such column: tournaments_player.wespa_rating` and silently
skip every tournament, leaving the digests at v1 forever. Its own comment had
predicted exactly this — "safe here only because ... it runs last".

Fixed three ways, since nothing was deployed yet:

1. The backfill is renumbered `0041`, after the entrant schema.
2. `digest_backfill.schema_mismatch()` compares the live models against the
   database's columns, and the migration now *refuses* on a mismatch rather
   than skipping — a backfill that quietly does nothing is worse than one that
   stops the deploy and says what to run.
3. `manage.py backfill_event_digests` exists for that case, and is safe to
   re-run.

Verified on a copy of the real dev database: 102 entrants unchanged, every
rating pinned to its player's (99 `coco`, 3 `none`), all new fields at
defaults; and the digest backfill rewrote 4 tournaments and correctly refused
the one whose log was already divergent. The whole chain also applies on
Postgres 18.2, with `sqlmigrate` showing plain `ADD COLUMN`s and no indexes.

---

## Phase 2 — Pairing and replay read the snapshot — **IMPLEMENTED**

### 2a. Seeding

- `PlayerData.from_db` (`pairing/base.py:28`) takes a `Player` today. Change it
  to build from an `Entrant` (or add `from_entrant`) so `rating` is the
  snapshot. `PairingData.for_division` is the only caller that matters.
- `seedings` (`base.py:438`) and the late-entrant sort (`base.py:473`) sort on
  the snapshot.
- `pairing/engine.py:51` sends `e.rating`, not `e.player.rating`, to Rust.
- `match_simulation.py:25` and `simulate.py:110` use the snapshot.
- `views.py:638` (`DivisionEntrantsView`) orders by `-rating`, not
  `-player__rating`.

No Rust change: the engine already receives a rating per entrant and is
indifferent to where it came from.

### 2b. Event log, digest, replay

- `EntrantsGrid.to_portable` / `from_portable` (`grids.py:84`, `:103`) carry
  the new fields. Keep the existing `player` + `rating` keys exactly as they
  are so old logs still replay; read the new keys with defaults.
- `division_digest` (`events.py:256`) extends its entrant tuple from
  `[number, name, dropped]` to include `rating`, `rating_source`, `tentative`,
  `paid`, `playing_up`. `payment_note` stays out — free text that no invariant
  depends on. This makes the new state part of the replay invariant.
- `events.py:428` (the log's entrant serialization) carries the same fields.
- `fuzz.py`: teach the fuzzer to flip `tentative` / `paid` / `playing_up` and
  to hand-edit a rating, so the meta-invariant (replay reproduces the digest)
  actually covers them.

**Verification:** `test_replay.py` round-trips a division with mixed rating
sources and registration flags; `test_fuzz.py` passes with the new mutations;
`test_engine_adapter.py` asserts the snapshot (not the live player rating)
reaches the engine — mutate `Player.rating` after entry and assert pairings are
unchanged.

### What landed, and what the fuzzer found

`PlayerData.from_db` became `from_entrant`, so everything downstream of it —
seeding, the engine boundary, simulation, the entrants page's seed order — reads
the pinned rating without knowing it changed. The DTO, portable payload, digest
and snapshot carry the new fields, and the fuzzer flips the flags and hand-edits
ratings.

**Teaching the fuzzer those mutations immediately found a real bug, and fixing
the fuzzer found two more.**

- **Stale drafts survived a rating edit.** `EntrantsGrid._roster_signature` was
  `(player_id, dropped)` — commented as "what the pairing engine keys off",
  which stopped being true the moment the entrant pinned its rating and this
  grid could edit it. A director who corrected a rating and then published got
  a round paired off the *old* one, silently; the fuzzer surfaced it as a bye
  handed to the wrong player. The rating is now part of the signature, pinned by
  `RatingEditInvalidatesDraftsTests`.

- **The v1 digest must stay a three-element entrant tuple.** Extending it for
  both versions made every pre-existing tournament fail the backfill's
  verification pass and be skipped — caught by re-running the migration against
  the real dev database, not by any test.

- **The log has to record what was *persisted*.** `on_saved` is handed the
  client's rows, which cannot contain a server-derived snapshot, so
  `to_portable` reads the entrants back from the database. And replay restores
  the recorded `rating_source` verbatim rather than re-deriving it — a recorded
  `(0, "none")` must not come back as the `manual` that carrying a rating
  otherwise implies.

Two incidental fixes fell out:

- `_random_outcome` divided by zero for two unrated entrants — a state that is
  now perfectly ordinary (`rating_source = none` pins 0). It is a coin flip.
- The fuzzer had been posting playoff seeds by *name* since the identity plan
  moved that form to keys, so its playoff coverage had quietly become a no-op.
  It posts keys now.

---

## Phase 3 — Editing surfaces — **IMPLEMENTED**

### 3a. Registration page (primary add flow)

New editor-only view + route, `division_register`
(`/t/<slug>/d/<slug>/register/`), reached from the entrants page and the edit
grid.

- **Search**: autocomplete over players, showing name, CoCo number, CoCo
  rating, WESPA rating. Served through the Phase 5 seam so it can later hit the
  playerdb instead of the local table.
- **Add existing**: pick a player → seat number (auto-next), rating snapshot
  prefilled from the cascade and editable, tentative / paid / playing-up
  checkboxes, payment note.
- **Create guest**: name + WESPA rating (and/or a manual rating), which mints a
  `T-` number and a provisional player, then enters them — one action, one
  page (decision 4).
- **Edit one entrant**: same form over an existing entrant, so a director can
  confirm a tentative player, mark them paid, or fix a rating without touching
  the grid.
- Marking paid clears `tentative` unless the organizer explicitly re-ticks it
  (decision 5) — implement as a form-level default, not a model-level
  side effect, so the override is representable.

### 3b. Commands

Three new `@records_event` commands in `tournaments/commands.py`, all
keyed on `player_number` (per `PLAN_PLAYER_IDENTITY.md`; `player` below is a
number, never a name):

| event | payload |
| --- | --- |
| `player_created` | `player_number`, `name`, `rating`, `wespa_rating` |
| `entrant_added` | `division`, `player`, `number`, `rating`, `rating_source`, `tentative`, `paid`, `playing_up`, `payment_note` |
| `entrant_updated` | `division`, `player`, + the changed registration fields |

- Register all three in the catalog (`events.py:40`) — `player_created`
  already has a slot; wire it rather than adding a fourth name.
- `player_created` replays as "create this player if no player with that
  *number* exists". The `resolve_player` fallback in `from_portable`
  (`grids.py:38`, number-keyed after the identity plan) stays as a safety net
  for logs recorded before this phase.
- Remove `CreatePlayerView` from `EXEMPT` in `test_event_completeness.py` and
  add it, plus the new registration view, to `COMMAND_BACKED`.
- Add activity-page descriptions for the three events (`events.py:533`).

### 3c. Grid

`EntrantsGrid` gains columns and `EntrantDTO` (`dto.py:68`) gains fields:

```
number | player | rating | source | Tent. | Paid | Up | Dropped
```

- `rating` — `Column(kind="number")`. Editing it sets `rating_source=manual`
  server-side in `prepare`.
- `source` — `kind="display"`, read-only.
- `tentative` / `paid` / `playing_up` — bool choice columns, same shape as the
  existing `dropped` column (`grids.py:74`).
- `payment_note` is **not** a grid column; it lives on the registration page.
- `lookups` (`grids.py:120`) sends CoCo + WESPA + effective rating per player,
  so a row added in the grid prefills its snapshot client-side. `prepare`
  re-derives the snapshot server-side for any row that arrives without one —
  the client is never trusted for it.
- `update_fields` extends to the new columns so grid edits actually persist
  (today it is `("number", "dropped")`, `grids.py:69`).

**Verification:** `test_views.py` covers add-existing, create-guest, and edit
through the registration page, plus the paid→tentative default and its
override; `test_events.py` asserts the three events' payloads;
`test_event_completeness.py` passes with the updated sets; a grid save
round-trips every new column.

### What landed

The registration page (`division_register`), the three commands, and the grid
columns, with `tournaments/player_source.py` holding the phase 5 seam early
because 3a needs it. Tests are in `test_registration.py` (26 of them, including
a full replay of an add + guest + edit session). `CreatePlayerView` is now
command-backed and off the exempt list.

**Three bugs came out of driving it in a browser rather than only in tests.**

- **The same rating box means two different things, and both had to be told
  apart from "unchanged".** The edit form prefills the current rating and the
  grid round-trips it, so treating a *present* value as a hand-edit converted
  every entrant to ``manual`` on any save — silently making the whole division
  immune to a later sync. Only a rating that actually *differs* is an override
  now. The grid case is pinned by `RatingEditInvalidatesDraftsTests`, whose row
  helper deliberately sends the rating exactly as the client does; without that
  fidelity the test passed against the bug.

- **The registration fieldset appears twice on one page**, so the guest copy
  needs a form prefix. Without it both halves render identical element ids and
  the guest form's labels quietly point at the add form's inputs — visible in
  the accessibility tree as three unlabelled checkboxes.

- The page rendered `messages` itself, duplicating what the base template
  already shows.

One deviation: `PlayerSource` (phase 5's seam) was built here rather than
later, because 3a's search and guest creation are specified to go through it and
writing direct queries first would only mean rewriting them. WESPA and the
registry-backed implementation stay in phase 5.

---

## Phase 4 — Public display and the embeddable fragment — **IMPLEMENTED**

### 4a. Display conventions

On `division_entrants.html`, per the owner's display requirements:

1. A rating whose `rating_source` is `wespa` renders in italics.
2. `rating_source == "none"` renders `0`.
3. `tentative` appends `*` to the name.
4. `playing_up` appends `^` to the name.

Plus a legend (`* Tentative`, `^ Playing up`, `italic = WESPA rating`) and, for
accessibility, a visually-hidden textual equivalent on each marked row so the
status never depends on punctuation alone (#42's acceptance criteria).
`paid` and `payment_note` are rendered **only** when `can_edit`.

### 4b. Embeddable fragment

New route `/t/<slug>/d/<slug>/entrants/embed/` rendering the same table with no
site chrome, no nav, and inline-safe styling.

**An embedded view contains strictly what a signed-out visitor would get, and
nothing more** (settled with the owner). An iframe is loaded by the *visitor's*
browser carrying the visitor's cookies, so a director browsing the CoCo site
while signed into Baxter must be served the same bytes as everyone else. Two
separate things enforce that and both are needed: the division is resolved as
anonymous (`PubliclyVisibleDivisionMixin`), and `can_edit` is forced False.

- Django's `XFrameOptionsMiddleware` defaults to `SAMEORIGIN`, so this view
  needs `@xframe_options_exempt` — otherwise the CoCo site's iframe renders
  blank. Note it explicitly in the view docstring.
- Editor-only fields are never included, regardless of who is logged in.
- A test division is a 404 **even for the editor who owns it** — the ordinary
  division page still shows it to them, so this is the embed's rule rather than
  a change to what an editor may see.

**Verification:** `test_views.py` — a tentative entrant's name carries the
asterisk and the accessible text; a confirmed entrant carries neither; a
WESPA-sourced rating is italicised; `paid`/`payment_note` are absent for an
anonymous request to both the page and the fragment; the fragment's response
has no `X-Frame-Options: SAMEORIGIN`.

### What landed

`_entrants_table.html` holds the conventions once and both surfaces include it,
so the page and the fragment cannot drift. Tests are in `test_registration.py`;
the fragment was also loaded in a browser and reads correctly with no chrome.

Three decisions worth recording:

- **The legend only explains markers that are on the page.** A table with no
  tentative entrant should not tell the reader what an asterisk would have
  meant. The view computes the three flags; the template renders what is true.

- **Nothing in the fragment varies by viewer**, which is the rule the owner
  settled: an embed contains strictly what a signed-out visitor would get.
  Payment is absent even for a signed-in editor (``can_edit`` forced False,
  explicitly rather than merely unset), and a test division is a 404 even for
  its own organizer (`PubliclyVisibleDivisionMixin`). The strongest test simply
  fetches the page twice, signed in and signed out, and asserts the bytes are
  identical.

  The first cut only did the ``can_edit`` half, and its test passed by accident:
  nothing else sets ``can_edit`` on that view, so leaving it unset looked the
  same. Both halves are pinned by tests confirmed to fail without them.

- **The fragment inlines its own styles.** The embedding site has no reason to
  carry Baxter's stylesheet, and a fragment that only renders correctly when
  someone else's CSS happens to be present is not really embeddable.

Also fixed in passing: the entrant list had still been showing
``entrant.player.rating`` — the live rating — rather than the pinned snapshot
the division was seeded from. A phase 2 miss.

---

## Phase 5 — The external-source seam — **IMPLEMENTED**

One interface, one local implementation, no network code:

```python
class PlayerSource:
    def search(self, query) -> list[PlayerRecord]: ...
    def fetch(self, player_number) -> PlayerRecord | None: ...
    def mint_number(self, player) -> str | None: ...   # None = keep the T- number
```

- `LocalPlayerSource` reads the `Player` table and mints `T-` numbers exactly
  as today. It is the configured default.
- The registration page's autocomplete and the create-guest flow both go
  through the seam, so swapping in a playerdb-backed source later changes no
  view code.
- **The concrete implementation is no longer unknown.** `PlayerSource` is
  backed by the central roster pull specified in `PLAN_COCO_PROGRAM.md` — an
  authenticated JSON endpoint plus an equivalent downloadable snapshot, both
  returning `player_number`, `name`, `rating`, `deviation`, `career_games`,
  `last_played`. `LocalPlayerSource` remains the offline default and the thing
  tests run against.
- **WESPA**: add `wespa_ratings.py` with a `refresh_wespa_ratings(rows)`
  upsert setting `Player.wespa_rating`, plus an admin-only upload page modelled
  on `PlayerImportView`. Match on `player_number` when the source supplies one.
  Matching on name is a **fallback only** and must follow the same rule as the
  CSV entrant import: a name matching exactly one player updates it; a name
  matching several updates none and is reported, since WESPA has no idea which
  "John Smith" it means. The *fetcher* is a
  documented stub — the source (bulk file? per-player lookup? URL? format?) is
  not yet known, and this plan deliberately does not invent one.
- **playerdb**: the wire protocol is out of scope (decision 11). Record in this
  plan that the existing `T-`/`is_provisional` reconciliation
  (`tournament_export.py:7`) is the intended landing spot for minted numbers.

**Verification:** `test_player_sync.py` gains WESPA upsert cases (new rating,
updated rating, unmatched name is a no-op); a fake `PlayerSource` drives the
registration page's search in `test_views.py`.

### What landed

`player_source.py` (the interface + `LocalPlayerSource`) went in with phase 3,
since the registration page needs it. This phase adds `wespa_ratings.py` and the
admin-only upload page, plus the seam tests: a fake `PlayerSource` drives the
search, the add flow and guest minting, proving no view reaches around it.

The fetcher stays a documented absence, as decided. Where WESPA ratings come
from is not settled — bulk file, per-player lookup, some URL, some format — so
the module takes rows that are already parsed and the CSV upload is the concrete
way in. Inventing a protocol would only have to be undone.

Two things the tests pin that are easy to get wrong later:

- **An ambiguous name updates nobody.** WESPA cannot say which "John Smith" it
  means, and a wrong rating is worse than a missing one. The rows are listed by
  name rather than counted, because "3 names were ambiguous" is not something
  anyone can act on.

- **A refresh cannot move a pinned entrant rating** — which is exactly why this
  can stay an unlogged global action. There is a test that enters someone,
  refreshes the roster underneath them, and asserts their snapshot did not
  budge.

---

## Phase 6 (optional) — Export the snapshot — **IMPLEMENTED**

`ExportEntrant` (`tournament_export.py:41`) carries `rating` and
`rating_source`, so the registry can see the rating the tournament was actually
seeded from rather than re-deriving it from a since-drifted player record.
Additive; no consumer exists yet, so this can be dropped or deferred without
affecting anything else.

### What landed

Both fields, defaulted so an older consumer reading the bundle is unaffected.
The tests make the distinction concrete: change the player's rating after entry
and the bundle still reports the seed under `entrants`, while `players` reports
today's — two different questions, answered separately.

---

## Explicitly out of scope

- Fee schedules, amounts, payment methods, or a payment ledger (decision 6).
- Rating bands on `Division` / programmatic playing-up detection (decision 7).
- Any concrete WESPA fetcher or playerdb HTTP client (decision 11).
- Self-service player registration (nothing in #47, #6 or #42 asks for it).
- Renaming `Player.rating` to `coco_rating` (decision 2).
