# WESPA integration

Fill in the hole `PLAN_ENTRANTS.md` decision 11 left open: where WESPA ratings
come from, and how a player who exists *only* in WESPA gets into a tournament.

> **Status: implemented** (all five phases).
>
> Phase 1 landed the local mirror and the link field, phase 2 the pull, phase 3
> the matching, phase 4 the admin page, phase 5 the guest flow. Phases 1–3 were
> indivisible (the mirror is useless without a pull, the pull writes nothing
> without the matching). Phase 5 is the one anyone actually asked for; the rest
> is what makes it correct.

## The source

`https://wespa-api.xerafin.net/players.php?idsonly=1` returns the whole list as
one JSON document — ~9,200 players, ~700 KB, no authentication:

```json
{"players": [{"playerid": 5, "name": "Adam Logan", "country": "CAN", "cswrating": 2070}, ...]}
```

`player.php?player=<id>` gives a per-player detail record (ranking, W/L/T/B,
city, photo). Nothing here needs it, so nothing here fetches it.

This is not a WESPA-run service; it is a third party's mirror of WESPA's list.
That is exactly why the fetcher was left out before, and it is the reason for
decision 2 below: Baxter must keep working when it goes away.

## Why this is not just "add a fetcher"

The manual CSV upload (`wespa_ratings.py`) refreshes `Player.wespa_rating` for
players Baxter already has. That is the *smaller* half of the problem.

**The case that matters is a player Baxter does not have.** An overseas visitor
turning up at a CoCo event has no CoCo number and no CoCo rating; the only
number anyone knows for them is their WESPA rating, and today a director types
it into the guest form by hand, from a website, at the registration desk. Making
that a search over a list Baxter already holds is the point of this work. Every
other piece below exists to make that search trustworthy.

## Decisions

1. **The list is mirrored, not merely applied.** A new `WespaPlayer` table holds
   the source's rows verbatim. Applying ratings to `Player` would let us skip
   it, but then the 9,200 players Baxter has *never seen* — the guests, the
   whole point — would be unsearchable. Same argument as the CoCo roster: it is
   mirrored so an event runs with no connection.

2. **A pull never creates a `Player`.** WESPA has 9,200 players and Baxter's
   roster is CoCo's. A `WespaPlayer` becomes a `Player` when a director enters
   one, and not before.

3. **`Player.wespa_id` is the link, and it is set by a fact where possible.**
   A guest minted from a WESPA row carries the id because a human picked that
   row. An exact name match that is unique on both sides also links — it is the
   same guess the CSV import already makes, and unlike the CSV it is now visible
   and undoable. Everything else waits for a human.

4. **Ambiguity is held back and listed; absence is not.** A name belonging to
   several players on either side links nobody and appears on the admin page for
   a director to resolve. A Baxter player with no WESPA row at all is the normal
   case for a CoCo regular and is not reported — a "pending" list containing
   most of the roster is a list nobody reads.

5. **`Player.wespa_rating` stays.** It is what the cascade reads
   (`effective_rating`), what an entrant snapshots, and the only place a
   manually-typed WESPA rating for an unlinked guest can live. A pull writes it
   from the linked `WespaPlayer`; it is a cache of the mirror, not a duplicate
   of it.

6. **The pull is unattended and records itself**, like the roster pull and for
   the same two reasons (`roster_sync.py`): a failure four times a week must be
   visible somewhere, and held-back rows must outlive the run that found them.
   `WespaSync` is `RosterSync`'s twin. Weekly, not six-hourly — WESPA ratings
   move when a rated tournament is processed, not on the hour.

7. **Still unlogged and still global.** Refreshing ratings mutates no
   replayable tournament state, because entrants pin their rating at
   registration (`PLAN_ENTRANTS.md` decision 3). This is what makes an
   unattended pull safe mid-event, and it is unchanged by any of the above.

8. **Creating a player *is* logged, and now carries the link.** `player_created`
   gains an optional `wespa_id`, so a replay into a fresh database recreates the
   guest already linked rather than as an unlinked name.

9. **The offline transport is the same document, not a CSV.** A file upload
   stays — an event with no connection can still be handed one — but it is now
   the endpoint's own JSON, exactly as the roster's snapshot file is the
   endpoint's own document, so there is one parser and one upsert. The old
   two/three-column CSV went with the stub it belonged to: it could not carry a
   `wespa_id`, so it could only ever re-guess by name, and hand-writing a WESPA
   rating file was never a real workflow — typing the rating into the guest form
   was.

10. **WESPA is not a `PlayerSource`.** The seam answers "which Baxter player is
    this" and mints numbers; a WESPA row is not a player and has no number. The
    registration page searches both and says which is which, rather than
    pretending they are one list.

---

## Phase 1 — The mirror and the link

`WespaPlayer`: `wespa_id` (unique), `name`, `country` (blank), `rating`,
`updated_at`. No FK to `Player` — the link lives on `Player.wespa_id` (null,
unique) so a player carries their own identity, as with `player_number`.

`Player.wespa_id = models.IntegerField(null=True, blank=True, unique=True)`.

### What landed

`tournaments/models.py`, migration `0044_wespa_mirror`. `WespaPlayer.rating` is
nullable, on the same convention as `Player.wespa_rating`: NULL means the list
carried no rating, which is distinct from a rating of 0 and leaves the row
searchable by name anyway.

The migration had to be numbered *before* `backfill_event_digests`, which
replays through the live models and so must always apply last; it moved from
0044 to 0045, for the third time (0038 → 0041 → 0042 → 0045). Its own comment
says to do this, and its guard fails the deploy loudly if you forget.

## Phase 2 — Fetch and record

- `wespa_api.py`: `fetch_wespa()` / `parse_wespa(raw)` → rows, with
  `WespaFetchError` / `WespaParseError` written to be read by an admin, modelled
  on `roster_import.py`'s fetch half. URL from `settings.WESPA_API_URL`, with
  the known endpoint as its default — there is no token, so unlike the roster
  there is nothing to configure before it works.
- `WespaSync` model: `RosterSync`'s twin (source, ok, error, counts, pending).
- `wespa_sync.run_sync(source, raw=None)`.
- `pull_wespa` management command + an `app.json` cron entry (weekly).

### What landed

`wespa_api.py`, `wespa_sync.py`, `WespaSync`, `pull_wespa`, and the weekly
`app.json` entry. `WESPA_API_URL` defaults to the known endpoint, so unlike the
roster there is nothing to configure before it works — and setting it empty
leaves only the upload, which is how an air-gapped install turns fetching off.

An unreadable row is fatal rather than skipped. It is tempting to drop the rows
you cannot understand and carry on, but that is a player's rating silently
failing to update, which is exactly the failure this machinery exists to make
visible.

## Phase 3 — Matching

`import_wespa(raw)` upserts the mirror, then applies ratings:

- players with `wespa_id` → exact, always;
- unlinked, exactly one player and one WESPA row share a name → link and apply;
- a name that is ambiguous on either side → `PendingLink`, nothing written.

### What landed

`import_wespa` in `wespa_ratings.py`, replacing the CSV refresh. Three things
the tests pin that are easy to break later:

- **A pull creates no players and deletes nothing.** 9,201 WESPA rows are not
  9,201 Baxter players, and a row that drops out of the list stays in the
  mirror.
- **A row already claimed by somebody is not offered to their namesake.** Two
  John Smiths where one is already linked is not ambiguous — it has one answer.
- **A link outlives a name change on either side**, which is the entire reason
  `wespa_id` exists rather than matching by name every time.

## Phase 4 — The admin page

`/players/wespa/` grows the roster page's shape: mirror size and last pull,
"Pull now", the file upload as the offline path, and the ambiguous names with a
confirm control. Plus the case the pending list deliberately excludes: guests
with no CoCo rating and no WESPA link, who are the players a missing rating
actually hurts.

### What landed

All of that, plus link-by-hand: `?link=<number>` searches the mirror starting
from the player's own name, since a spelling that is nearly right is the usual
reason a pull could not match them. The guest table lists linked guests too —
otherwise "unlink" would have had nowhere to live, and "did this guest get a
rating" would have had no answer. The admin index carries the pull's state
beside the roster's, for the same reason it carries the roster's at all: both
run on a timer now.

## Phase 5 — Guests from WESPA

The registration page's search covers the mirror as well as the player table,
marking WESPA-only hits as such. Choosing one mints a `T-` number and an
`is_provisional` player with the name, `wespa_rating` and `wespa_id` filled in,
through `create_player` — which is the flow this whole plan exists for.

### What landed

A second results table under the player search, and a `_wespa_guest` handler.
Two details worth keeping:

- **A WESPA result posts the ordinary add form.** Entering one is the same act
  as entering anybody else and should carry the same seat number, rating
  override and payment flags; it is told apart by the `wespa` id the button
  carries, rather than by a second hidden `action` field fighting with the
  first.
- **Rows already linked to a Baxter player are not offered.** Those people are
  in the results above, under the number they will actually be entered on, and
  offering both would be offering a way to mint a duplicate. The handler
  re-checks anyway, since another director may have minted one since the page
  was drawn.

## Explicitly out of scope

- Per-player detail (ranking, W/L, photo) — the bulk list has what the cascade
  needs.
- Pushing anything to WESPA. This is a one-way mirror of somebody else's number.
- Treating a WESPA rating as a CoCo one. The cascade is unchanged.
