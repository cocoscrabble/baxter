# Player Identity: `player_number` becomes the key

Prerequisite for the entrant redesign (`PLAN_ENTRANTS.md`, issue #47). Baxter
currently keys players by **name** everywhere outside the database: the pairing
engine, the event log, the digest, the playoff seed snapshot, and every import
path. Names are not unique in the real world, and Baxter only avoids the
collision today by *refusing* to create a second player with the same name —
which is not a fix, it is a deferral.

Written for implementation in a fresh session — file/line references are as of
commit `cf93873`. Each phase is a separate jj commit (keep scopes unmixed;
`jj new` after each describe).

## Background: where name is the key today

The **database is already id-based** — `Pairing`, `ResultSlip` and
`FixedPairing` all hold `Entrant` FKs. The name-keying lives in the layers
above:

| Layer | Where | What breaks with duplicate names |
| --- | --- | --- |
| Rust engine | `PlayerData.name`, `winner_name`/`loser_name`, name-keyed `HashMap`s throughout `pair.rs`, `cop.rs`, `swiss.rs`, `roundrobin.rs` | Two entrants collapse into one map entry — silently wrong pairings |
| Engine adapter | `pairing/engine.py:51`; `generate_pairings.py:291` `entrant_by_name` | Engine output can't be mapped back to the right entrant |
| Pairing layer | `PlayerData` (`base.py:24`), `Player` (`base.py:231`), `Repeats._key` (`:335`), `Starts._record` (`:356`) | Repeat and start ledgers merge two people |
| Event log | every payload (`commands.py:205` `_entrant`, `:250` `_find_pairing`, `:262` `_write_result`), `division_digest` (`events.py:256`) | Replay resolves to the wrong player, or ambiguously |
| Stored state | `Playoff.seeds` (`models.py:725`) — JSON keyed by name | Seed snapshot can't identify its qualifier |
| Imports | `import_entrants.py:88`, `grids.py:38` `resolve_player`, `player_sync.py` (keyed on number already) | Wrong player matched |
| Guards | `Player.create_unique` (`models.py:388`), `EntrantsGrid._duplicate_name_errors` (`grids.py:153`) | These *are* the deferral — both refuse the legitimate case |

Two pieces of luck are worth knowing before touching anything:

- The frozen corpus (`scrabble-pairing/tests/corpus/cases.json`) already feeds
  the engine opaque `"P01"`-style strings. The engine has never treated `name`
  as a display name — it is an opaque unique key. **That is why this whole
  change can be Python-side.**
- `BYE_PLAYER_NUMBER` is `"BYE"` (`models.py:352`), and both bye checks are
  case-insensitive comparisons against `"Bye"`
  (`scrabble-pairing/src/standings.rs:48`, `pairing/base.py:241`). So feeding
  the *number* as the engine key keeps bye detection working unchanged — by
  coincidence, not by design. Phase 2 pins this with a test.

## Design decisions

Settled with the owner before writing; a later session should not relitigate
them without asking.

1. **`player_number` is the identity.** It gets the unique constraint it
   currently lacks, and becomes the key in every portable payload and at the
   engine boundary. No new UUID: the CoCo number is already the registry's
   identity and is human-readable in a log.

2. **Rewrites are allowed; identity is point-in-time.** When the registry
   replaces a `T-` number with a real CoCo number, `player_number` is rewritten
   in place. What matters is that identity is consistent *at any given moment*;
   the guarantee that a CoCo number is never reused belongs to the CoCo admins,
   outside Baxter. A `player_number_changed` event (Phase 7) keeps the
   append-only log truthful across the rewrite.

3. **The Rust engine does not change.** Python sends the identity string in the
   existing `name` field. No crate edits, no corpus migration, no `cargo test`
   churn. The engine's `name` is documented as "opaque key" at the boundary.

4. **Event payloads are versioned.** `TournamentEvent.schema_version` (which
   already exists, `models.py:832`, and is always 1 today) becomes 2 for
   number-keyed payloads. `replay.SCHEMA_UPGRADES` (`replay.py:48`, an empty
   hook waiting for exactly this) upgrades v1 payloads on read.

   *This is safe precisely because v1 Baxter enforced globally unique names* —
   a v1 payload's name always resolves to exactly one player.

5. **Stored digests are backfilled.** A migration replays each tournament and
   rewrites `TournamentEvent.digest` to the v2 (number-keyed) form, so there is
   one digest format afterwards. See the safety procedure in Phase 3d — this
   rewrites an append-only log and must not be done blind.

6. **Duplicate names become legal, but the create flow pushes back.** Creating
   a second "John Smith" shows the existing same-named players with their
   numbers and asks whether you meant to add the existing one. The typo case is
   caught; the real case is not blocked.

7. **Display disambiguates only when it must.** A name renders bare; the player
   number is appended only when another player in the same scope shares it.

8. **The ratings CSV gains number columns.** `results_export.py` grows explicit
   winner/opponent number columns so the export is unambiguous.

---

## Phase 1 — `player_number` becomes a real identity — **IMPLEMENTED**

*Landed 2026-08-23. `canonical_player_number` is imported from
`coco_ratings.identity`; migration `0036_player_number_identity` widens the
column, canonicalizes, repairs, then adds the constraint. Verified against a
throwaway **Postgres 18.2** (not just SQLite) with a fixture containing a
bare/padded pair, a blank, an exact duplicate and two casings of the reserved
number. One consequence found and fixed: `player_sync.import_players` matched
raw numbers and wrote via `bulk_create`, so a bare `7` in a registry upload
would have inserted a second row beside a stored `0007` instead of updating it.*


### 1a. Canonical form and constraint

A CoCo number's canonical form is **zero-padded to four digits** (`0233`) — the
central database's form. Baxter normalizes with the *same function*, imported
rather than copied:

```python
from coco_ratings.identity import canonical_player_number
```

That function is dependency-free and lives in the shipped `coco-ratings`
package for this purpose (see `../ratings/plans/baxter-integration.md`). Two
implementations would eventually disagree about whether `233` and `0233` are one
person, which is the exact failure this plan exists to prevent. Non-numeric
values pass through untouched, so `T-7` and `BYE` survive canonicalization.

```python
player_number = models.CharField(max_length=16)   # was 8

def save(self, *args, **kwargs):
    self.player_number = canonical_player_number(self.player_number)
    ...

class Meta:
    constraints = [
        models.UniqueConstraint(
            Lower("player_number"), name="unique_player_number_ci"
        )
    ]
```

- **Normalize on write**, in `save()`, exactly as the central database does.
  One un-normalized write is enough to split a person in two, and
  `Player.objects.create`, the admin and the shell all go through `save()`
  (`bulk_create` does not — the migration in 1c is the one place that must
  canonicalize by hand).
- **Uniqueness is on the canonical form.** Canonicalization already collapses
  `233`/`0233`/`00233`; the `Lower()` index then covers the non-numeric keys,
  where case is the only thing that could differ (`bye` vs `BYE`). Bye detection
  is case-insensitive on both sides of the engine boundary, so identity must
  match that or the two disagree.
- **Widened to 16.** A canonical number is 4 characters and the central column
  is `max_length=4`, but Baxter also stores `T-` placeholders, so it needs the
  room. The current 8 would fit today's data and leaves almost none.
- ⚠ **This is a functional unique index.** Per the known SQLite-vs-Postgres
  gotcha, run `manage.py sqlmigrate` and check the generated Postgres DDL
  before deploying — do not trust a clean local SQLite run.

### 1b. Reserved number

No real player may hold a number equal (case-insensitively) to
`BYE_PLAYER_NUMBER`. Enforce in `Player.clean()` and in every mint/import path.
`next_temp_player_number` (`models.py:330`) already can't produce it, but the
registry import and the admin JSON upload can.

### 1c. Auto-repairing migration

The dev DB is clean (240 players, no blanks, no duplicates), but prod is
unknown. The data migration repairs rather than refuses, in this order:

1. **Canonicalize every existing number first.** This step is not optional and
   its position is not arbitrary: today's rows predate normalization, so `233`
   and `0233` may both be present as *the same person*. De-duplicating before
   canonicalizing would see two distinct strings, keep one and mint a `T-`
   number for the other — turning one person into two, permanently, which is
   the precise failure the canonical form exists to prevent. Canonicalize, then
   collisions are visible as collisions.
2. blank/null `player_number` → a fresh `T-` number
3. each collision group → the lowest-pk row keeps the number, the rest get
   fresh `T-` numbers
4. anything colliding with the reserved `BYE` → a fresh `T-` number
5. prints a full report of every row it changed, so the deploy log is the record

Note step 1 means the migration cannot rely on `Player.save()` — historical
models in a migration don't carry it, and it uses `bulk_update` anyway. Call
`canonical_player_number` directly.

Mint the replacements by scanning existing `T-` numbers once, not per row — the
whole repair must be a single pass and must be idempotent if re-run.

**Verification:** `test_models.py` — creating a duplicate number raises;
creating one differing only in case raises; a bare number and its padded form
resolve to one row rather than two; the reserved number is rejected; `T-` and
`BYE` survive `save()` unchanged. A conformance test over the same case table as
`../ratings/tests/test_identity.py`, so a divergence between the two repos fails
here too. Migration tests over a fixture containing blanks, a three-way
collision, a case-only collision, **and a bare/padded pair that is one person**,
asserting the survivors keep their numbers and every replacement is a distinct
`T-` number.

---

## Phase 2 — The pairing layer keys on `player_number` — **IMPLEMENTED**

### 2a. Carry the key through the pairing DTOs

- `PlayerData` (`base.py:24`) gains the key. Keep the field spelling `name` at
  the *engine boundary* (decision 3) but make the Python DTO explicit:
  `PlayerData(key, name, rating)` where `key` is the player number and `name`
  stays for display and error messages.
- `Player` (`base.py:231`) — the standings type — keys on the same string.
  `is_bye` (`:241`) compares against `BYE_PLAYER_NUMBER`, not the literal
  `"bye"`.
- `Repeats._key` (`:335`) and `Starts._record` (`:356`) key on the player
  number.
- `ResultSlipData.winner_name`/`loser_name` carry numbers; rename the fields to
  `winner_key`/`loser_key` on the Python side so nothing reads them as display
  text by accident.

### 2b. The boundary

`pairing_data_to_input` (`engine.py:46`) sends `{"name": e.player.key, …}` and
the same for slips, `fixed_pairings`, `published_pairings` and
`inactive_players`. Document at that call site that the engine's `name` is an
opaque key — this is the one place where the two vocabularies meet.

`generate_pairings.py`: `entrant_by_name` (`:291`) → `entrant_by_key`, keyed on
`e.player.player_number`; `_is_bye_name` (`:39`) compares to
`BYE_PLAYER_NUMBER`.

Also on the key: `starts.py`, `playoff.py` (series high/low), `whatif.py`,
`simulate.py`, `match_simulation.py`, `assign_tables.py`.

### 2c. Pin the bye coincidence

A test asserting `BYE_PLAYER_NUMBER.lower() == "bye"` — i.e. that the Django
constant still matches `scrabble-pairing/src/standings.rs`'s `BYE_NAME` under
its case-insensitive compare. Byes silently stop working if either constant
moves, and nothing else would catch it. Name the test so the failure explains
the coupling.

**Verification:** the existing pairing suites must pass unchanged (they are the
regression net). Add a division with two entrants sharing a name and assert the
engine pairs them as distinct players, that repeats between each of them and a
third player are tracked separately, and that their starts don't merge — the
test that could not have passed before this plan.

### What landed, and what the plan did not anticipate

The DTOs, the boundary, `generate_pairings`, `playoff.py` and `whatif.py` went
over as written. `SharedNameIdentityTests` and `ByeConstantCouplingTests`
(`test_pairings.py`) are the verification; the whole Django suite and
`cargo test` pass, the latter untouched, as decision 3 promised.

Four things the plan had not accounted for:

- **`starts.py`, `match_simulation.py` and `assign_tables.py` needed no change.**
  They already work on entrant primary keys. Only `starts.py`'s
  ``corrections`` payload names players, and that is a record rather than a
  lookup — it moves in Phase 3 with the rest of the payloads.

- **`Playoff.seeds` had to move too.** The frozen seed snapshot is what the
  bracket derives participants from, so leaving it name-keyed while the
  standings were number-keyed would have made every `seed_of` lookup miss.
  Each entry now carries `key` *and* `player`: the key is the identity, the
  name rides along so the snapshot stays readable and the qualifiers table
  needs no lookup. Migration `0037` backfills `key` by name — exact for those
  rows, since they were all written while names were unique. `_save_playoff`
  does the same for a replayed v1 payload; Phase 3 can move that into
  `SCHEMA_UPGRADES` where it belongs.

- **Display had to be threaded, not just preserved.** Result slips carry keys
  only, so a standings row built from results has no name to show. `Results`
  now takes a key→name map from the entrants, and the standings `Player` keeps
  both `key` and `name` (falling back to the key when it has none — visibly
  wrong beats silently blank). The same applies to the bracket page: `Series`
  holds keys, so `views._series_row` resolves the names *and* the
  who-won highlights, because a template comparing rendered strings would light
  up both rows when two bracket players share a name.

- **The state digest changes shape here.** `division_digest` hashes series
  participants and playoff seeds, which are now numbers. That is the change
  Phase 3d already plans to backfill, so it is left to land there rather than
  being papered over twice. The fuzzer's meta-invariant (replay reproduces the
  digest) still holds, because both sides moved together.

---

## Phase 3 — Event log v2 — **IMPLEMENTED**

### 3a. Payloads

Every player reference in a payload becomes a player number. Affected commands
in `commands.py`: `_entrant` (`:205`), `_find_pairing` (`:250`), `_write_result`
(`:262`), `add_result`/`edit_result`, `add_fixed_pairing_cmd` /
`remove_fixed_pairing_cmd`, `create_playoff` (seeds), `import_division`,
`simulate_match_cmd` / `simulate_round_cmd`, `bulk_import_entrants`. Plus
`EntrantsGrid.to_portable`/`from_portable` (`grids.py:84`, `:103`) and
`resolve_player` (`:38`), which resolves on number and takes the name as
creation data rather than as the lookup key.

Keep the payload key spelled `player` (not `player_number`) where a payload
already has one field per participant, and rename `name1`/`name2`,
`first_name`/`second_name`, `winner_name`/`loser_name` to `…_player` so no
reader mistakes a number for a name.

### 3b. Versioning

- `records_event` stamps `schema_version=2` on new events; the JSONL export
  (`events.py:377`) writes the real version instead of the hardcoded `1`.
- Register a v1→v2 upgrader in `SCHEMA_UPGRADES` for each affected event type.
  Each resolves names against the roster **as it stands at that point in the
  replay**, which is unambiguous for v1 data (decision 4).
- `_upgrade` (`replay.py:72`) dispatches per event type today; a single shared
  upgrader function registered under each affected type is enough — don't add a
  wildcard mechanism for one migration.

### 3c. Digest and stored state

- `division_digest` (`events.py:256`) keys its entrant/pairing/result tuples on
  player number. Keep the v1 function beside it as
  `_division_digest_v1`, used only by the Phase 3d backfill, and mark it frozen.
- `Playoff.seeds` (`models.py:725`) is stored DB state keyed by name — a data
  migration rewrites each entry's `player` to the number. It is the *only*
  JSONField besides the event payload that holds a player name.

### 3d. Digest backfill (decision 5)

This rewrites an append-only log, so the migration must earn the right to:

1. For each tournament, replay it under **v1 rules** and compare against the
   stored v1 digests. If a tournament doesn't verify clean, **skip it and report
   it** — its log was already divergent and the backfill must not paper over
   that.
2. For tournaments that verify clean, replay under v2 and write the recomputed
   digest to each event.
3. Print a per-tournament report: verified-and-backfilled, or skipped and why.

Run it as a data migration, but keep the logic in a function that the tests can
call directly against a fixture.

### 3e. Fuzzer

`fuzz.py` currently generates distinct names (`:47`). Teach it to generate
*colliding* names — two or three players sharing one — so the meta-invariant
(replay reproduces the digest) actually exercises the case this whole plan
exists for.

**Verification:** `test_replay.py` — a v1 JSONL fixture replays clean through
the upgraders; a v2 log round-trips; a division with duplicate names replays to
an identical digest. `test_events.py` — payloads carry numbers, events are
stamped v2. `test_fuzz.py` passes with colliding names. Backfill tests: a clean
tournament is rewritten, a deliberately-corrupted one is skipped and reported.

### What landed, and where it departed from the plan

The payload rename, the version stamp, the upgraders, the versioned digest, the
backfill (migration `0038`, logic in `tournaments/digest_backfill.py`) and the
colliding-name fuzzer are all in. Fifteen fuzz seeds pass, and the whole chain
`0036`–`0038` was run on Postgres against a database whose digests had been
walked back to v1 first.

Four deliberate departures:

- **`division_state` is one function with a `version` argument, not a frozen
  `_division_digest_v1` beside it.** The freeze could not have worked:
  `standings_after_round`, `final_placements` and `PlayoffConfig.seeds` all
  moved to keys in phase 2, so v1 output has to be *reconstructed* from today's
  machinery rather than preserved. Branching in the four places the vocabularies
  differ keeps that visible; two near-identical 90-line functions would invite
  exactly the drift the freeze was meant to prevent.

- **Two v1 shapes need no upgrader**, because their player references point
  *within the payload* rather than into the database: a `state_snapshot` and an
  `entrants_saved` row. Their readers only require the tokens to match each
  other, so each reader distinguishes v1 from v2 by whether the row carries a
  separate `name` — self-describing, and visible at the point of use.

- **`entrants_bulk_imported` and `division_imported` stay name-keyed.** Their
  payload *is* the external document (a pasted CSV, a historical results
  bundle), which identifies people by name because that is all it has.
  Re-keying would mean inventing numbers for rows that have none; ambiguity
  there is the importer's to report (phase 4), not the log's to hide.

- **Payloads are not rewritten by the backfill, only digests.** A payload is
  recorded intent and stays at the version it was written; `schema_version` plus
  the upgraders are exactly the mechanism for that. A digest is a derived value,
  which is why it may be recomputed — and the migration still has to prove each
  tournament reproduces its stored *v1* digests before it is allowed to.

`Playoff.seeds` (listed under 3c) had already moved in phase 2, since the
bracket could not have derived participants without it.

---

## Phase 4 — Names become non-unique — **IMPLEMENTED**

- Delete `EntrantsGrid._duplicate_name_errors` (`grids.py:153`) and its call in
  `prepare` (`:147`). The guard exists solely because the engine keyed on name;
  Phase 2 removes the reason.
- `Player.create_unique` (`models.py:388`) → `Player.create` plus a separate
  `same_named(name)` query. The create flow returns "these players already have
  this name" rather than an error, and the caller confirms (decision 6).
  `CreatePlayerView` (`views.py:1469`) grows a `confirm` flag: absent + matches
  found → 409 with the candidate list (name, number, rating); present →
  creates.
- `import_entrants.py`: accept `name`, `name,rating`, or
  `number,name,rating`. A number resolves exactly. A bare name resolves if it
  matches exactly one player; if it matches several, **abort the whole import**
  with an error naming the candidates and their numbers (the import is already
  all-or-nothing on errors, `import_entrants.py:126`).
- `player_sync.import_players` already keys on number — verify it no longer
  needs its name-collision caveat and update the module docstring
  (`player_sync.py:9`).

**Verification:** `test_models.py` — two players may share a name; both keep
distinct numbers. `test_views.py` — the create flow returns candidates without
`confirm` and creates with it. `test_import_entrants.py` — the three CSV shapes,
plus the ambiguous-name abort listing both candidates.

### What landed

All four items, as written, plus the client half the plan did not spell out:
`edit_entrants.js` shows the candidates the 409 returns, each with their player
number and rating, and offers either "add this existing person" or "add as a
different person" (which resends with `confirm`). Driven end to end in a browser:
creating a second *Duplicate Dave* is refused, then confirmed, then both are
entered in one division and the grid saves.

Two details worth keeping in mind:

- **The CSV's duplicate check moved from the name to the identity.** Two rows
  with the same name and *different* numbers are two people and now import fine;
  two rows with the same number, or two bare rows with the same name, are still
  rejected — the latter because both would resolve to whoever holds it.

- **That browser run also showed why phase 5 is not optional.** The saved grid
  renders two identical `Duplicate Dave` rows with nothing to tell them apart,
  and the "Add Entrant" picker has the same problem. Phase 4 made the state
  legal; only the disambiguation rule makes it legible.

---

## Phase 5 — Display disambiguation — **IMPLEMENTED**

One helper, used everywhere, so the rule lives once:

```python
def display_names(players) -> dict[key, str]:
    """Name alone, or "Name (NUMBER)" when another player in the same scope
    shares that name (decision 7)."""
```

- **Scope is the division** for rosters, standings, pairings, results,
  scorecards, and the public entrants page.
- **Scope is the whole roster** for the player picker in `EntrantsGrid.lookups`
  (`grids.py:120`) and the registration-page search — the candidate set there is
  every player, so ambiguity must be judged against all of them.
- Applies to `results_export.py`'s name columns and the scorecard docx
  (`scorecards.py`).

**Verification:** `test_views.py` — a division with no clash renders bare names;
adding a same-named entrant makes *both* render with numbers, and no one else
changes. Grid lookups disambiguate on the global roster even when the division
has no clash.

### What landed

`tournaments/display.py` holds the rule: `display_names` (the primitive),
`division_labels` + `label_entrants` (stamp `Entrant.display_name` on the
instances a template will render), and `label_standings` (rewrite the standings
row's `name`, since that type already carries `key` and `name` separately).
Tests are in `test_display.py`; the pages were also read in a browser with two
*Ann Lee*s entered in one division.

Applied to: the entrants page, results, standings, pairings (both presenters),
the round tab, the played-result page, the fixed-pairing pickers, the entrant
picker, the explore comparison, the playoff bracket and its setup page, and the
scorecard docx (both the player's own name and their opponents').

Two things to know:

- **The ratings CSV was left alone**, against what this section said. That file
  is byte-compatible with the format the separate results program produces, and
  coco-ratings joins it on the name column; appending `(0233)` there would break
  that join for every tournament with a clash, to no reader's benefit. Phase 6's
  explicit number columns are the right fix, and they land next.

- **`Name (NUMBER)` sits awkwardly next to the standings' seed marker**, which
  renders as `Ann Lee (T-8001) (#1)` — two parentheticals in a row. Each is
  labelled differently (`#` marks the seed) and it only appears in the rare
  clash case, so it is left as decision 7 specifies rather than redesigned
  unilaterally.

---

## Phase 6 — Ratings CSV carries numbers — **IMPLEMENTED**

`results_export.HEADERS` (`:25`) gains winner/opponent number columns.

> **The column order below is wrong and was not used.** Interleaving the
> number columns breaks the ratings reader — see "What landed". The shipped
> order appends them:
>
> ```
> Submitted On, Round, Winner, Winners Score, Opponent, Opponents Score,
> Winner Number, Opponent Number
> ```

```
Submitted On, Round, Winner, Winner Number, Winners Score,
Opponent, Opponent Number, Opponents Score
```

`whatif_import._parse_csv` (`whatif_import.py:95`) currently requires an exact
header match. It must accept **both** the legacy 6-column and the new 8-column
form — historical CSVs exist and the what-if importer is the thing that reads
them — preferring the number columns when present.

The coco-ratings side of this is planned in
`../ratings/plans/baxter-integration.md` Phase 2. The columns are **additive**:
the name-keyed six-column form stays valid input there, because it is still
produced by a Google Form export that cannot carry numbers (players type their
own names into it). So Phase 6 breaks nothing, needs no coordination window, and
can land whenever.

**Verification:** `test_results_export.py` — the new columns carry numbers and
disambiguated names. `test_whatif_import.py` — both header widths parse, and the
8-column form resolves duplicate-named players correctly where the 6-column form
cannot.

### What landed

Both readers accept either width, and the importer prefers the number when a row
carries one — `test_whatif_import` shows the eight-column form reaching a
lower-rated second *Alice* that the six-column form cannot address at all.

**The column order in this section was wrong.** `coco_ratings.io.ResultCSVReader`
unpacks positionally:

```python
_time, round, winner, win_score, opp, opp_score, *rest = row
```

so a column inserted before `Opponents Score` shifts every later field —
`win_score` reads a player number, `opp` reads a score. Fed the interleaved
layout, the real reader fails with *"Score field contained a non-digit: Bob"*;
with numeric-looking names it could have corrupted a rating run instead. The
trailing `*rest` is what makes *appending* safe, so the numbers go at the end.
`test_results_export.RatingsReaderCompatibilityTests` pins this against the
actual reader — a Baxter export parses to the right records, and the interleaved
layout raises.

That also makes this section's "breaks nothing, needs no coordination window"
claim true, which it was not as originally written.

The name columns are deliberately **not** disambiguated (the phase 5 note): they
are the join key, and the numbers beside them are what resolve a shared name.

---

## Phase 7 — `player_number_changed`

Ships the mechanism that keeps decision 2 honest, ahead of the registry upload
that will need it:

- A `player_number_changed` command + event, payload `{old, new}`, added to the
  catalog (`events.py:40`) with an activity-page description (`:533`). This is
  no longer speculative: it is the mechanism behind number resolution in
  `PLAN_COCO_PROGRAM.md` — a guest enters as `T-7`, an admin assigns `0412`
  centrally, Baxter pulls and the director confirms the resolution.
- `ReplayContext` (`replay.py:51`) keeps a live rename map; every player lookup
  during replay resolves through it, so an event recorded at seq 12 under
  `T-7` still resolves after seq 40 renamed it to `CO1234`.
- The rewrite path itself (applying the registry's id_map after upload) is
  **not** built here — that belongs with the upload transport. This phase is
  the log/replay half, so the log is already correct when the upload lands.

**Verification:** `test_replay.py` — a log that enters a player as `T-7`,
records results, renames to `CO1234`, then records more results replays to a
single player with the full result set and a matching digest.

---

## Sequencing and risk

Phases 1–3 are one indivisible correctness change and should land together or
in quick succession: between Phase 1 and Phase 3 the payloads and the engine
key disagree with each other. Phases 4–7 are independent and can land in any
order afterwards.

The three things most likely to bite:

1. **The functional unique index on Postgres** (Phase 1a) — verify with
   `sqlmigrate`, not a local SQLite run.
2. **The digest backfill** (Phase 3d) — it rewrites an append-only log. The
   verify-first-then-rewrite procedure is not optional.
3. **The bye coincidence** (Phase 2c) — byes keep working only because
   `BYE_PLAYER_NUMBER == "BYE"` matches the Rust `BYE_NAME` compare. Untested,
   this is a silent breakage waiting for someone to tidy a constant.

## Explicitly out of scope

- Any change to the `scrabble-pairing` crate or its frozen corpus (decision 3).
- A UUID or surrogate identity separate from `player_number` (decision 1).
- Applying the registry's id_map on upload (Phase 7 ships only the log half).
- Making player *names* unique, warned-about globally, or normalized.
