# Plan: Unplayed games (byes and forfeits) in the ratings database

**Status: proposed** — drafted 2026-10-20; decision 4 settled the same day.
Spans this repo and `../ratings`. Code references are pinned at baxter
`dab0590` and ratings `532aaef`.

## Goal

A bye or a forfeit is part of a player's tournament record, but it is not
evidence of how well they play. The rule, as settled:

- **The rating calculation never sees an unplayed game.** It is not rated, the
  bye is never a player, and any games count that the formula reads (today
  only the multiplier's career games) counts **played games only**.
- **Everything else counts it.** That covers win/loss and spread
  (`ratings.TournamentResult.wins/losses/spread`), which must match the
  tournament's own standings, and the career games count shown anywhere,
  which includes byes and forfeits.

So there are two counts: **rated games**, used only by the formula, and
**career games**, which includes unplayed games and is used for everything
else (decision 4).

Today neither half is reliable, and the problem depends on which tool produced
the file.

## What happens today

### Baxter leaves unplayed games out

`DivisionResultsExportView` (`tournaments/views.py:1976`) exports only
`slip.is_played` rows, so a bye or forfeit never reaches `../ratings`. The
registry bundle does the same (`tournament_export.py:102`, `.played()`). A
player with a bye in a nine-player field shows a record one game short.
`pdx2032-sept2026` is like this: nine players and one player missing from every
round.

### The Google-Form corpus includes them, by accident of convention

Most hand-run files list a bye as an ordinary row, `James Curley,50,Bye,0`, and
`../ratings` handles it through three separate quirks:

| Where | What it does with a bye row |
|---|---|
| `io.ResultCSVReader.parse_row` | creates a **player called "Bye"** and puts them in the section |
| `core/types.Player.tally_results` | counts the game in W/L and spread (this is what we want) |
| `core/calculator.py:131` | skips any game where **either score is 0** ("skipping bye / forfeit"), so it is not rated |
| `core/types.Player.update_career_games` | is meant to skip byes, but compares a `Player` to the string `"Zz Bye"`, which is never equal, so **every bye and forfeit counts as a career game** |
| `RatingsDB` | carries "Bye" forward between tournaments like a real player, rated after its first appearance |
| `build_db` | skips "Bye" only because it has no `players.Player` row |
| `rating.py:74`, `players.py:52`, `io.py:449,472` | each keep their own list or test for bye names |

The zero-score rule also covers forfeits between two real players, which the
corpus records as an ordinary 50–0 game: `wgpo-earlybird-2022` has
`Zbigniew Wieckowski,50,David Eldar,0` and three like it, and `atx-2022` has
`Randi Goldberg,50,Winter,0` ("Winter" is a real player).

### Measured effect of the quirks

Run over the full history (scratch experiment, not committed):

- **The "Bye" player affects nothing.** Removing it from sections and from
  opponent lists moves **0 of 249** current ratings.
- **Byes in career games affect a lot.** Excluding unplayed games from career
  games changes career games for 108 players and moves **128 of 249** current
  ratings, by 1–3 points (Sam Rosin 1909→1912, 102→99 games; Rob Robinsky
  1932→1934, 481→475 games).

## Decisions

### 1. An unplayed game is a game result with no rated opponent

Two kinds, both already scored this way in Baxter (`plans/PLAN_FORFEITS.md` §1):

- **bye**: the player wins by the division's `bye_spread`, against the bye;
- **forfeit**: the player loses by `bye_spread`, against the bye.

A third case is an absence scored 0–0, which is a tie against the bye. Each of
these goes into W/L and spread like a played game, and nowhere else.

### 2. No new column: the bye opponent is the marker

A row is unplayed when:

1. the opponent is the bye: number `BYE` in a numbered file, or a bye name
   (the set now in `rating.py:74`, matched case-insensitively) in a legacy file;
   **or**
2. either score is 0. This is the calculator's current rule, moved into one
   predicate. It is how legacy files record forfeits between two real players.

**Rejected: an explicit `Rated` / `Game Type` column.** Every unplayed game
Baxter records is against the bye entrant. A paired game that becomes a forfeit
is split into two rows against the bye (PLAN_FORFEITS §4), so the number `BYE`
already marks them exactly. A new column would add a third header shape to both
readers (`ResultCSVReader`, `whatif_import._parse_csv`), and Google-Form files
could never fill it in, so rule 2 would still be needed. Using the bye as the
marker also means a `../ratings` checkout that does not have this change still
reads Baxter's new rows the way it reads the corpus's bye rows. They are
counted in the record and not rated, though they still count as career games
until decision 4.

Rule 2 is a heuristic: a genuine game in which someone scored 0 would be
misread. This is no worse than today, since the calculator already skips such
games.

### 3. The bye is not a player

The reader stops calling `player_for` for the bye. A bye game's opponent is a
module-level sentinel in `core/types.py`. It has `name == "Bye"` (so the `.tou`
and tabular writers keep working) and is never added to a `Section`, a
`PlayerList` or `RatingsDB`. `GameResult` gains an `is_rated` property that
implements decision 2. The calculator's skip and `update_career_games` both use
it, so the rule exists in one place. The bye-name lists in `rating.py`,
`players.py` and `io.py` move to one constant next to the sentinel.

This alone is **rating-neutral** (0 of 249 ratings move), so the golden-master
test must pass unchanged. That makes it a good refactor to land on its own.

`calculator.py` keeps its zero-score check as well as `is_rated`. Baxter's live
projection builds `GameResult`s itself (`tournaments/live_ratings.py`) and
should get the same answer whether or not it filters first.

### 4. Two counts: rated games for the formula, career games for everything else

**Settled 2026-10-20:** the formula counts only played games, and every other
use counts byes and forfeits as well.

Today one number, `career_games`, does both jobs. It feeds
`_player_multiplier` (`calculator.py:97`), and it is also what is displayed
and exported. Because of the `"Zz Bye"` bug it includes unplayed games
everywhere. That is right for the displayed count and wrong for the formula.
Splitting it:

| Count | Includes unplayed | Read by |
|---|---|---|
| `rated_games` (new) | no | `_player_multiplier`, and nothing else |
| `career_games` | **yes** | the site (`CurrentRating.career_games`, `_ratings_table.html`, the player page), `TournamentResult.games`, the "Games played" column of `complete-ratings-list.csv`, the roster API |

The split is **additive everywhere**. `career_games` keeps its name and gets
the meaning the user-facing places already assume. `rated_games` is new, and
only the formula and Baxter's live projection read it. Renaming the existing
field to mean "rated" was rejected. Baxter's event log records `career_games`
in `entrant_ratings_refreshed` payloads, so renaming it would mean a
compatibility shim to replay old logs. Old payloads stay valid as they are.

This **changes published ratings**. Taking the unplayed games out of the
multiplier's count moves 128 of 249 current ratings by 1–3 points over the
whole history (measured above). Do it as its own commit, regenerate
`tests/golden_all_ratings.txt` (`UPDATE_GOLDEN=1`), and regenerate
`plans/rating-history.md`. Displayed career games do not change, since they
already include byes.

**Where the two counts come from.**

- **Results files:** each game adds 1 to `career_games`; it adds 1 to
  `rated_games` only if `g.is_rated`.
- **Ratings files** (`.RT`, CSV ratings lists): these seed a player's first
  appearance and carry a single games number. It seeds both counts, since
  the file cannot tell them apart. The `.RT` writer is a rating-input format
  that round-trips into the engine, so it writes `rated_games`.
  `CSVRatingsFileWriter` ("Games played") is for people to read, so it
  writes `career_games`.

**The roster** (`coco.roster/1`, `web/ratings/roster.py`) adds `rated_games`
next to `career_games`. Adding a field is compatible: a Baxter that does not
know `rated_games` keeps reading `career_games`, which still includes byes.
That is no worse than it is today.

### 5. Baxter exports every slip, played or not

The CSV export writes not-played slips as well. The bye side is written as
name `Bye` and number `BYE`, which are `BYE_PLAYER_NAME` / `BYE_PLAYER_NUMBER`.
The bye is never renamed, so the name is stable. A forfeit is written
`Bye,50,<absentee>,0`, which means the bye is the winner. This is the slip's
orientation, and decision 2 handles either orientation.

`withdrawal = omit` still produces no rows for the rounds a withdrawn player
missed, which is correct: in that mode they did not lose those games, they
stopped playing.

### 6. `../ratings` changes first, then Baxter

Baxter pins `coco-ratings` to the ratings repo's `main` through `uv.lock`. If
Baxter exported bye rows before the reader understood `BYE`, the old reader
would create a player keyed `BYE`. `RatingsDB.key_for` would then move the
corpus's name-keyed "Bye" record onto that number. That is harmless to ratings (the
"Bye" player measurably affects nothing) but confusing to untangle. So the ratings change merges first,
then Baxter runs `uv lock --upgrade-package coco-ratings` and changes the
exporter.

### 7. Backfill only from an authoritative source; never infer

The git history of `results/` is the source of truth, so a backfill is an
edited CSV, committed. A round where a player is missing looks the same
whether they had a bye, forfeited, arrived late, withdrew without forfeits, or
the round number was mistyped. The corpus has all of these (see the audit
below). So a missing game is filled in only from a record that says what
happened: Baxter's database, the tournament's sheet workbook, or the
director. Gaps that cannot be explained that way are listed as known gaps,
not guessed at.

## Phases

### Phase 1: the bye is not a player (`../ratings`, rating-neutral)

- Add the bye sentinel, a `BYE_NAMES` constant, and `GameResult.is_rated` in
  `core/types.py`. Keep `core` stdlib-only, as `test_core_isolation` requires.
- `ResultCSVReader.parse_row`: if either side is the bye (number `BYE` or a bye
  name), give the real player a `GameResult` against the sentinel and register
  only that player. A row with the bye on both sides is a parse error.
- `TouReader`: check how `.tou` files mark byes before changing it. No `.tou`
  file is in `results/` today, so the minimum is that it keeps parsing what it
  parsed before.
- Calculator: skip `not g.is_rated` (plus the existing self-opponent and
  zero-score checks). `get_opponents` excludes the sentinel, which matters for
  the unrated-opponent ratio in `calc_initial_ratings`.
- Remove the scattered bye-name lists in `rating.py`, `players.py` and `io.py`.
  `PlayerDB.get_id("bye") → "0000"` should become dead code; confirm and delete
  it.
- `build_db` no longer meets a "Bye" to skip. Update its comment and the
  skipped-names test, if it names Bye.

*Verify:* `make test` with the golden test **unchanged and passing**, and also
run locally, since CI skips golden. Add unit tests: a bye row gives the player
a win, +50 spread, no rating effect and no section entry for the bye; a forfeit
row (`Bye,50,X,0`) gives a loss and −50; a 0–0 bye row gives a tie; a numbered
file with `BYE` creates no `BYE` key in `RatingsDB`. `build_db`'s output no
longer flags "Bye".

### Phase 2: rated games vs career games (`../ratings`, moves ratings)

- `core.Player` gains `rated_games`, alongside `career_games`, as a
  constructor argument with default 0. `update_career_games`, which should
  probably be renamed `update_game_counts`, adds every game to `career_games`
  and only `g.is_rated` games to `rated_games`. This replaces the
  `"Zz Bye"` comparison.
- `_player_multiplier` reads `rated_games`.
- `PlayerRecord` carries both counts. `RatingsDB.adjust_tournament` carries
  both forward. The ratings-file readers seed both from their one column
  (decision 4).
- `CurrentRating` gains a `rated_games` column (migration). `build_db` fills
  it. Display keeps showing `career_games`.
- The roster API adds `rated_games`. Update `test_roster` and the contract
  description in `roster.py`.
- Regenerate the golden file and `plans/rating-history.md`. Record the
  largest movers in the commit message. The golden snapshot should include
  both counts, so a later change cannot quietly swap them.

*Verify:* the golden diff shows changed ratings and a new `rated_games`. It
should show **no** change to `career_games`, W/L or spread. Spot-check a
heavy-bye player, such as anyone from `mn-2022` (5 players, 15 bye rows):
`career_games` should be unchanged, and `rated_games` should be lower by
their bye count.

### Phase 3 (optional): show it on the site (`../ratings`)

`TournamentResult` gains `unplayed` (an int, the count of unrated games).
`PlayerReport` carries it and `build_db` fills it. The player page shows
"8–3 (1 bye)", or similar, so a record that does not match the rated games
explains itself. This is a projection field, rebuilt every deploy, so there is
no data migration beyond adding the column.

### Phase 4: Baxter exports unplayed games, and seeds its projection from rated games

- `uv lock --upgrade-package coco-ratings` to pick up phases 1–2.
- **Rated games in the live projection.** `Player` and `Entrant` gain
  `rated_games` (migration, default 0). Add it to `entrant_sync.SEED_FIELDS`
  and `current_seed`, to the registration defaults (`models.py:887`) and to
  `player_ratings`' rating-field set. `roster_import` reads `rated_games`, or
  `career_games` when an older roster or snapshot file lacks it.
  `live_ratings` passes `rated_games` to the core `Player`. Baxter keeps
  syncing `career_games` but nothing in Baxter reads it for the calculation.
- **Replay:** the `entrant_ratings_refreshed` command reads `row[field]`
  strictly (`commands.py:1025`). Payloads logged before this have no
  `rated_games`, so take `career_games` for it when it is missing. Neither
  count is in `division_digest`, so the digest does not move.
- `DivisionResultsExportView`: export every slip and remove the `is_played`
  filter. Use `BYE_PLAYER_NAME` / `BYE_PLAYER_NUMBER` for the bye side rather
  than `slip.winner.name`, so the marker does not depend on the bye player
  row's name.
- Update the `results_export.py` docstring, which documents the column
  contract, and the comment in the view.
- `test_results_export`: extend the test that runs the real `ResultCSVReader`
  so that it covers a bye, a forfeit and a 0–0 absence. It should check that
  the records come out right and that the bye is in no section.

*Verify:* export a division that has an odd field and a withdrawal
(`withdrawal = forfeit`), then rate it through `coco-rate`. W/L should match
Baxter's standings page exactly, and ratings should match an export with the
unplayed rows removed.

### Phase 5: the other readers of the format (Baxter)

- **What-if import** (`whatif_import._parse_csv` → `division_imported`): a row
  naming the bye has to become a slip against `division.bye_entrant()`. Today
  `entrant("Bye")` calls `resolve_player(None, "Bye")`, which finds the
  singleton bye `Player` by name and enters it as an ordinary entrant numbered
  1000+. That bug **already exists** for any legacy corpus file with bye rows,
  so this fixes it as well. Bye inference stays for files that leave byes out.
  An entrant who has a bye row is in `played` and gets no inferred bye.
- **Registry bundle** (`tournament_export.py`): include not-played results so
  that the two exports agree. The bundle has no reader except what-if, and the
  upload transport is not built (`project_registry_sync`), so this is cheap
  now and harder later. `ExportResult` gains nothing: the bye's player number
  `BYE` is the marker here too, and `players` keeps excluding it.

*Verify:* round-trip a division with a bye and a forfeit through both export
formats and the what-if import. The sandbox standings should equal the
original's, and no entrant should be the bye player.

### Phase 6: backfill (`../ratings` `results/`)

**6a. Baxter-produced files.** `pdx2032-sept2026` and `vancouver-sept2026`
are the only files with number columns. Re-export both from production Baxter
with the phase-4 exporter and replace the files. The diff must only **add**
rows, and every added row must involve `BYE`. If any existing row changed,
stop and find out why before committing. Both are dated after the golden
cutoff (`2025-12-31`), so golden is unaffected. Ratings should not move, since
unplayed games are unrated and, after phase 2, not counted in `rated_games`.
Only `career_games` and W/L should change, for the players who had a bye or forfeit. Check that
with `coco-rate` before and after.

`vancouver-sept2026` has 8 players but only 2–3 games a round from round 4
on, which looks like a playoff or split schedule rather than byes. The
re-export settles it either way.

Check for any other division run in Baxter whose results reached `../ratings`
through a path that dropped the number columns. The 2026 files without number
columns are the candidates. Ask whoever imported them.

**6b. Legacy gaps.** Commit the scan as `scripts/audit_unplayed.py`. For each
results file, it reports rounds where a player is absent or appears twice.
Its current output, with files that already have bye rows and no gaps
omitted:

| Tournament | Players | Gaps | Likely cause (to be confirmed) |
|---|---|---|---|
| osc-2022 | 11 | one missing every round | odd field, byes omitted |
| pdx2032-sept2026 | 9 | one missing every round | Baxter export (6a) |
| wordcup-2025-d1 / d2 | 33 / 28 | 1–3 missing every round | odd field and/or late entry |
| wordcup-d1-2022 / d2-2022 | 39 / 32 | one missing from r8 / r22 on | withdrawal |
| nacc2023-d2, austin-atx2024, seattle-may2024 | | one missing from some round on | withdrawal |
| rome-may2025, glenhaven-2024 | 6 | 2–4 missing in late rounds | early finish / playoff |
| loco-2021, brattleboro-2022, potomac-2022, slingerlands-2023 | | a player twice in one round, absent from the next | mistyped round number (fix the round, not a bye) |
| seattle-sept2023, riorancho-oct2023, nola-lite-2025, bend-2022, portland-pub-13apr2025, seattle-jul2023 | | scattered | unknown |

For each file, find the source: the sheet workbook (but note
`sheet_workbooks_are_snapshots`: withdrawals and the Bye row overwrite their
own history, so a workbook may not be able to say), or the director. Then
either add the rows (`X,50,Bye,0` / `Bye,50,X,0`), fix the round number, or
add the file to a known-gaps list in the script with the reason. Files dated
before the golden cutoff need a golden regeneration. Do this **after** phase 2
so the regeneration happens once.

*Verify:* the audit script reports nothing that is not on the known-gaps
list. Optionally run it in CI so a new file with a gap fails unless it is
listed.

## Order and commits

1. ratings phase 1 → ratings phase 2 (separate commits; deploy). Phase 2
   changes the roster, so Baxter's phase 4 should follow soon. Until it does,
   Baxter's projection uses `career_games` as it does today.
2. ratings phase 3 (optional, any time after 1)
3. baxter phase 4 → baxter phase 5
4. backfill 6a (needs 4 deployed to production Baxter) → 6b, file by file

## Open questions

- **Phase 3**: show unplayed games on the site, e.g. "8–3 (1 bye)", or only
  fix the counts?
- **6b**: how far back is it worth chasing sources for legacy gaps? The
  rating-neutral answer is "only where W/L is visibly wrong to someone".
