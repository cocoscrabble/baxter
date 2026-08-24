# CoCo Program Plan: Baxter ↔ the central player/ratings DB

Program-level design covering the two repos together. Baxter's entrant redesign
(#47), its player-identity change, the central player DB, the data interchange
between them, and live in-tournament rating projections are one piece of work
with one shared identity model — planning them separately is how they end up
disagreeing.

This document holds the **decisions and the contract**. Per-repo phase detail
lives in:

- `plans/PLAN_PLAYER_IDENTITY.md` (Baxter) — `player_number` becomes the key
- `plans/PLAN_ENTRANTS.md` (Baxter) — entrant/registration redesign
- `../ratings/plans/baxter-integration.md` (coco-ratings) — the central side

Written for implementation in a fresh session. Baxter references are as of
commit `cf93873`; coco-ratings references are as of `d4cd9a8`.

---

## The two systems

**Baxter** (this repo) runs tournaments: pairing, results, standings. Django 6 /
Python 3.14, with the pairing engine in Rust.

**coco-ratings** (`../ratings`, deployed as the `cocodb` Dokku app) is both the
rating engine and the central player DB:

- `src/coco_ratings/` — an installable package (`coco-ratings`, **zero runtime
  dependencies**, `requires-python >=3.10`) holding the Norwegian rating system:
  `types.py` (pure data model), `io.py` (file formats), `rating.py`
  (`RatingsCalculator` — the math), `ratingsdb.py` (carry-forward replay),
  `pipeline.py`.
- `web/` — a Django site with three apps sharing one identity: `players`
  (`Player`: `player_number` + `name`, plus private `PlayerDetails`), `ratings`
  (`CurrentRating`, `TournamentResult`, `Tournament` — projections), `accounts`.
- `results/*.csv` + `data/tournaments.csv` are the **source of truth**. There is
  no persisted rating state: `build_db` replays the entire tournament history in
  chronological order every time, because a player's new rating depends on their
  opponents' ratings *at that moment*.

Scale is small and worth knowing: **245 players, 130 tournaments**. Nothing here
needs to be built for volume.

## Governing principles

1. **Baxter never needs the central DB while a tournament is live.** Everything
   required to run the event and to project ratings is pulled beforehand and
   frozen on the entrants. A network outage mid-event changes nothing.

2. **One identity: the CoCo player number**, canonically zero-padded to four
   digits (`0233`). Names are display, never keys — in *either* system.

3. **Provisional players never enter the central DB.** Baxter may create them
   locally with `T-` numbers; the push blocks until a central admin has assigned
   them real numbers and Baxter has pulled those.

4. **One implementation of the rating math.** Baxter imports the same
   `RatingsCalculator` the official replay uses. A second implementation would
   be a bug generator.

5. **Live ratings are non-binding, and derived, never stored.** Official ratings
   only ever come from the central chronological replay. Baxter's projection is
   recomputed from results on demand — the same discipline the playoff bracket
   already follows.

## Decisions

Settled with the owner before writing; a later session should not relitigate
them without asking.

**Identity**

1. `player_number` is the key in both systems. Baxter adopts the central
   `canonical_player_number` **as shared code**, so `233`, `0233` and `00233`
   can never split one person in two. It lived in the ratings project's Django
   app (`web/players/models.py`), which the shipped package does not include, so
   it moved to `coco_ratings.identity` — dependency-free, importable from both.
   (Done 2026-08-23 in the ratings repo.)

   Conveniently, that function is already correct for Baxter's extra forms:
   `str(int(v)).zfill(4) if v.isdigit() else v` passes `T-7` and `BYE` through
   untouched. Baxter's uniqueness constraint applies to the canonical form, and
   remains case-insensitive for the non-digit forms.

2. Baxter's `player_number` holds `0233` for known players, `T-7` for
   locally-created ones, `BYE` for the bye. The central DB's stricter
   `^\d{1,4}$` validator is *correct* and stays — it is what enforces principle
   3.

3. A `T-` number is resolved by **central admin assignment, then a Baxter pull**
   — not by a minting API. The central DB stays the sole authority on who gets a
   number. Baxter records the resolution as the `player_number_changed` event
   from `PLAN_PLAYER_IDENTITY.md` Phase 7, so the log stays replayable across
   the rename.

**Rating math**

4. **(Implemented 2026-08-23.)** The ratings repo extracts a dependency-free
   `coco_ratings.core` (the `types`
   data model + `RatingsCalculator`) that imports neither `io` nor the logging
   config. Baxter depends on `coco-ratings` and imports only `core`.

   This is not cosmetic. `rating.py:22` runs
   `logging.basicConfig(filename="coco_ratings.log", level=DEBUG)` **at import
   time** — that is what produced the 1.68 GB log in the ratings repo, and
   importing it into Baxter would hijack Django's root logging config and start
   writing a DEBUG file into the working directory. `rating.py` also imports
   `io.py` at module level, so today you cannot get the math without the
   file-format layer.

5. Baxter consumes it as a **git dependency tracking `main`**
   (`coco-ratings = { git = "https://github.com/cocoscrabble/ratings.git", branch = "main" }`).
   `uv.lock` pins the commit, so builds stay reproducible and an upgrade is a
   deliberate relock. A path dependency cannot work — Baxter builds in Docker
   and `../ratings` is outside the build context — and both repos are public, so
   the clone needs no build-time credentials.

   The consequence to keep in mind: the ratings repo's `main` is now an input to
   Baxter's image build.

6. Baxter freezes the full rating seed on the **Entrant** at registration:
   `rating`, `deviation`, `career_games`, `last_played`. A later pull cannot
   shift a running tournament's projection, and replay reproduces it exactly.
   This extends the rating snapshot `PLAN_ENTRANTS.md` already establishes.

**Interchange**

7. **Pull** = the full roster with current ratings, available two ways off one
   schema: an authenticated read-only JSON endpoint (normal path) and a
   downloadable snapshot file (offline/air-gapped events). Baxter's importer is
   one code path.

8. **Push** = Baxter emits the ratings project's *native artifacts* as a
   download; a human commits them and reruns `build_db`. No ingest endpoint, no
   new trust boundary, and the committed-files-are-truth model is preserved.

9. **The shared results CSV gains optional number columns, and both forms stay
   supported.** The name-keyed format is a direct export from a Google Form
   linked to a spreadsheet, still in service; Baxter's exporter was written to
   match that export byte-for-byte, which is why the two already agree. The
   ratings-side readers dispatch on the header: numbers when present, otherwise
   today's name path joined through `data/players.csv` (which exists for exactly
   that join).

   The Form path can never carry numbers — players type their own names into it
   — so it is not a format to be upgraded, only one to be replaced if Baxter
   eventually takes over result collection.

   Nothing in the existing corpus is rewritten and the name path is not deleted.
   Backfilling history and dropping name matching become available as cleanup
   once the other producer is retired; doing either now would break a live
   producer.

**Live ratings**

10. Standings gains a projected-rating column with the delta (`1652 (+12)`) and a
   clear non-binding note.

---

## The interchange contract

Both repos carry a copy of this section; it is the thing that must not drift.

### Roster snapshot (pull)

One schema, two transports. `GET /api/roster/` (token-authenticated) and the
published snapshot file return an identical document:

```json
{
  "schema": "coco.roster/1",
  "generated_at": "2026-08-22T14:03:00Z",
  "players": [
    {
      "player_number": "0233",
      "name": "Alec Sjöholm",
      "rating": 2093,
      "deviation": 76.92,
      "career_games": 489,
      "last_played": "2026-03-14"
    }
  ]
}
```

- `player_number` is canonical (zero-padded). It is the only identity; `name` is
  display data that Baxter may overwrite on every pull.
- The four rating fields come from `ratings.CurrentRating`. A player with no
  rated games yet appears with `rating: null` and Baxter treats them as unrated
  (the calculator seeds them at 1500 / deviation 150 and solves).
- The whole roster is 245 rows. Do not build pagination.

### Tournament bundle (push)

Baxter produces a download containing exactly what the ratings repo ingests:

| Artifact | Content |
| --- | --- |
| `<filename>-results.csv` | `Submitted On, Round, Winner, Winner Number, Winners Score, Opponent, Opponent Number, Opponents Score` |
| `<filename>-ratings.csv` | `Name, Number, Rating` — the pre-tournament rating list, from each entrant's frozen snapshot |

Baxter always emits the **number-bearing** form. The legacy six-column results
form and two-column ratings form remain valid input to the ratings project
(decision 9), but Baxter has no reason to produce them: it knows every player's
number, and blocking the push on unresolved `T-` numbers is what guarantees it.
| `tournaments.csv` row | `FancyName, Division, City, Name, Tournament, Filename, Date, Order` |

- One results/ratings pair **per division** — the ratings project's "section" is
  Baxter's division, and `data/tournaments.csv` already carries a `Division`
  column.
- `Submitted On` is the Excel serial `results_export.py` already emits.
- **The push is blocked** if any entrant's number is still `T-`, listing who
  needs assignment. This is the enforcement point for principle 3.
- `Order` matters: two tournaments on the same day that share players rate
  differently depending on sequence, and the date cannot express that. Baxter
  should leave it blank and let the committer set it.

The headers above are a superset of what `results_export.py` emits today. The
existing six columns are byte-identical to `results/*-results.csv` because
Baxter's exporter was deliberately written to match the Google Form export that
produces those files (`Submitted On` is the Form's timestamp, as an Excel
serial) — so the added columns extend a format Baxter already speaks, rather
than replacing it.

### Number resolution

```
1. Baxter registers a guest              -> player_number "T-7"
2. tournament runs, results entered      -> push blocked: "T-7 needs a number"
3. admin adds them at /manage/players/add -> central assigns 0412
4. Baxter pulls the roster               -> matches by name, offers the resolution
5. director confirms                     -> player_number_changed {T-7 -> 0412}
6. push unblocks
```

Step 4 matches by **name**, the one place it is unavoidable — the whole point is
that the new player has no shared key yet. It is a director-confirmed suggestion,
never an automatic rewrite, and ambiguity is reported rather than guessed.

---

## Live rating projection

Pure function, no storage, no network:

```python
def project_ratings(division) -> dict[player_number, Projection]:
    """Seed a coco_ratings.core Section from each entrant's frozen snapshot,
    add a GameResult per completed game, run the calculator, return
    (new_rating, delta) per entrant. Derived; never stored."""
```

Details that matter:

- Seed each `core.Player` from the entrant snapshot (`rating`, `deviation`,
  `career_games`, `last_played`), then call `adjust_initial_deviation(start_date)`
  exactly as the official replay does — deviation grows with inactivity, and
  skipping it would make the projection systematically wrong for returning
  players.
- Unrated entrants go through `calc_initial_ratings`, the same convergence loop
  the official run uses.
- **Byes need no special-casing**: the calculator already skips a game where
  `g.opp_score == 0 or g.score == 0`, and Baxter materializes byes as 50–0.
- Recompute on demand — 245 players is nothing. Cache only if profiling says so.
- The projection is stale the moment another tournament rates ahead of this one
  in the chronological replay. That is inherent, and is exactly why it is
  labelled non-binding.

---

## Workstreams and sequencing

```
W1  Baxter identity          PLAN_PLAYER_IDENTITY.md      ──┐
W2  ratings-side re-keying   ../ratings/plans/…            ──┤
                                                            ├─> W4 sync
W3  Baxter entrants          PLAN_ENTRANTS.md             ──┘        │
                                                                     │
W5  live ratings  (needs core extraction from W2 + seeds from W3) ───┘
```

**Status:** W1 (`PLAN_PLAYER_IDENTITY.md`, seven phases), W2's core extraction,
and W3 (`PLAN_ENTRANTS.md`, six phases) are all **implemented**.

What remains is the half that crosses the wire, and it is all in the other
repo's court or waiting on a protocol:

- **W2's remaining phases** — number-keyed results, the roster-pull endpoint,
  assigning numbers centrally — live in `../ratings/plans/baxter-integration.md`.
  Baxter's side of the first is already done (the export carries numbers; both
  CSV widths parse).
- **W4 (sync)** needs the roster pull to exist. Baxter has the seam
  (`player_source.PlayerSource`), the outbound bundle
  (`tournament_export.py`), and the rename mechanism
  (`commands.change_player_number`) — what is missing is the transport and
  applying the returned id_map.
- **W5 (live ratings)** now has everything it needs on this side: the shared
  calculator, and entrants that pin `rating`/`deviation`/`career_games`/
  `last_played` as the seed. The last three are zero/null until the pull fills
  them, which the calculator reads as an unrated player.

- **W1 and W2 are independent of each other** and can run in parallel — they
  converge on the same canonical number format, which is why decision 1 makes
  the canonicalizer shared code rather than two implementations.
- **W3 depends on W1** (already stated in `PLAN_ENTRANTS.md`).
- **W4 (sync) depends on W1 + W2 + W3**: it needs the identity, the endpoint,
  and the entrant fields to store what it pulls.
- **W5 (live ratings) depends on W2's core extraction and W3's seed snapshot**,
  but not on W4 — it can be built against hand-entered seeds and only becomes
  automatic once the pull exists.

The order to actually work in: **W2's core extraction first** (it is small,
unblocks W5, and fixes a live bug in the ratings repo), then W1, then W3, then
W4 and W5 in either order.

---

## Amendments to the existing Baxter plans — **APPLIED**

Folded into `PLAN_PLAYER_IDENTITY.md` and `PLAN_ENTRANTS.md` on 2026-08-23.
Kept here as the record of what changed and why:

**`PLAN_PLAYER_IDENTITY.md`**

- Phase 1a: uniqueness is on the **canonical** form, not merely
  case-insensitive. Adopt `canonical_player_number` from the ratings repo as
  shared code and normalize in `Player.save()`, exactly as the central DB does.
- Phase 1a: state that real numbers are zero-padded 4-digit; `max_length=16`
  still stands (for `T-` forms), but the *format* of a resolved number is the
  central one.
- Phase 1c: the auto-repair migration must canonicalize existing numbers
  (`233` → `0233`) before de-duplicating, or it will "repair" two spellings of
  one person into two players.
- Phase 6: the ratings CSV number columns are now a **cross-repo** change with a
  defined counterpart in `../ratings/plans/baxter-integration.md`. The columns are
  *additive*: the name-keyed form stays valid on the reading side, so Phase 6
  breaks nothing and needs no coordination window. Its own note about confirming
  the format with coco-ratings first is now satisfied.
- Phase 7: `player_number_changed` is no longer speculative — it is the
  mechanism behind number resolution above.

**`PLAN_ENTRANTS.md`**

- Phase 1a: the entrant snapshot grows `deviation`, `career_games`,
  `last_played` alongside `rating`/`rating_source` (decision 6).
- Phase 5: the `PlayerSource` seam's concrete implementation is now specified —
  it is the roster pull in this document, not an unknown.
- Decision 2's rating cascade needs one addition: the central rating is the CoCo
  rating, so `Player.rating` is what a pull writes. WESPA stays independent.

## Open items

- **The WESPA source is still unknown** and is unaffected by any of this; it
  remains a stub seam in `PLAN_ENTRANTS.md` Phase 5.
- **Auth for the roster endpoint** is unspecified: a shared static token is
  almost certainly enough for a 245-player read-only roster, but it is a
  decision, not an assumption. The snapshot-file path works with no auth story
  at all, which is a reason to build it first.
- **`complete-ratings-list.csv` is name-keyed and carries no player number**
  (`Name,Rating,Deviation,Games played`). It is not part of the contract above —
  the pull reads the DB, not this file — but it is another name-keyed artifact
  in the corpus and should be revisited when W2 lands.
