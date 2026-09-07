# Plan: Forfeits and dropouts

**Status: not started.** Design drafted 2026-09-07 for
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

A forfeit is scored like a bye with the sign flipped: the absentee takes 0–50
and −50 spread, and the game is **not rated**.

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
write 50–0 if the real entrant is playing and 0–50 if they are withdrawn.

**This is what answers "when did they drop", with no new time field.** The rounds
an entrant forfeits are exactly the rounds that were published while they were
withdrawn. `Entrant.dropped` can stay the bare boolean CLAUDE.md describes,
because the log already orders the withdrawal against the publishes around it.

Scenario 3 (rejoin) then costs nothing. Withdraw before round 5; rounds 5 and 6
are published and record forfeits. Rejoin before round 7; round 7 pairs them
normally, and rounds 5–6 keep the slips written when *they* were published.
Nothing is backfilled, because nothing was ever missing.

### 3. A separate `Entrant.forfeits` flag, so old logs replay unchanged

`dropped` keeps its current meaning: withdrawn, leaving no trace in later rounds.
That is what every existing log recorded, and `division_digest` hashes it
(`tournaments/events.py:293`). If publish started generating forfeits for every
dropped entrant, replaying an old log would produce slips its recorded digest
does not have, and every pre-existing tournament with a withdrawal would fail
`--verify`.

So withdrawing *with* forfeits is a new, separately recorded decision:
`Entrant.forfeits` (default `False`), set by a new `entrant_withdrawn` command.
This is the `entrants_reseeded` precedent — its own event precisely so that
every payload written before the feature existed replays exactly as it always
did.

It is also a real distinction directors want. Removing a player from a small
club event should leave no forfeit losses; withdrawing one from a rated event
usually should. The two paths stay separate: the entrants grid's `Dropped`
column keeps meaning the first, the Withdraw button means the second.

`forfeits` stays **out of the digest**, for the same backward-compatibility
reason. It needs no place there: its whole effect is the forfeit slips, and
`results` is already hashed.

### 4. The corner case — a printed board is never rewritten

The case the issue raises: X is paired against Y in round 5, round 5 is
published, and then X withdraws. The round cannot be re-paired.

**The forfeit is recorded as the result of the game that was printed.** The
X–Y pairing keeps its identity and its board; its result becomes
`winner=Y, loser=X, 50–0, forfeit=True`. Y turned up and gets the win and the
+50; X gets the loss and the −50; the `forfeit` flag keeps the game out of the
ratings. The withdrawal command repairs every open published round this way,
mirroring `_add_missing_playoff_games`, which is the existing precedent for a
published window being corrected in place (`plans/PLAN_PLAYOFFS.md`).

**Why not the two-slip split the issue proposes.** Recording Y's half as a bye
means deleting the X–Y `Pairing` and creating two bye-shaped ones in a published
round. That contradicts `PUBLISHED_PAIRING_OWNS_THE_START`
(`tournaments/starts.py`) — a published pairing is a printed board, and this
would unprint one. It also drops the game out of `published_pairings`, which is
what pins an in-progress round-robin round so the solver does not recompute or
duplicate it (`tournaments/pairing/base.py:180`).

The standings outcome is identical either way: +50 and a win for Y, −50 and a
loss for X, neither rated. The only difference is what Y's row says, and "won by
forfeit" is the more accurate of the two — they showed up.

One consequence, accepted deliberately: X and Y count as having met, so the
engine will not pair them again if X rejoins. Removing the forfeited game from
the repeat ledger would mean removing it from `published_pairings`, which is the
same round-robin pinning the paragraph above depends on. A rejoining player
missing one possible opponent is the smaller cost.

### 5. `ResultSlip.forfeit` — one flag for "recorded, not played"

Needed only for the corner case, and needed there absolutely: that slip sits on
an ordinary pairing between two real entrants, and nothing else distinguishes
"X forfeited to Y" from "Y beat X 50–0".

Derived forfeits (§2) set it too, so one predicate covers both:

```python
def played(self):  # ResultSlipQuerySet
    """Games actually contested — no byes, no forfeits."""
    return self.exclude(
        Q(winner__player__is_bye=True)
        | Q(loser__player__is_bye=True)
        | Q(forfeit=True)
    )
```

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

## Phases

### Phase 1 — the predicate

`ResultSlip.forfeit` and `Entrant.forfeits` (one migration, both default
`False`), a `ResultSlipQuerySet.played()`, and the six call sites in §6 moved
onto it. No behaviour change yet: nothing sets either flag.

*Verify:* existing suite green; a hand-built bye-as-winner slip no longer flips
`update_status` to IN_PROGRESS or blocks `unpublish_rounds`.

### Phase 2 — derive forfeits at pair and publish

`regenerate_pairings` synthesizes a forfeit pairing (`entrant`, `bye_entrant`)
for each `forfeits` entrant in each draft round, alongside the bye pairings it
already sets aside (`generate_pairings.py:479`). Parity is unaffected: the
engine never saw the withdrawn entrant, so the odd-field bye is computed on the
remaining field and the forfeit rows are added on top.

`materialize_byes` becomes `materialize_absences` and picks the direction from
the real entrant's `dropped`/`forfeits` state at publish time.

*Verify:* withdraw-with-forfeits before round 5 of an 8-round division; rounds
5–8 each gain one 0–50 slip on publish; standings show the losses and the −50s;
the ratings export and `tournament_export` omit them; `replay --verify`
reproduces the digest. Then rejoin before round 7 and confirm 5–6 keep their
forfeits and 7–8 pair normally.

### Phase 3 — the corner case, and single-game forfeits

Two commands sharing one implementation:

- `entrant_withdrawn` — sets `dropped`/`forfeits`, then repairs every
  PUBLISHED/IN_PROGRESS round in which the entrant has no result yet: a real
  pairing gets the forfeit result written onto it (§4); no pairing at all gets a
  forfeit pairing plus slip, the phase-2 shape.
- `game_forfeited` — issue #57 scenario 1. The same write for one named pairing,
  with no withdrawal.

Both must be added to the completeness test's view coverage, per CLAUDE.md.

*Verify:* publish round 5, withdraw a paired entrant, confirm the board still
shows X–Y and its result reads as a forfeit; confirm the opponent's standings
row gains a win and +50 and the ratings export omits the game; `replay --verify`
green; a fuzzer run with withdrawals enabled reproduces digests.

### Phase 4 — the surfaces

A Withdraw/Rejoin control on the entrants page (`_entrants_table.html` is shared
with the public embed, so editor-only, like the drift column), and a Forfeit
button per game on the pairings page. Forfeit rows render as "forfeit", not
"bye", on the pairings and results pages.

*Verify:* drive it with `/verify`.

### Phase 5 — byes and forfeits in the edit-results grid

Two blockers today. `ResultsGrid.lookups` builds its dropdowns from
`division.entrants` (`grids.py:531`), which is the manager that *hides* the bye,
so "Bye" cannot be selected at all; and `ResultSlipDTO.from_json` rejects any row
with a blank field (`dto.py:30`), which is the "all fields are required" error
the issue names. Add the bye entrant to the lookup and default the scores when
the opponent is the bye (50/0, or 0/50 for a forfeit).

`ResultsGrid.prepare` also requires every row to resolve to an existing
`Pairing` (`grids.py:543`), so a forfeit row entered by hand needs its pairing
synthesized the same way phase 3 does.

*Verify:* enter a bye and a forfeit by hand in the grid, save, and confirm the
round reaches FINISHED and the digest survives a replay.
