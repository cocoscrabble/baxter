# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Baxter is a scrabble tournament manager built with Django 6.0 and Python 3.14.

Design/implementation plans live in `plans/` (see `plans/README.md`) — put new
plans there, not in the repo root.

## Development Commands

```bash
npm install        # JS deps (Tabulator for the edit grids) — served from
                   # node_modules via django-node-assets. Without it, dev grids
                   # 404 Tabulator and render blank (prod bakes it in via Dockerfile).

# Rebuild the Rust pairing extension after editing scrabble-pairing*/ crates
# (uv does not watch the crate source)
make rust-engine   # == uv sync --reinstall-package scrabble-pairing-py
```

## Pairing engine (Rust)

The pairing computation is the `scrabble-pairing` Rust crate, called from Python
via the `scrabble-pairing-py` PyO3 extension (a separate crate so the core stays
wasm-clean). `scrabble_pairing_py.pair_json(str) -> str` is the boundary;
`tournaments/pairing/engine.py::pair_with_engine` is the single Python entry
point. Building requires a local Rust toolchain; `uv sync` builds the wheel via
maturin (`make rust-engine` to rebuild after crate edits).

**The Python engine has been deleted** — engine changes are Rust-only now (edit
`scrabble-pairing/src/`, add a `cargo test`). What stays in Python is the
ORM-facing layer: `PairingData` assembly, `standings_after_round`/`seedings`
(standings display), `Repeats`/`Starts`, `PairingError`, and the publish/
regenerate lifecycle. `tests/corpus/cases.json` is a **frozen** regression
fixture (the Python oracle that generated it is gone); `cargo test` still checks
the Rust engine against it.

## Playoffs

A division may carry a `Playoff`: a 2/4/8-player bracket with per-series
best-of-N lengths, run either after the main schedule (postscript) or alongside
it (concurrent). See `plans/PLAN_PLAYOFFS.md`.

The bracket is **derived, never stored**. The only recorded intent is the
playoff's configuration plus its confirmed seed snapshot (`playoff_created`);
who meets whom, series scores, which games still need playing, and final
placements are all computed by `tournaments/playoff.py` from that plus the
division's results. Consequences worth knowing before changing anything here:

- A game is only ever generated when it is *certainly* necessary, so a game a
  clinch made pointless never exists — no bye, no zero score, no export row.
- `regenerate_pairings` builds playoff games alongside engine pairings; a
  published window is repaired in both directions (a decider that turns out to
  be needed is added, a retired game is deleted).
- `PlayoffSeries` rows are derived structure only (participants, length, window),
  upserted like `RoundPairings`; never store a score or winner on them.
- Concurrent mode reserves bracket players from ordinary pairing through the
  engine's generic `inactive_players` (round -> names): no game, no bye, and not
  withdrawn. `Entrant.dropped` is never touched by a playoff.

## Starts

A published pairing is a printed board, and it **owns the start**. Its
first/second assignment is authoritative from the moment the round is published
— before any result is entered — and a result slip keyed the other way round is
rewritten to match, as a logged `result_starts_corrected` event.

The rule is stated in two places that must agree: `tournaments/starts.py`
(`PUBLISHED_PAIRING_OWNS_THE_START`, which drives the rewrite) and
`scrabble-pairing/src/pair.rs` (`PUBLISHED_ORIENTATION_WINS`, which drives the
engine's ledger). Both are single constants so the policy can be reversed.

The board owns the *start*, never *who played whom*: a saved pairing whose
players turn up in some other game that round is stale and dropped. Bye rows are
excluded throughout — they are stored real-player-first for display, the
opposite of the ledger's convention that the bye opponent is the notional
starter (`PairingData.for_division` flips them back).

## Event log

Every state-changing action is recorded as an append-only `TournamentEvent`
(see PLAN_EVENT_LOG.md). Mutations go through **commands** (`@records_event` in
`tournaments/commands.py` + domain modules; grid saves via the editgrid
`on_saved` hook) — not direct view writes. Payloads are pk-free (name-keyed) so
the log replays into a fresh DB. Key pieces:

- `tournaments/events.py` — recorder, `division_digest` (excludes DRAFT rounds),
  the command catalog, the opt-in `strict_write_guard`.
- `tournaments/replay.py` + `replay_tournament` command — reconstruct a
  tournament from its log (`--verify` compares digests).
- `tournaments/fuzz.py` + `fuzz_tournament` command + `test_fuzz` — seeded
  fuzzer whose meta-invariant is that replay reproduces the digest.
- Activity page + `export_event_log` (JSONL); `snapshot_tournaments` backfills
  pre-log tournaments. Both are gated on `CanReadTournamentLogMixin` — the
  tournament's editors, **plus Supervisor and above**, since Director is the
  default role and would let every account read every log. Each row unfolds into
  what the event changed (`events.event_detail`).

**A new mutating POST view must route through a command** (or be added to the
exempt set in `test_event_completeness.py`, which fails CI otherwise).

## Admin pages

Anything gated on the Admin role is listed at `/tournaments/manage/`
(`AdminIndexView`), and admins get an **Admin** link in the navbar — the only
navigation into it. The three player pages (roster pull, player import, WESPA
import) used to link only to each other, so they were reachable only by typing a
URL; that stopped being tolerable when the roster pull started running on a
timer and could leave work waiting on a page nobody visited.

**A new admin-only view must appear on that page.**
`test_admin_index.CompletenessTests` reads the URLconf for every view gated on
`IsAdminMixin` and fails if one is not linked there, so a new page cannot
quietly become unreachable the way those three were.

The page shows the roster's *state*, not just links: a guest awaiting
confirmation, or a scheduled pull that has been failing, is flagged here.
`/manage/` mirrors the sibling cocodb site's staff area; `/admin/` stays Django's
own, which is gated on `is_staff` rather than on the role.

**Every admin action is recorded in the admin log** (`/manage/log/`,
`AdminAction`): who did it (blank for a scheduled pull), and whether it worked.
Actions run inside `admin_log.logged(kind, actor)`, which records success, a
refusal the caller marks with `entry.fail(...)`, or an exception on its way out
(then re-raised) — so a cron pull that crashed still leaves a row. The pulls log
from `run_sync`, so all three paths are covered. `test_admin_log.CompletenessTests`
fails if an admin view with a POST handler never logs. The index flags any
failure in the last week.

`/manage/users/` lists every account and lets an admin set a new password on
one ranked **strictly below** them (`can_set_password_for`): never their own,
another admin's, a superuser's, or — unless they are staff — a staff account's,
since resetting a password is taking the account over.

## Login throttling

django-axes locks a login out after 10 failures for 5 minutes (lenient on purpose
until we know directors don't trip it), keyed on
username **and** client address together (settings.py explains why neither alone
is safe). The address is the *right-most* `X-Forwarded-For` entry — the one
Dokku's nginx wrote — so a forged header cannot dodge it; `test_login_throttling`
fails if that is changed. Axes is disabled under `manage.py test` because
`client.login()` passes no request; tests re-enable it with `override_settings`.
To lift a lockout early: `manage.py axes_reset_username <username>`.

## Roster sync (the central player database)

Baxter mirrors player identity and CoCo ratings from the central database
(`cocodb`, the `../ratings` repo). `tournaments/roster_import.py` fetches and
upserts a `coco.roster/1` document; `tournaments/roster_sync.py` runs that and
records the outcome. See `plans/PLAN_COCO_PROGRAM.md` (W4).

**It runs unattended.** `app.json` declares a Dokku cron entry that runs
`manage.py pull_roster` every six hours, so the player table keeps up on its own;
it is `uv run --no-sync` because a plain `uv run` rebuilds the Rust extension
inside every one-off container (the Dockerfile's `COPY . .` lands fresh mtimes on
the crate sources after `uv sync`);
`/players/roster/` is for pulling sooner than that, or for uploading a snapshot
at an event with no connection. All three paths go through `run_sync`, so they
leave the same kind of record.

Two things follow from nobody watching a scheduled pull, and both are the reason
`RosterSync` exists at all:

- **A failure has to be visible.** A rotated `ROSTER_API_TOKEN` would otherwise
  401 four times a day in silence (and `../vps` sets Dokku config with
  `--no-restart`, so a rotation lands on the next deploy, not immediately). The
  command exits non-zero for cron, and every attempt writes a `RosterSync` row
  that `/players/roster/` shows.
- **Held-back rows have to outlive the run.** A pull that finds a roster number
  whose name matches exactly one local guest changes nothing and offers the
  match for confirmation — matching by name is a guess, so it is the one step a
  human makes. Those live on the record, not in the puller's session, which is
  what lets a director confirm what a cron tick found.

A pull cannot disturb a running event: entrants' rating seeds freeze when their
division's first round is published (`plans/PLAN_ENTRANTS.md` decision 3), which
is what makes an unattended pull safe at any hour. Before that, a pull re-pins
them — see "Entrant ratings".

**Two guests with one name are not held back** — picking between them is the
guess — so the pull creates the real player as a third row, and renumbering is
then impossible. `/players/merge/` (`player_merge.py`, the `player_merged`
command) offers every guest whose name matches a CoCo player's, and moves the
guest's entrants onto that player. It is logged in every tournament the guest
played; replayed into a database without the CoCo player, it renumbers instead,
which leaves the same digest.

## WESPA ratings (the other rating list)

Baxter keeps a **local mirror of the whole WESPA rating list** — `WespaPlayer`,
some 9,200 rows — pulled from one bulk JSON document
(`WESPA_API_URL`, default `wespa-api.xerafin.net/players.php?idsonly=1`) by
`tournaments/wespa_api.py` + `wespa_ratings.py`, run and recorded by
`wespa_sync.run_sync` and `app.json`'s weekly `pull_wespa` cron entry. See
`plans/PLAN_WESPA.md`.

**Why the whole list and not just the ratings.** The players Baxter has never
seen are the point: an overseas visitor has no CoCo number and no CoCo rating, so
the player table can never find them, and their rating used to be typed in at the
registration desk from a website. The registration page searches the mirror
alongside the player table, and entering a WESPA-only hit mints the guest with
their name, rating and `wespa_id` already filled in. Everything else in this
section exists to make that search trustworthy.

Four rules, all easy to break:

- **A pull creates no `Player` and deletes nothing.** A WESPA row becomes a
  player when a director enters one, not before.
- **`Player.wespa_id` is the link, and links survive renames.** It is set when a
  human picks the row, or when a name is unique on *both* sides — the same guess
  the old CSV import made, now visible and undoable. Everything else waits.
- **Ambiguity is held back and listed; absence is not.** A name shared by several
  people links nobody and lands on `WespaSync.pending`. A player with no WESPA
  row is the normal case and is not reported — a pending list holding most of the
  roster is a list nobody reads. Spelling mismatches are linked by hand at
  `/players/wespa/`.
- **Still unlogged and still global** for the player table: entrants' ratings
  freeze once their division is under way, so a weekly pull cannot move a live
  event. What it logs is `player_created`, which carries `wespa_id` so a replay
  recreates a guest already linked, and the re-pinning of divisions that have
  not started (see "Entrant ratings").

## Entrant numbers are a seeding

`Entrant.number` is the entrant's number **for this tournament** — not a seat,
not a board, not a table. (Boards and tables are a separate, genuinely physical
thing: `board_table_map` and `assign_tables`.) Nothing pairs off it; the engine
keys on rating, and the number is what breaks a tie between equal ratings and
what the standings show in brackets.

**Nobody types it.** It is derived from the pinned rating, highest rated is 1,
ties broken on the player number — `Entrant.seeding_for` /
`commands.reseed_entrants`. The registration form has no number field and the
grid's column is read-only; every path that can change the order (add, guest,
WESPA guest, edit, grid save, CSV import, rating refresh) calls
`reseed_entrants` afterwards.

Three things that are easy to break:

- **It stops once a round has left draft.** After that the seeding is what the
  division actually started as, so a late entrant is appended rather than
  shifting every number on the standings page. `reseed_entrants` records
  nothing when `division_under_way`, so callers need no condition of their own.
- **The numbers are recorded, not re-derived.** Entrant numbers are in
  `division_digest`, so a payload meaning "sort by whatever the ratings are"
  would replay against a different rating table and renumber differently — the
  same rule `entrant_ratings_refreshed` follows.
- **It is its own event, not folded into the add or the grid save.** That is
  what leaves every payload written before numbers were derived replaying
  exactly as it always did: no `entrants_reseeded` event, no reseed.

## Entrant ratings

An entrant pins their whole rating seed on the entrant row, but the pin only
**freezes once the division is under way** (first round published) — the same
moment the seeding freezes. Before that it is live: people register for a future
event early, and the rating they registered with is not the one they bring
(`plans/PLAN_ENTRANTS.md` decision 3, revised).

- **Every write of a player's rating goes through `player_ratings.save_players`**
  — the roster pull and resolution confirm, the WESPA pull and link, the player
  imports. It writes, then runs `players_rerated`, the one place for everything
  downstream of a rating change (today `entrant_sync.refresh_upcoming`, which
  re-pins and reseeds every division that has not started). `players_rerated`
  is called on its own only where the rating an entrant follows changes without
  a rating write: a guest merge, and the Django admin form.
  `test_player_ratings` fails on a `Player.objects.bulk_update` anywhere else.
  **Never call either from inside a command** — replaying that command would
  re-read the replay database's player table.
- Publishing catches up first (`refresh_before_start`). If anything moved, the
  drafts are re-paired and the director is asked to publish again rather than
  printing a round they never saw.
- It goes through the ordinary logged commands, so replay is exact; a scheduled
  pull records them with no actor. Re-pinning or renumbering drops draft rounds.

Once under way, the roster pull cannot move a seed — that is what makes the
six-hourly sync safe mid-tournament. The exception is deliberate and
per-entrant: `/entrants/refresh-ratings/` re-pins the ticked entrants from the
player table. The entrants page shows the drift for editors, with checkboxes,
and warns if a round has already left draft.

Three rules both paths follow, all easy to break:

- **Manual ratings are never offered.** A typed rating is a director saying what
  a player is worth; a sync does not overrule it.
- **The event records the values it wrote**, not the intent to sync. Entrant
  ratings are in `division_digest`, so an event meaning "take whatever the
  roster says" would replay to a different digest every time.
- **The drift column is editor-only.** `_entrants_table.html` is shared with the
  public embed; `show_drift` is only ever set for editors, like `can_edit` for
  the payment column.

## Commit as you go

**Commit each piece of work as it lands, not in a batch at the end.** When a
change is complete and its tests pass, commit it before starting the next one —
even when the next one is obviously coming, and even when the user has not asked
for a commit yet.

This repo is `jj` (colocated with git). `jj commit -m "..."` describes the
working copy and starts a new change on top; `jj commit <paths> -m "..."` commits
only those paths and leaves the rest in the working copy.

The reason is what happens otherwise. Several pieces of work pile into one
working copy, their edits interleave in the same files, and splitting them
afterwards means hand-reconstructing intermediate versions of `views.py` and
friends that have to be rebuilt and re-tested one at a time to check each commit
stands on its own. Committing at the point the work is done costs nothing; not
committing costs that.

Related, and the reason it is worth the discipline here specifically: a change
in this codebase usually touches a model, a command, a view, a template and the
plan document together, so *every* piece of work is the kind that entangles.

## Code Standards

- Do not add tests that are just testing django functionality
- `round` is used as a variable/parameter name throughout the pairing code (it refers to a tournament round). Do not rename it to avoid shadowing the builtin.
