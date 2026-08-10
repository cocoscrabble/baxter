# Plan: Championship playoffs (brackets + best-of series)

**Status: implemented** (all five phases, 2026-08-10). Design drafted 2026-08-09
for [issue #44](https://github.com/cocoscrabble/baxter/issues/44); code
references pinned at commit `96aa83d`.

Implementation notes, where the build differed from the design:

- **A scheduled game can never become unnecessary.** "Schedule game *i* only when
  it is certainly needed" turned out to be stronger than expected: a game that
  qualifies cannot be retired by any later result. The pruner therefore only
  fires on a *correction* to an earlier game, which is what its test now covers.
- **A published window is repaired in both directions.** Publishing a whole
  window up front leaves its later rounds empty (nothing is needed yet); when a
  series then goes 1–1, the decider has to be added to an already-published
  round. `_add_missing_playoff_games` is the mirror of the pruner.
- **An empty playoff round closes only when nothing can still appear in it** —
  a round holding a `pending` game stays open, or the decider would be stranded.
- **`publish_rounds` regenerates first for a playoff division**, so publishing
  never ships a stale window and the live path matches replay (which regenerates
  before every publish event).
- **Two pre-existing bugs surfaced and were fixed**: `simulate_round_cmd`'s
  replay branch skipped the status/prune refresh when it had no results to
  apply, and `RoundPairings.update_status` could not reach FINISHED in one call
  when the last outstanding result was a bye (found by the fuzzer).

## Goal

First-class 2/4/8-player championship playoffs attached to an existing division,
so a director stops modelling them with fixed pairings, duplicate divisions and
temporary withdrawals.

- **Top 2** — one championship series.
- **Top 4** — semifinals (1–4, 2–3), championship, third place.
- **Top 8** — quarterfinals (1–8, 4–5, 2–7, 3–6), semifinals, championship,
  third place, and a consolation half deciding 5th–8th.
- Placement series are optional in postscript mode and **mandatory in concurrent
  mode**, where an eliminated bracket player with nothing to play would be the
  only idle person in the room.
- Series length configured **per series** (single-game QFs, best-of-3 semis,
  best-of-5 final is a legal combination).
- Two timing modes: **postscript** (main event stops at the qualification round)
  and **concurrent** (everyone else keeps playing the configured schedule).
- Final placement is **bracket-derived**, not aggregate playoff record.

First concrete use case: the NACC Final Four — best-of-3 semis, best-of-3
championship, best-of-3 third place, postscript.

## The central design decision: the bracket is *derived*, not stored

Baxter already has one hard rule (`CLAUDE.md`, `plans/PLAN_EVENT_LOG.md`): the
event log records a command's **inputs**, and everything else is recomputed. The
playoff fits that rule exactly if the only recorded intent is the playoff's
*configuration plus its confirmed seed snapshot*, and every later question —
who is in which series, what the series score is, which games still need to be
played, who finished third — is a pure function of

    (playoff config + confirmed seeds) × (the division's result slips)

That single decision resolves most of the issue's harder requirements for free:

- *"Only unresolved series receive their next pairing"* — the generator only
  ever creates the games the derivation says are needed.
- *"Unnecessary games create no pairing, bye, result, standings change, or
  ratings/export row"* — a game that is never generated cannot leak anywhere.
- *"A score correction ... must recompute future games safely"* — correcting a
  result changes the derived bracket, and the next `regenerate_pairings` rebuilds
  the draft rounds from it. Only *already-published* downstream state needs
  explicit handling (see "Result corrections" below).
- *"Event-log replay reproduces qualification seeds, timing mode, bracket state,
  pairings, results, and final placements"* — replaying `playoff_created` plus
  the same result events reproduces the bracket by construction.

So there is no series state machine to advance, no `Entrant.dropped` mutation, no
per-round participation bookkeeping to keep in sync. There is a pure function and
a generator that materializes its output into `Pairing` rows.

## Decisions

Answers to the issue's open decisions, plus the ones the design forces.

| Question | Decision | Why |
| --- | --- | --- |
| Tied game / series with no outright majority | A drawn game scores 0.5 to each. A series is **clinched** when a player's score is strictly more than half of `max_games`. If all `max_games` are played and neither is over half, the series is decided on **cumulative spread across that series' games only**. If the series spread is *also* level, the series state is **`tied`**: it shows as "tied — needs a decider", nothing advances, and nothing further is inferred. | Series spread is the standard decider. Note the scope: the issue forbids silently falling back to *tournament-wide* cumulative spread; a series-local tiebreak that the bracket displays as the reason a series was won ("won on series spread, +112") is explicit, not silent, and touches no game outside the series. The residual `tied` state cannot be designed away — a drawn one-game series has series spread 0–0 by construction — but it is now rare. The "strictly more than half" form reduces to the issue's `floor(N/2)+1` for whole wins while handling a drawn game sensibly (win + tie + tie = 2.0 of 3 *is* a majority). |
| Who goes first in a playoff game | Baxter's existing starts rule, replayed through the Python `Starts` class (`tournaments/pairing/base.py:330`): fewest starts so far, then head-to-head, then recency. | Confirmed with the requester. Keeps one rule in the codebase; a series naturally alternates under it because the two players' start counts stay within one of each other. |
| Compressed schedule (next stage starts early when every series clinches early) | Not built. Stage windows are fixed. | The issue's stated initial behavior. The derivation already knows a stage is complete, so a later `compress_schedule` flag is a small change to the window math and nothing else. |
| May a clinched participant rejoin ordinary pairing before their stage window ends (concurrent)? | No — reserved through the stage window. | The issue's stated initial behavior; keeps the main field's schedule predictable. |
| May an *eliminated* participant rejoin ordinary pairing (concurrent)? | No — a player pulled into the bracket never returns to the ordinary pairing pool. Reserved from the qualification round to the end of the playoff. | Confirmed with the requester. Predictable: entering the bracket takes you out of the main field, full stop. The alternative churns the main field's roster mid-event and lets a bracket player take games off main-track contenders after their own placement is already decided. |
| Placement brackets beyond an optional third-place series | **Built.** The 8-bracket carries a full consolation half: quarterfinal losers meet in the semifinal window (5th–8th semifinals), and their winners/losers meet in the final window for 5th and 7th. Every placement series is optional in postscript mode and **mandatory in concurrent mode**. | This answers the issue's open question, and concurrent mode forces it: a quarterfinal loser is reserved from the main field for the rest of the playoff, so without a consolation half the four eliminated players would be the only people in the room with nothing to play. The same argument applies one size down — 4 qualifiers concurrent requires the third-place series, or the two semifinal losers sit out the whole final window. |
| Store series state, or derive it? | Derive. `PlayoffSeries` rows exist (the issue asks for the layer, and `Pairing` needs something to point at) but hold only *structure*: series key, bracket position, participants once known, max games, window start. Score, winner, loser and status are computed, never written. | "Derive required games and series winners/losers from recorded results; do not store redundant manually editable standings." |
| New pairing strategy (`RP.Playoff`)? | No. The playoff is not a strategy — it is an overlay that owns certain rounds. `STRATEGY_TYPES` is untouched. | A strategy would have to reach into bracket state from inside the Rust engine; the overlay keeps the engine ignorant of playoffs except for one generic primitive (per-round inactive players). |
| Should the *engine* own the playoff — bracket structure, field split and matchups — so that pairings are only ever arranged in one place? | No. The bracket is derived in the app; the engine gains one generic capability (`inactive_players`) and no playoff knowledge. See below. | The engine's value-add is irrelevant here, and the bracket has many more consumers than pairing. |

### Why the bracket is not computed in the engine

The tempting version of this design puts the whole playoff in the Rust crate: the
engine reads the bracket config, splits the field, and emits both the playoff
games and the ordinary ones, so pairings are only ever arranged in one place.
That single-locus argument is real, but it does not survive contact with what a
bracket actually is here.

- **It is not a pairing problem.** There is no matching, no optimization, no
  RNG, no repeat avoidance: the championship series is
  `winner_of(SF/0) vs winner_of(SF/1)`. Every capability the engine exists for —
  min-cost matching, COP's Monte Carlo contention, the round-robin solver, the
  seeded RNG — is irrelevant to computing it.
- **Pairing is the smallest consumer of bracket state.** The public bracket page,
  final placements, the state digest, the result-correction guard, the series
  status badges, and the pruner that removes clinched-away published games all
  need it too. Deriving it in Rust means either duplicating the logic in Python
  (the worst outcome) or designing and versioning a *second* boundary type — a
  `bracket_json` carrying series states, winners, losers and placements. That is
  a considerably larger boundary than the one it avoids, and it makes reading a
  bracket in a Django shell depend on the extension being built.
- **The engine deliberately does not model what a series needs.** Stage windows,
  round numbers, and draft-vs-published lifecycle are `DivisionSettings` /
  `RoundPairings` concepts; the engine knows only names, ratings, results and
  per-round strategies. `can_pair` and `round_status` would both have to grow
  playoff-awareness on top. Its output type `OutPairing {first, second, repeats}`
  has no room for a series or game number either.
- **What genuinely belongs in the engine is one primitive, not a feature.**
  "Pair this round without these people" (`inactive_players`) is domain-agnostic,
  is the honest fix for `round_status` in a mixed concurrent round, and earns its
  keep independently of playoffs: Baxter currently cannot express "excuse this
  player for one round without withdrawing them", which directors do ask for.

The concession to the single-locus instinct: the derivation is specified as a
**pure function over `PairingData`** — no ORM access, the same input shape the
engine adapter serializes — so if the desktop/wasm story ever wants it in Rust,
the port is mechanical rather than a rewrite. Python already keeps `Repeats`,
`Starts` and standings on the same terms.

## Data model

New models in `tournaments/models.py`, one migration (`0035_playoff`).

```python
class Playoff(models.Model):
    division            = OneToOneField(Division, related_name="playoff")
    qualification_round = IntegerField()          # seeds come from standings after this round
    qualifier_count     = IntegerField()          # 2, 4 or 8
    timing              = CharField(POSTSCRIPT | CONCURRENT)
    stage_games         = JSONField()             # games per series, by series key:
                                                  # {"quarterfinal": 1, "semifinal": 3,
                                                  #  "championship": 3, "third_place": 3,
                                                  #  "consolation_semifinal": 3,
                                                  #  "fifth_place": 3, "seventh_place": 3}
                                                  # a placement key absent/0 = that series is
                                                  # not played (postscript only)
    seeds               = JSONField()             # confirmed snapshot, seed order:
                                                  # [{"seed": 1, "player": "…", "wins": 5.0,
                                                  #   "spread": 412}, …]
```

`seeds` is the audit record the issue asks for: the standings snapshot as
confirmed (or overridden) by the director, by player **name**, so it replays into
a fresh DB. There is no `state` field — a playoff exists or it does not, and
whether it is finished is derived.

```python
class PlayoffSeries(models.Model):
    playoff     = FK(Playoff, related_name="series")
    key         = CharField()                     # "quarterfinal", "semifinal",
                                                  # "consolation_semifinal", "championship",
                                                  # "third_place", "fifth_place", …
    position    = IntegerField()                  # bracket slot within the key, 0-based
    high        = FK(Entrant, null=True)          # better-seeded participant, once known
    low         = FK(Entrant, null=True)
    max_games   = IntegerField()
    start_round = IntegerField()                  # first round of this series' window
    class Meta: unique_together = [("playoff", "key", "position")]
```

Derived rows, like `RoundPairings`: upserted by the generator on
`(playoff, key, position)` and **never deleted** while the playoff exists, so
`Pairing.series` stays valid. Advancement is structural, not stored — it comes
from the bracket template below.

```python
class Pairing(models.Model):          # two new nullable fields
    series      = FK(PlayoffSeries, null=True, on_delete=SET_NULL, related_name="pairings")
    game_number = IntegerField(null=True)         # 1-based within the series
```

Nothing else changes. `Entrant.dropped` is untouched; no entrant is duplicated.

## Bracket derivation — `tournaments/playoff.py`

A new module, ORM-light in the same spirit as `tournaments/pairing/base.py`: it
takes the `Playoff` row's plain fields plus a `PairingData` (which already
carries every result slip, `tournaments/pairing/base.py:120`) and returns a
frozen `Bracket`. No writes, no queries.

### Bracket templates

The bracket is a static template per qualifier count: an ordered list of
**windows**, each holding the series played in parallel during it. A series names
its two participants by *source* — a seed, or the winner/loser of an earlier
series — and, if it is terminal, the pair of places it decides.

```
Source ::= Seed(n) | Winner(key/pos) | Loser(key/pos)

2 qualifiers
  window 0  championship/0          = Seed1, Seed2                    → places 1,2

4 qualifiers
  window 0  semifinal/0             = Seed1, Seed4
            semifinal/1             = Seed2, Seed3
  window 1  championship/0          = W(sf/0), W(sf/1)                → places 1,2
            third_place/0           = L(sf/0), L(sf/1)                → places 3,4

8 qualifiers
  window 0  quarterfinal/0          = Seed1, Seed8
            quarterfinal/1          = Seed4, Seed5
            quarterfinal/2          = Seed2, Seed7
            quarterfinal/3          = Seed3, Seed6
  window 1  semifinal/0             = W(qf/0), W(qf/1)
            semifinal/1             = W(qf/2), W(qf/3)
            consolation_semifinal/0 = L(qf/0), L(qf/1)
            consolation_semifinal/1 = L(qf/2), L(qf/3)
  window 2  championship/0          = W(sf/0), W(sf/1)                → places 1,2
            third_place/0           = L(sf/0), L(sf/1)                → places 3,4
            fifth_place/0           = W(csf/0), W(csf/1)              → places 5,6
            seventh_place/0         = L(csf/0), L(csf/1)              → places 7,8
```

The consolation half is what keeps every bracket player occupied in every window:
with it, all 8 (or all 4) play in each window, which is exactly what concurrent
mode needs. It also makes placement fall out of the bracket for all eight
players rather than only the top four.

**Disabling placement series** (postscript only). Setting a placement series'
games to 0 removes it; disabling `consolation_semifinal` removes `fifth_place`
and `seventh_place` with it. The removed players are then placed by
qualification-snapshot order (see Final placements). Concurrent mode rejects the
configuration at form level: every series in the template must be present.

**Windows.** Contiguous from `Q + 1`, where `Q` is the qualification round:

```
len(window) = max(stage_games[key] for each enabled series in it)
playoff_rounds = Q+1 … Q + Σ len(window)
```

Placement series default to their window's length so nobody in the window runs
out of games early; a director may shorten one, and in concurrent mode the form
warns that doing so idles those players for the remaining window rounds.

Seeds come from the confirmed snapshot, not recomputed at render time.
`high`/`low` within a series is by qualification seed, which also fixes display
orientation. (It does **not** decide who starts — that is the starts rule.)

**Residual idleness.** Two cases remain, both inherent to fixed windows and both
bounded by a window rather than by the rest of the event: a series that clinches
early (2–0 in a best-of-3) idles its two players for the last window round, and a
deliberately shortened placement series does the same. The compressed-schedule
option noted in Decisions is the eventual fix for both; neither is the
"eliminated players have nothing to do for the rest of the playoff" problem the
consolation half solves.

### Series state

For each series, walk its games in order. Game *i* is played in window round
`start_round + i - 1`; its result is the result slip for that round whose two
entrants are the series participants.

```
score(p)   = wins + 0.5 × ties                     (over the series' played games)
sspread(p) = Σ (own score − opponent score)        (over the series' played games)

clinched      ⟺ score(p) > max_games / 2
decided_on_spread ⟺ all max_games played ∧ neither clinched ∧ sspread differs
status        ∈ {pending, scheduled, in_progress, clinched, tied}
```

- `pending` — participants not yet known (an upstream series is unresolved).
- `clinched` — has a winner, either on majority or on series spread; the
  derivation records *which*, so the bracket can say "won on series spread
  (+112)" rather than presenting it as a normal series win.
- `tied` — all `max_games` played, neither over half, and series spread level.
  Guaranteed for a drawn one-game series (spread 0–0); otherwise rare. Nothing
  advances and no further game is scheduled until the director acts.
- `winner`/`loser` are `None` unless `clinched`.

Note that spread only ever settles a *completed* series: it cannot end one early,
and it never reads a game outside the series. There is also no early
"mathematically undecidable" case to detect — a win always adds a whole point, so
a live series can always still reach the threshold, and the scheduling rule below
covers everything up to the final game.

**Which games to schedule.** Game *i* of a live series is scheduled iff it cannot
already be unnecessary — that is, iff no participant's *best case* score after
games `1 … i-1` (known results plus every unplayed earlier game awarded to them)
exceeds half of `max_games`. Consequences, all desirable:

- games `1 … wins_required` are always scheduled, so a director can print the
  first two games of a best-of-3 up front;
- game 3 of a best-of-3 appears only once game 2 is in and the series is 1–1;
- a clinched series schedules nothing further;
- a `tied` or `pending` series schedules nothing.

### Final placements

```
final_placements(division) ->
    for each terminal series in the template, in place order:
        its two places go to the series' winner and loser once it is decided
    any bracket player not placed by a terminal series (because the placement
        series is disabled, or its series is still live/tied) keeps the block of
        places their elimination stage allows, ordered by qualification snapshot
    every non-playoff entrant, in standings order after the division's last round
```

With the full 8-bracket enabled, the first clause places all eight; the second is
only reached for a postscript playoff that disabled placement series (its 5th–8th
then order by qualification seed) or for a bracket still in progress.

### Determinism, especially in postscript mode

Postscript is the mode where this needs care: eliminated players stop playing
while the finalists keep going, so games played differ across the field and
ordinary standings order is meaningless between them. The placement order above
is nonetheless a total order over deterministic inputs, and every input is either
a recorded result or the recorded seed snapshot.

The snapshot is what makes it robust. `Results.standings()`
(`tournaments/pairing/base.py:303`) sorts on `(-score, -spread)` with a stable
sort and **no further tiebreak**, so two players on identical wins *and* spread
fall back to result-slip iteration order — and `ResultSlip.Meta.ordering` is
`["round"]` only, leaving intra-round order to the database. Freezing the seeds
at confirmation removes that ambiguity from the playoff entirely: the one place a
human judgement is genuinely required (who takes the 4 seed when seeds 4 and 5
are dead level) is asked once, answered by the director, recorded by name, and
replayed verbatim. Three consequences the implementation must honor:

- **The non-playoff tail has no snapshot** — it is live standings, so it inherits
  that same tie ambiguity. `final_placements` therefore applies an explicit final
  tiebreak of **entrant number** after wins and spread, making the tail a total
  order by construction. This tiebreak goes in `final_placements` **only**;
  `standings_after_round` must not change, because it feeds `division_state`
  (`tournaments/events.py:288`) and re-ordering exact ties there would perturb
  digests already recorded for existing tournaments and break their replay
  verification.
- **An undecided series yields no places.** A `tied` or still-live series leaves
  its two places explicitly unresolved in the placement list ("championship
  undecided"). The elimination-block fallback must not quietly seed-order them,
  which would present an unfinished bracket as a finished one.
- **A withdrawal inside the bracket is recorded as a forfeit result**, not as
  `Entrant.dropped`. Dropping a bracket player would stall their series forever —
  it can never reach a majority, so nothing downstream is ever scheduled. A
  director-entered forfeit score is an ordinary result slip and clinches the
  series through the normal derivation. The setup preview likewise does not offer
  an already-dropped entrant as a qualifier.

A presentational corollary, not a determinism one: with a playoff attached, the
ordinary standings table compares players who have played different numbers of
games. The standings page therefore carries a banner — "Final placement is
determined by the playoff bracket" — linking to the placements, so nobody reads
the raw table as the finishing order.

The non-playoff tail needs no branch on timing mode: in postscript mode those
entrants have no games after the qualification round, so "standings after the
last round" *is* their qualification-round order. Playoff participants are
removed from that tail and placed by bracket. Records and ratings still count
every played game — placement simply does not come from them. This is exactly
the 2023 Division Two case (runner-up 3–3, third place 3–2) and it becomes a
regression fixture.

## Pairing generation

All of it lands in `regenerate_pairings` (`tournaments/generate_pairings.py:170`),
which already is the one place derived pairings are built, is `@as_derived`, and
is atomic.

```
pd = PairingData.for_division(division)
playoff = getattr(division, "playoff", None)
if playoff:
    br = build_bracket(playoff, pd)
    upsert_series_rows(playoff, br)             # structure only
    pd.inactive_players = br.reserved_names_by_round()   # concurrent mode only
pairings = pair_with_engine(pd)                 # unchanged call site
...
for round_num in every draft round (engine rounds ∪ playoff rounds):
    resolved  = engine pairings for the round        (concurrent: the main field)
    resolved += playoff games the bracket scheduled  (oriented via Starts)
    → existing sort-by-rank / assign_tables / Pairing.objects.create path
```

Details:

- **Postscript**: playoff rounds are not in `DivisionSettings.round_pairings`, so
  the engine returns nothing for them. The generator creates their
  `RoundPairings` (DRAFT) itself and fills them with playoff games only. No bye
  is created and no non-participant is touched — the "inactive without being
  dropped" requirement is satisfied by simply not generating anything for them.
- **Concurrent**: the round *is* in the schedule; the engine pairs it with the
  reserved players marked inactive (see the engine change), and the playoff games
  are appended before table assignment. Playoff games sort to the top boards
  naturally, since participants are top-of-standings; fixed tables still apply.
- **Orientation**: build a Python `Starts` from every finished result slip in
  round order, then `starts.add()` each playoff game. Same rule the engine
  applies to ordinary games.
- **Repeats**: a best-of-N legitimately repeats a pairing; `Pairing.repeats`
  carries the running count, which the display already shows.
- **Validation** (form-level, so the director sees it before confirming):
  postscript requires `qualification_round == last configured round`; concurrent
  requires the configured schedule to extend through the last playoff round, and
  requires every placement series in the template to be enabled (so no reserved
  player is left without a series while the rest of the room plays).

### Engine change (concurrent mode only)

One generic addition to the Rust crate — the engine learns about *inactive
players for a round*, not about playoffs:

```rust
// scrabble-pairing/src/model.rs, PairingInput
#[serde(default)]
pub inactive_players: HashMap<i32, Vec<String>>,   // round -> names sitting this round out
```

Honored in three places in `scrabble-pairing/src/pair.rs`:

1. `pair_round` — union the round's inactive names into the `excluded` set fed to
   `Ctx`/`standings_after_round`, so the strategy never sees them;
2. `bye_pairing` — same exclusion, so the odd/even test and the bye recipient are
   computed over the *active* field;
3. `round_status` — a round is finished when every *expected* real player has a
   result, i.e. `n_real - inactive(round)`. Without this a mixed concurrent round
   never reads `Finished` and the following ordinary round never pairs. This is
   the subtle one and gets its own `cargo test`.

`#[serde(default)]` keeps the frozen `tests/corpus/cases.json` parsing. The
round-robin family and COP take the full-field path in `pair_round`; for those,
inactive players are filtered out of the player list before dispatch (COP) or the
configuration is rejected at form level (a concurrent playoff overlapping a
round-robin block cannot be satisfied).

## Result lifecycle

- **Entering results** is unchanged: playoff games are ordinary `Pairing` rows,
  so `ResultSlipCreateView`, the results grid, scorecards and the public pairings
  page all work as they stand.
- **Clinching away a published game.** Generation never creates an unnecessary
  game, but a director may have published a window round before the previous
  game clinched the series. After every result write, `prune_playoff_pairings`
  (a derived write) deletes any *published, unplayed* pairing whose series the
  bracket no longer schedules. No result, no bye, no zero score — the game simply
  ceases to exist, which is the behavior the issue asks for and the one place the
  design deliberately departs from tsh's `ZeroOutPartials`.
- **A playoff round that ends up with no games** (both semis clinched 2–0, window
  round 3 empty) is marked `FINISHED` by the pruner. `RoundPairings.update_status`
  keeps its `total > 0` guard (`tournaments/models.py:582`) — that guard protects
  unpaired round-robin rounds and must not be relaxed; the playoff module owns
  this transition and comments why.
- **Result corrections.** After a result write, recompute the bracket and compare
  against recorded results: if a change would alter the participants of a series
  that *already has results*, the write is rejected with an explicit message
  ("Changing this result would change who plays in the championship series, which
  already has games recorded"). Because commands are atomic, raising rolls the
  write back cleanly. The grid path reports it through `prepare`'s error list.
  Downstream *unplayed* state needs no protection — it is regenerated. An
  explicit confirmed-rollback flow (delete the downstream results, then apply) is
  left as a follow-up; blocking satisfies the acceptance criterion.

## Commands, event log, replay

Three new event types in `EVENT_TYPES` (`tournaments/events.py:28`) and commands
in `tournaments/commands.py`:

| Event | Payload | Notes |
| --- | --- | --- |
| `playoff_created` | `{division, qualification_round, qualifier_count, timing, stage_games, seeds: [names in seed order]}` | The whole intent, name-keyed. |
| `playoff_updated` | same shape | Only while no playoff game has a result. |
| `playoff_deleted` | `{division}` | Same precondition; deletes the `Playoff` and its series (pairings in playoff rounds are draft and regenerate away). |

`division_state` (`tournaments/events.py:245`) gains a `"playoff"` key **only when
a playoff exists** — config, per-series derived state, and final placements — so
existing tournaments' recorded digests are bit-for-bit unchanged while playoff
state becomes digest-verified. `replay.py` needs no new machinery: the commands
register themselves, and `NEEDS_REGEN` already covers the publish events that
consume draft pairings.

## UI surfaces

| Surface | Route | Contents |
| --- | --- | --- |
| Setup (editor) | `D + "playoff/setup/"` | Qualification round, qualifier count, timing, per-series max games, and per-placement-series enable toggles (locked on, with an explanation, in concurrent mode). Live preview of qualifiers and the whole bracket from standings at the chosen round; seeds editable before confirming. Confirm → `playoff_created`. Edit/delete while no playoff result exists. |
| Bracket (public) | `D + "playoff/"` | The bracket: each series shows participants and seeds, series length ("best of 3"), running score, status badge (**Scheduled / In progress / Clinched / Tied / Not necessary**), how a decided series was won (majority, or series spread with the margin), each game's round number and score, and where the winner/loser advances. Never the words "dropped" or "bye". |
| Final placements | on the bracket page (and linked from Standings) | Bracket-derived places 1–N, then the main field, with a note that records include playoff games. Places awaiting an undecided series are shown as unresolved, never seed-ordered. Standings carries a banner pointing here, since a playoff division's raw standings compare unequal numbers of games. |
| Pairings page | existing | Playoff rounds get tabs labelled by stage and game ("Semifinal, game 2"); playoff rows carry the series score. `PairingsPresenter` (`tournaments/pairings_view.py:83`) builds tabs from `pd.round_pairings`, so it gains the playoff rounds from the bracket. |
| Nav | `base_division.html` | A "Playoff" tab when a playoff exists; "Playoff" entry in the editor Settings menu otherwise. |

## Phases

Each phase ends green on `make test` (plus `cargo test` where the crate changes).

### Phase 1 — model + derivation

`Playoff`/`PlayoffSeries`/`Pairing` fields + migration; `tournaments/playoff.py`
(bracket templates, windows, series state, scheduling rule, `final_placements`).
No UI, no generation.

*Verification*: `tournaments/tests/test_playoff.py` — pure unit tests over the
derivation for 2/4/8 brackets, every stage length (1, 3, 5), early clinch and
full-length outcomes, the "game *i* is certainly necessary" rule, a drawn game, a
series decided on series spread (and a check that a *tournament-wide* spread
difference does not decide one), an unresolved `tied` series (drawn best-of-1),
a full 8-bracket placing all eight players from its terminal series, the same
bracket with placement series disabled falling back to qualification order, and a
**2023 NACC Division Two fixture** asserting that a 3–3 runner-up places above a
3–2 third.

Determinism gets its own tests: a postscript division where two non-playoff
entrants are dead level on wins *and* spread places them by entrant number and
does so identically across a rebuild; an undecided championship leaves places 1–2
unresolved rather than seed-ordering them; and a forfeit result clinches a series
exactly as a played game does.

### Phase 2 — postscript generation and lifecycle

The three commands; `regenerate_pairings` integration; series upsert; starts
orientation; the pruner and the empty-round transition; the result-correction
guard; `division_state` extension.

*Verification*: a full NACC-shaped tournament driven through the real views in
tests (qualify → semis → championship + third place, one series 2–0 and one 2–1),
asserting: no bye rows, no `dropped` mutation, unnecessary games leave no
pairing/result/export row, and `replay_tournament --verify` reproduces the
digest.

### Phase 3 — UI

Setup form and preview/confirm view, bracket page, placements, pairings-page
labels, nav tabs, `test_event_completeness` entries for the new POST views.

*Verification*: view tests (permissions, preview, confirm, override), plus a
`/verify` run driving a real division through setup → pairing → results →
bracket → placements in the browser.

### Phase 4 — concurrent mode

The `inactive_players` engine field and its three call sites; per-round reserved
names from the bracket; merged rounds; schedule-coverage validation.

*Verification*: `cargo test` for exclusion, bye computation and round status;
Django tests for a mixed round (playoff + ordinary games in one round, correct
tables), for the round *after* a mixed round pairing normally, for exports
containing every played game exactly once, and — the invariant this phase exists
to protect — that in every concurrent playoff round **every non-dropped entrant
has a game**, playoff or ordinary, except where a series clinched early inside
its own window.

### Phase 5 — polish

Fuzzer ops (`tournaments/fuzz.py`) that create a playoff on a small division and
simulate its rounds, so the replay meta-invariant covers playoff state; scorecard
check for playoff rounds; a `CLAUDE.md` paragraph describing the overlay.

## Acceptance criteria → where they are met

| Criterion (issue #44) | Phase |
| --- | --- |
| Create a playoff, confirm or override 2/4/8 qualifiers | 2 (command), 3 (UI) |
| 2 → championship; 4 → 1–4/2–3 + third; 8 → 1–8/4–5/2–7/3–6 + consolation half | 1 |
| Independent positive odd max length per series | 1 |
| Tests cover early clinch and full length for every stage | 1 |
| Only unresolved series receive their next pairing | 1 (rule), 2 (generator) |
| Semifinal winners → championship, losers → third place | 1 |
| Every bracket player has a series in every window (concurrent) | 1 (template), 4 (test) |
| Unnecessary games create no pairing/bye/result/standings/export row | 1–2 |
| No `Entrant.dropped` change, no duplicated entrant | 2 |
| Playoff rounds may close with a subset of pairings, or none | 2 |
| Postscript leaves nonqualifiers inactive, unlabelled, byeless | 2 |
| Concurrent reserves participants and keeps pairing everyone else | 4 |
| Mixed rounds display and export correctly | 4 |
| Bracket views show length, score, status, games, advancement | 3 |
| Bracket-derived placements + 2023 D2 regression fixture | 1 |
| Same-stage eliminations keep qualification order | 1 |
| Exports contain all and only played games | 2, 4 |
| Replay reproduces seeds, timing, bracket, pairings, results, placements | 2 |
| Result edits recompute unplayed downstream state and protect played state | 2 |

## Out of scope

Compressed schedules (so a window that finishes early releases its players);
bracket sizes other than 2/4/8; a confirmed-rollback flow for result corrections that invalidate
played downstream games (blocked instead); exporting bracket placements to the
registry bundle (the bundle carries games; placements are derivable from the
replayed log).
