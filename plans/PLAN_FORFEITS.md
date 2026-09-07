# Plan: Forfeits and dropouts

**Status: phases 1–3 done**, phases 4–5 not started. Design drafted 2026-09-07 for
[issue #57](https://github.com/cocoscrabble/baxter/issues/57); code references
pinned at commit `e3ad086`.

## Goal

Make a withdrawal produce the results it implies. Today `Entrant.dropped` does
only the pairing-side half: the engine drops the entrant from the pairable field
(`scrabble-pairing/src/standings.rs:198`) and keeps their played games, and
nothing else happens. The rounds they no longer play leave no trace, so the
standings show a player who simply stopped accruing games.

Four things follow from issue #57:

1. A withdrawn entrant keeps getting **forfeit result slips** for the rounds
   they are absent for.
2. Withdraw-then-rejoin leaves forfeits on the rounds actually missed, and
   nothing on the rounds either side of them.
3. A director can forfeit **a single game** without withdrawing anybody.
4. Byes and forfeits can be entered in the edit-results grid, which today cannot
   express either.

A forfeit is scored like a bye with the sign flipped: the absentee loses by the
division's `bye_spread` (50 by default, 100 for a WESPA event) instead of
winning by it, and the game is **not rated**. Where a game was already paired,
the pairing is split so that both players end up with a bye-shaped row — the
absentee's a forfeit, the opponent's a bye — which is what makes a withdrawal
before pairing and one after it the same thing (§4).

## Decisions

### 1. A forfeit is a bye with the sign flipped

Reuse the existing bye machinery rather than build a parallel one. A bye slip is
`winner=real, loser=bye_entrant, 50–0`; a forfeit slip is
`winner=bye_entrant, loser=absentee, 50–0`. Everything downstream already copes:
the Rust standings accumulate the slip and then strip the bye from the field
(`standings.rs:229`), `live_ratings._add_games` skips a slip with the bye on
*either* side (`tournaments/live_ratings.py:163`), and so does the results
export (`tournaments/views.py:1488`).

The bye entrant is the notional starter in both directions, so neither player is
charged a start — the rule `PairingData.for_division` already implements
(`tournaments/pairing/base.py:189`).

### 2. Forfeits are derived at publish, exactly like byes

`materialize_byes` (`tournaments/generate_pairings.py:45`) writes a bye's result
when its round is published, under `derived_writes()` — derived state, not a
command, re-derived on replay. Forfeits ride the same path and become
`materialize_absences`: for each pairing in the round with the bye on one side,
award the division's `bye_spread` to the real entrant if they are playing, and
charge it to them if they are withdrawn.

**This is what answers "when did they drop", with no new time field.** The rounds
an entrant forfeits are exactly the rounds that were published while they were
withdrawn. `Entrant.dropped` can stay the bare boolean CLAUDE.md describes,
because the log already orders the withdrawal against the publishes around it.

Scenario 3 (rejoin) then costs nothing. Withdraw before round 5; rounds 5 and 6
are published and record forfeits. Rejoin before round 7; round 7 pairs them
normally, and rounds 5–6 keep the slips written when *they* were published.
Nothing is backfilled, because nothing was ever missing.

### 3. The withdrawal policy is a division setting, not a per-entrant flag

`Entrant.dropped` keeps its current meaning — withdrawn, leaving no trace in
later rounds — and stays a bare boolean. That is what every existing log
recorded and what `division_digest` hashes (`tournaments/events.py:293`); if
publish started generating forfeits for every dropped entrant, replaying an old
log would produce slips its recorded digest does not have, and every
pre-existing tournament with a withdrawal would fail `--verify`.

What decides whether a withdrawal is *recorded* lives on the division:

```
DivisionSettings.bye_spread   int, default 50       # the jurisdictional magnitude
DivisionSettings.withdrawal   omit (default) | forfeit
```

**Why the division and not the entrant.** "Is this player out" is a fact about
an entrant; "how does this tournament record an absence" is a rule, answered
once from the rulebook. An earlier draft put it on the entrant as
`Entrant.forfeits` and produced three valid states across two booleans with one
meaningless combination (`forfeits` without `dropped`) — the usual sign of a
rule wearing a fact's clothes. It also belongs beside `bye_spread`, which is
needed anyway: 50 under NSA, 75 ABSP, **100 WESPA and Thailand**, 300 Poland,
and Baxter enters WESPA-only visitors while hardcoding 50.

`OMIT` is the default because it is what Baxter did before the setting existed,
so replay of an old log is unaffected — the same backward-compatibility the
per-entrant flag had. `DivisionSettings` is outside `division_digest` entirely
(it is rebuilt from `division_settings_saved`), so nothing else changes.

**`withdrawal` is a choice, not a boolean**, because TSH shows the space has four
points rather than two: it also scores an absence as a bye every round
(`off 50`) and as an unscored non-event that is neither win nor loss (`off 0`).
Those are not built; the field takes them without changing shape.

**Per-entrant is deferred, not rejected.** TSH is per-player
(`Deactivate($spread)`) and the reason is real — a round-1 no-show, a player
leaving after round 5, and a lenient exit are different calls in one event. An
override on top of this default can be added when someone asks; starting
per-entrant and adding a default later leaves both to maintain.

### 3a. A committed block keeps the field that started it

A round robin is a rotation over a fixed field; a quad block is a grouping into
fours. Both lay out several rounds at once, so the schedule is decided when the
block begins and belongs to the players who began it. Losing one of them partway
through must not re-cut the template — everyone else's remaining fixtures are
already promised.

**Handled in Baxter, not the engine.** The engine is handed the field that
*started* the block and pairs it exactly as it always would; `regenerate_pairings`
dissolves the withdrawn player's fixtures when the pairings come back, into the
same bye-and-forfeit pair every other absence produces. Two edits to the engine
input, both Baxter's rules rather than the pairer's
(`committed_field_rounds` / `commit_withdrawn_to_field`):

- the withdrawal is lifted for those rounds, so the template is cut whole;
- the entrant is listed in `inactive_players` for every round *outside* those
  blocks — the engine's existing "no game, no bye, not withdrawn", which is what
  a withdrawal already means to an ordinary round, and what playoffs use to pair
  around reserved players.

A block that has **not** started is not committed to anybody and is simply
solved for whoever is left, which is the better tournament.

The strategies this covers are exactly the ones calling
`guard_no_dropped_in_block` in the engine — round robin, double round robin,
Charlottesville, the three quad variants and sixes. That guard is the list to
check `_FIXED_FIELD_STRATEGIES` against, and it is now effectively unreachable
from Baxter: a block with results in it is by definition committed, so the
engine is never asked to re-pair around a withdrawal. It stays as the engine's
own safety net.

Two consequences worth knowing:

- **An odd field can carry two byes in a round** — the rotation's own, plus the
  one the absence creates. That is correct; what must not happen is a player
  appearing twice or not at all.
- **When the rotation hands the bye to the withdrawn player**, it is theirs to
  forfeit. Nobody else gains one, or the round would be a game short.

### 4. The corner case — the pairing is split, and the split is recorded

The case the issue raises: X is paired against Y in round 5, round 5 is
published, and then X withdraws. The round cannot be re-paired.

**The pairing is split, as TSH does it.** The X–Y pairing is dissolved and each
player is paired with the bye: X takes the forfeit (0–50), Y takes the bye
(50–0). `doc/trouble.html` is unambiguous that this is the operation — "you
should manually repair him and his opponent to assign them both byes" — and
`floss` performs exactly that (`$dp->Pair($opp->ID(), 0, $r0, 'repair')` for the
opponent, then the same for the forfeiter).

**Recorded as an explicit unpair, then the two results.** That is what keeps a
split replayable: the log carries the dissolution as its own step rather than
leaving it implicit in a forfeit command, so a replay reproduces the same
surgery, and the primitive is available on its own — TSH has `UnPairPlayer` and
`UnPairRound` for the same reason.

**This is what makes the two kinds of forfeit one kind.** A player dropped
*before* the round was paired never gets a real pairing: §2 gives them
`Pairing(X, bye)` at pair time. A player dropped *after* it was paired gets the
same `Pairing(X, bye)` via the unpair. Both then take the identical slip through
the identical publish path. There is no second shape to carry, no second code
path, and nothing downstream has to ask which kind of forfeit it is looking at.

**It also fixes bye distribution, which the alternative could not.** `CountByes`
in TSH counts every opponent-0 round regardless of sign, and that count is what
bye assignment minimises. Splitting gives Y a real bye pairing, so
`disallow_repeat_byes` sees it without any special pleading. Recording the
forfeit on the intact X–Y pairing (the design this replaces) left Y looking
bye-less, and they could then be handed a real bye while others had none — the
second of the two failures `doc/trouble.html` warns about.

**What it costs, and where to be careful.**

- **The printed board loses a row.** This is a real cost — it is why the
  previous design avoided it — and it is accepted: TSH's directors have run on
  these semantics for two decades, and the board is a record of what was
  *intended*, which a withdrawal has changed. The pairings page should say so
  rather than silently dropping the game; TSH's scoreboard renders an `F`.
- **Round robins are the sharp edge.** `published_pairings` pins an in-progress
  round-robin round so the solver neither recomputes nor duplicates it
  (`tournaments/pairing/base.py:180`). Removing X–Y from it means the solver no
  longer knows that fixture was scheduled, and a round robin's whole invariant is
  that every pair meets exactly once. A forfeited round-robin fixture must still
  count as *met* for schedule completion even though it counts as *not met* for
  Swiss repeat avoidance. Phase 3 has to separate those two uses of the same
  ledger; today they are one.
- **Starts.** X–Y's orientation disappears with the pairing, and the two bye
  rows charge nobody a start under Baxter's current `bye_firsts = 'ignore'`
  behaviour. See §6's note on the NSA `'alternate'` rule, which is a separate
  policy call.

### 5. `ResultSlip.forfeit` — kept, but on a narrower footing

**Its original justification is gone.** The flag was introduced (phase 1, already
landed) for a slip sitting on an ordinary pairing between two real entrants,
where nothing else could distinguish "X forfeited to Y" from "Y beat X 50–0".
Splitting means no such slip exists: every forfeit now carries the bye entrant,
as the *winner*, and is identifiable by that alone — TSH's own scheme, where the
sign of the spread is the whole distinction.

It stays for two narrower reasons, and the `forfeit=True` clause in `played()`
is dead weight until the second one lands:

- **Explicitness.** Encoding "not played" implicitly in *which side* the bye sat
  on is precisely what produced the asymmetry §6 fixes — six call sites that all
  assumed the bye could only win. A column that says what the row is does not
  have that failure mode.
- **The unscored game is coming.** TSH's `off 0` records "a missed game without
  assigning a win or loss", and a 0–0 slip has no winner to put the bye on, so
  direction cannot encode it. Whatever carries that case is this column or a
  successor to it.

If neither argument survives contact with phase 3, drop the column — it is one
migration, and a dead flag is worse than none.

### 6. Fix the `loser__player__is_bye` asymmetry while we are here

Most bye filters in the codebase are written as `loser__player__is_bye=True` —
they assume the bye can only ever *win*. A forfeit slip inverts that, so each of
these mis-handles one today, before any of this plan lands:

| Site | What a bye-as-winner slip does |
|---|---|
| `models.py:914` (`RoundPairings.update_status`) | counts as a real result, so a freshly published round reads IN_PROGRESS |
| `generate_pairings.py:132` (`unpublish_rounds`) | blocks unpublishing an untouched round |
| `generate_pairings.py:140,328` | forfeit slips are not cleaned up when a round is re-paired |
| `pairings_view.py:154` (`rounds_with_real_results`) | same, for the editor's tab state |
| `tournament_export.py:103` | the forfeit leaks into the registry push as a real game |

All five become `.played()` (or its negation). This is a prerequisite for
everything else, not a tidy-up.

## What TSH does, and what we neglected

Read out of `~/github/tsh` (John Chew's Tournament Scrabble Helper, the
reference implementation most CoCo directors have used). Its forfeit handling is
`TSH::Command::ForfeitLOSS` (`floss`), `TSH::Player::Deactivate` /
`SpliceInactive`, and the "Players Who Miss Games" section of `doc/trouble.html`.

**What it confirms.** TSH represents a forfeit in the same slot as a bye —
opponent 0, distinguished by the *sign* of the spread. A bye is `+spread`, a
forfeit loss is `-spread`, and neither is ever rated (`$ratedwins += $result if
$oppid`). Withdrawal pre-fills the absent player's rounds at pairing time and
only where nothing is recorded yet — `SpliceInactive` is documented as "assigns
them byes for the next N rounds beginning with round R **unless they already
have pairings/scores for those rounds**", which is decision 2 exactly, and
`Activate()` pops the auto-filled rows back to the last real paired round, which
is the rejoin case. So the shape of this plan matches the reference.

**Six things it does that this plan does not.**

1. ~~**The spread is configurable, and jurisdictional.**~~ **Closed** —
   `DivisionSettings.bye_spread` (§3). 50 under NSA, 75 ABSP, 100 WESPA and
   Thailand, 300 Poland. Still outstanding: `floss` takes a *per-call* spread,
   so a one-off forfeit can be scored differently from the tournament default.

2. **There is a third outcome: the unscored game.** `off 0` "will record a missed
   game without assigning a win or loss"; `bye_spread = 0` makes byes "count
   neither as wins nor losses". In the standings loop a zero-spread bye
   increments neither `wins` nor `losses` — the round simply does not exist for
   that player. This plan has only bye (a win) and forfeit (a loss). `off 50`
   is a third withdrawal mode again: withdrawn but credited a bye every round.

3. **A bye is not necessarily a win.** `all_byes_tie` (Poland) scores every bye
   as half a win, and `zero_byes_tie` makes a zero-spread bye a tie rather than a
   nonevent.

4. **Withdrawal carries a spread, not a boolean.** `Deactivate($spread)` stores
   an integer, and the modes above are just its sign. Partly closed: the
   magnitude is now `bye_spread` and the mode is `withdrawal`, a choice field
   with room for the other two points (§3). Still outstanding: the modes
   themselves, and that TSH sets them per player rather than per division.

5. **A forfeit counts toward bye distribution — and decision 4 breaks that.**
   `CountByes` counts every round with opponent 0 *regardless of sign*, and that
   count is what bye assignment minimises. So in TSH a forfeit win **and** a
   forfeit loss both count as byes already had. `doc/trouble.html` gives this as
   one of the two reasons to repair a missed game into two byes: "the unplayed
   game will count for ratings, and **some players may end up getting multiple
   byes before others get any**."

   This is what settled decision 4. An earlier draft of this plan kept the real
   pairing and flagged its result, which answered the ratings half without
   unprinting a board — but left Y looking bye-less, so they could be handed a
   real bye while others had none. Splitting gives Y an actual bye pairing and
   the count comes out right with no special pleading, which is the argument
   that carried.

6. **`bye_firsts`, and the NSA rule we are on the wrong side of.** The default is
   `'alternate'`: "use the rule the NSA adopted on 2008-07-24 to assign
   alternating firsts and seconds to players who have forfeit losses". `'ignore'`
   (ABSP, Germany, Pakistan, Singapore) ignores byes and forfeits when comparing
   firsts. **Baxter is `'ignore'`** — the bye is the notional starter and the
   absent player is charged nothing — and the derived Started column hardened
   that. CoCo is an NSA-rules body, so the default is arguably wrong for its home
   jurisdiction. This is a policy call, not a bug.

**One defensive check worth copying.** `floss` verifies that the opponent's own
record names the forfeiting player before assigning the forfeit win, and warns
(`enfwop`, "your .t file is corrupt") rather than proceeding. Phase 3's repair of
an already-published round should make the same check.

**Not adopted.** `show_inactive` — TSH hides withdrawn players from reports by
default and needs a config to show them; Baxter shows them always
(`include_dropped=True`), which is the better default and already decided.

## Phases

### Phase 1 — the predicate — **done**

`ResultSlip.forfeit` and `Entrant.forfeits` (migration `0045_forfeit_flags`,
both default `False`; `Entrant.forfeits` was later moved to
`DivisionSettings.withdrawal` — see §3 — and removed in `0047`), `ResultSlipQuerySet.played()` / `.not_played()` with
`ResultSlip.is_played` beside them, and the six call sites in §6 moved onto
them. Nothing sets either flag yet.

Two notes for the phases that follow:

- The two sites that already tested both sides (the results CSV and the live
  rating calculator) were moved onto `is_played` as well, so the definition
  lives once.
- The digest backfill migration had to be renumbered to stay last, as its own
  comment requires. **Any further schema migration in this plan must do the
  same** — that is now `0046_backfill_event_digests`, and it is a no-op on a
  database that already ran it.

*Verified:* suite green (1071); the new tests in `NotPlayedPredicateTests` fail
against the old filters at four of the six sites.

### Phase 2 — derive forfeits at pair and publish — **done**

`regenerate_pairings` synthesizes a forfeit pairing (`entrant`, `bye_entrant`)
for each dropped entrant in each draft round, when the division's `withdrawal`
setting says `forfeit`, alongside the bye pairings it
already sets aside (`generate_pairings.py:479`). Parity is unaffected: the
engine never saw the withdrawn entrant, so the odd-field bye is computed on the
remaining field and the forfeit rows are added on top.

`materialize_byes` becomes `materialize_absences` and picks the direction from
**`Pairing.forfeit`** — a new column, deviating from this plan's first draft,
which read the entrant's `dropped` state at publish time. That state moves: an
entrant who forfeits round 5 and rejoins for round 7 would make the round-5 row
read as an ordinary bye afterwards. The row records what was decided when the
round was paired, so it carries the decision. Encoding it in *which side* the
bye sits on was the other option and was rejected for the reason in §5 —
implicit orientation is what produced the asymmetry §6 had to clean up.

**A pre-existing engine bug had to be fixed to get here.** `round_status` in
`scrabble-pairing/src/pair.rs` counted withdrawn players among those a round is
waiting on, so every round after a withdrawal read `Partial` forever and
`can_pair` refused the next one: **dropping anybody stopped the tournament
dead**, with or without forfeits. Withdrawn players are now excluded exactly as
`inactive_players` already were. Only lowering `expected` is safe because the
test is `real >= expected`, so a round the player did appear in still finishes.
Forfeits masked the bug — a forfeiting entrant keeps appearing in every round's
slips — which is why it surfaced only from the *without*-forfeits test.

*Verified* (`tournaments/tests/test_forfeits.py`): rounds published while an
entrant is withdrawn each gain one 0–50 slip; the standings show the losses and
the −50s; the ratings projection counts only games actually played and both
exports omit the forfeits; a rejoin keeps the earlier forfeits and pairs the
later rounds normally; withdrawing *without* forfeits still records nothing; a
bye and a forfeit coexist in one round without disturbing parity; and
regenerating repeatedly neither duplicates the row nor moves the digest.

**Replay is not fully exercisable until phase 3.** Re-deriving is
digest-stable (tested above) and replay regenerates before every publish
(`replay.NEEDS_REGEN`), so the derivation itself replays. But nothing writes
`DivisionSettings.withdrawal` through a command yet, so there is no event for a
replay to read it from — a division set by hand today replays as `omit`.
Nothing existing is affected, since the default *is* `omit`. Phase 3 adds the
command and owns the end-to-end `replay --verify`.

### Phase 3 — splitting a paired game, and single-game forfeits — **done**

`tournaments/forfeits.py`. Three commands, over one shared plain function:

- `entrant_withdrawn` — sets `dropped`, repairs every PUBLISHED/IN_PROGRESS
  round the entrant is no longer going to play, and drops the draft rounds so
  they re-pair around the smaller field.
- `entrant_rejoined` — clears `dropped` and drops the drafts. Undoes nothing:
  the rounds missed keep the rows written while the entrant was out.
- `game_forfeited` — issue #57 scenario 1. Independent of the division's
  `withdrawal` setting, which governs what a *withdrawal* records; this is a
  director saying outright that this player forfeited this game.

**The unpair is recorded in the payload, not as its own event.** Nesting two
`@records_event` commands would double-apply on replay: replay re-invokes the
outer command, which would perform the inner one again *and* re-record it, on
top of the inner event the log already carries. So `entrant_withdrawn` adds a
`resolved` list — the rounds it repaired and against whom — exactly as
`result_starts_corrected` records the corrections it made. Replay recomputes it
from the same state rather than reading it back, so the log stays a record of
what happened rather than an instruction that could apply twice.

**`OMIT` still resolves the opponent's game.** A withdrawal from a printed round
dissolves the pairing under either policy — otherwise the round waits forever on
a game nobody will play — and the opponent takes their bye. The policy decides
only whether the *absentee* gets a row: under `FORFEIT` a forfeit, under `OMIT`
nothing at all, which is the honest reading of "leave the rounds blank".

`floss`'s defensive check has an analogue: Baxter keeps both players on one
`Pairing`, so they cannot disagree the way TSH's two-sided record can, but
`game_in_round` refuses to guess when a player appears in two games in one
round, and a game that already has a result is refused outright.

`save_settings` grew optional `withdrawal` and `bye_spread` keys, so the policy
reaches the log and a replay rebuilds it. Absent keys leave the fields alone,
so every payload written before they existed replays unchanged.

**The round-robin hazard §4 feared never materialised**, and the real problem
turned out to be a different one. `published_pairings`' two uses never collide,
because a round robin that loses a player mid-block did not re-solve wrongly —
it refused to solve at all, leaving the division unpairable until the director
rewrote the schedule. Two fixes, neither of them a separation of those uses:
`guard_no_dropped_in_block` no longer counts bye-shaped slips as games played in
the block, and §3a keeps the field whole so the guard is not reached in the
first place.

*Verified* (`tournaments/tests/test_forfeit_commands.py`): the printed game is
split into a bye and a forfeit with the right scores and starts; the opponent
now reads as having had a bye in `published_pairings`, which is what bye
avoidance consumes; **a withdrawal before pairing and one after produce an
identical row and slip**; the round still reaches FINISHED; a played game and a
double-booked player are refused; rejoining leaves the printed rounds untouched;
`OMIT` gives the opponent their bye and the absentee nothing; and a log carrying
a withdrawal replays under `verify=True` to the same digest, settings included.

### Phase 4 — the surfaces

A Withdraw/Rejoin control on the entrants page (`_entrants_table.html` is shared
with the public embed, so editor-only, like the drift column), and a Forfeit
button per game on the pairings page. Forfeit rows render as "forfeit", not
"bye", on the pairings and results pages.

*Verify:* drive it with `/verify`.

### Phase 5 — entering a bye or forfeit by hand in the edit-results grid

**Editing an existing one already works** (done ahead of this plan, commits
`Let the results grid see the bye entrant` and `Derive the Started column on a
bye or forfeit row`): the grid sees the bye entrant, so a bye row loads with its
Opponent cell filled, can be rescored or deleted, round-trips into the logged
payload, and replays. The Started column is derived rather than typed on such a
row, because `winner_started` orients the pairing the engine replays into its
start ledger and `correct_result_starts` skips bye pairings, so a wrong value
there would never be put back. The forfeit shape (bye as *winner*) is covered by
tests already, so phase 3 lands on a grid that carries it.

What remains is entering a *new* bye or forfeit row, which needs two things:

- **Score defaulting.** `ResultSlipDTO.from_json` rejects any row with a blank
  field (`dto.py:30`) — the "all fields are required" error the issue names.
  Default the scores when the opponent is the bye: 50/0 for a bye, 0/50 for a
  forfeit.
- **A pairing to hang it on.** `ResultsGrid.prepare` requires every row to
  resolve to an existing `Pairing` (`grids.py:543`). A hand-entered forfeit for
  a round that never paired the absentee needs its pairing synthesized, the same
  way phase 3 does.

*Verify:* enter a bye and a forfeit by hand in the grid, save, and confirm the
round reaches FINISHED and the digest survives a replay.
