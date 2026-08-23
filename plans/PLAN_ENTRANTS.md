# Entrant Management Redesign (issue #47)

Plan for reworking entrant management: WESPA ratings (#6), tentative status
(#42), playerdb/CoCo-number integration, and payment tracking. Written for
implementation in a fresh session — file/line references are as of commit
`cf93873`. Each phase is a separate jj commit (keep scopes unmixed; `jj new`
after each describe).

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

3. **The entrant pins the rating.** `Entrant.rating` +
   `Entrant.rating_source` snapshot the effective rating when the player is
   entered. Seeding, display and replay read the snapshot; `Player.*` is free
   to drift under a running tournament without reshuffling pairings. A
   director may hand-edit the snapshot, which sets `rating_source = manual`
   and makes it immune to any later sync.

   *Consequence worth knowing:* because entrants pin their rating, a global
   rating refresh (WESPA pull, playerdb sync) mutates no replayable tournament
   state, so it stays an unlogged global action like `PlayerImportView`.

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

---

## Phase 1 — Model, migration, and the rating cascade

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
tentative = models.BooleanField(default=False)
paid = models.BooleanField(default=False)
payment_note = models.TextField(blank=True, default="")
playing_up = models.BooleanField(default=False)
```

Every new field has a default, so `Division.bye_entrant()` (`models.py:189`)
keeps working untouched: the bye entrant gets `rating=0`, `rating_source=none`.

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

---

## Phase 2 — Pairing and replay read the snapshot

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

---

## Phase 3 — Editing surfaces

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

---

## Phase 4 — Public display and the embeddable fragment

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

- Django's `XFrameOptionsMiddleware` defaults to `SAMEORIGIN`, so this view
  needs `@xframe_options_exempt` — otherwise the CoCo site's iframe renders
  blank. Note it explicitly in the view docstring.
- Editor-only fields are never included, regardless of who is logged in.
- Respects `VisibleDivisionMixin` (test divisions stay 404).

**Verification:** `test_views.py` — a tentative entrant's name carries the
asterisk and the accessible text; a confirmed entrant carries neither; a
WESPA-sourced rating is italicised; `paid`/`payment_note` are absent for an
anonymous request to both the page and the fragment; the fragment's response
has no `X-Frame-Options: SAMEORIGIN`.

---

## Phase 5 — The external-source seam

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

---

## Phase 6 (optional) — Export the snapshot

`ExportEntrant` (`tournament_export.py:41`) carries `rating` and
`rating_source`, so the registry can see the rating the tournament was actually
seeded from rather than re-deriving it from a since-drifted player record.
Additive; no consumer exists yet, so this can be dropped or deferred without
affecting anything else.

---

## Explicitly out of scope

- Fee schedules, amounts, payment methods, or a payment ledger (decision 6).
- Rating bands on `Division` / programmatic playing-up detection (decision 7).
- Any concrete WESPA fetcher or playerdb HTTP client (decision 11).
- Self-service player registration (nothing in #47, #6 or #42 asks for it).
- Renaming `Player.rating` to `coco_rating` (decision 2).
