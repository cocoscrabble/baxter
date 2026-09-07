"""Withdrawals and forfeits: what happens to the rounds a player is not playing.

The two halves of a withdrawal live apart on purpose. ``Entrant.dropped`` is the
per-entrant fact — this player is out — and the engine reads it to keep them out
of the pairable field. ``DivisionSettings.withdrawal`` is the division's rule for
what their absence *records*: nothing (``OMIT``), or a loss by the division's
``bye_spread`` every round (``FORFEIT``). See plans/PLAN_FORFEITS.md §3.

**Future rounds need nothing from this module.** They are derived at pair and
publish time (``generate_pairings``): a dropped entrant gets a bye-shaped row in
each draft round, and publish turns it into the slip. This module exists for the
rounds that are already *printed* — published or in progress — which
regeneration will never touch again.

For those, the printed game is dissolved and each player takes a bye-shaped row
of their own: the opponent a bye, the absentee a forfeit. That is what TSH does
(``ForfeitLOSS`` repairs both players onto opponent 0), and it is what makes a
withdrawal before pairing and one after it the same thing — both end at
``Pairing(absentee, bye)``, and nothing downstream has to ask which it is
looking at.

Under ``OMIT`` the absentee simply gets no row at all, which is the honest
reading of "leave the rounds blank" and still lets the round finish: the
opponent has their bye, and the round is no longer waiting on a game nobody will
play.
"""

from django.db import models

from tournaments.events import EventResult, records_event
from tournaments.generate_pairings import (
    absence_row,
    materialize_absences,
    withdrawal_policy,
)
from tournaments.models import (
    Division,
    DivisionSettings,
    Pairing,
    RoundPairings,
)

# The rounds this module can act on: printed, not yet closed. A DRAFT round is
# regenerated from scratch and needs no repair; a FINISHED one is history.
OPEN_STATUSES = (RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS)


class ForfeitError(Exception):
    """A forfeit or withdrawal that cannot be applied to the state as it is."""


def _division(tournament, name):
    return Division.objects.get(tournament=tournament, name=name)


def _entrant(division, key):
    return division.entrants.get(player__player_number=key)


def game_in_round(division, round_num, entrant):
    """The entrant's game in a round, or None.

    Refuses to guess when there are two. TSH's ``floss`` makes the same check
    from the other side — it verifies the opponent's own record names the
    forfeiting player, and warns that the file is corrupt rather than proceeding
    (``enfwop``). Baxter keeps both players on one row so they cannot disagree
    that way, but a player appearing in two games in one round is the same class
    of inconsistency, and guessing which to dissolve would make it worse.
    """
    rows = list(
        division.pairings.filter(
            models.Q(first=entrant) | models.Q(second=entrant), round=round_num
        ).select_related("first__player", "second__player")
    )
    if len(rows) > 1:
        raise ForfeitError(
            f"{entrant.name} appears in {len(rows)} games in round {round_num}; "
            "fix the pairings before recording a forfeit."
        )
    return rows[0] if rows else None


def resolve_absence(division, round_pairings, entrant, *, record_absence):
    """Give ``entrant`` and their opponent their rows for one printed round.

    Returns what was done, as ``{"round": n, "opponent": key or None}``, or None
    if there was nothing to do. The caller records that as the payload's account
    of the change; replay recomputes it rather than reading it back.
    """
    round_num = round_pairings.round
    pairing = game_in_round(division, round_num, entrant)
    opponent = None
    if pairing is not None:
        other = pairing.second if pairing.first_id == entrant.pk else pairing.first
        if other.player.is_bye:
            # Already a bye or forfeit row, so there is no game to dissolve and
            # nothing to give an opponent. Whatever is recorded stands: a bye
            # they were handed stays a bye, which is what TSH does too
            # (``SpliceInactive`` only fills a round with no score yet).
            #
            # **Checked before the result guard below, not after.** A bye's
            # result is written at publish, so asking about the result first
            # made this the "already has a result" refusal — and withdrawing
            # somebody who held the round's bye is not a corner case, an odd
            # field byes a different player every round. It refused the whole
            # withdrawal, flag included, because the command is atomic.
            #
            # Turning that bye into a forfeit is a judgement call, so it is left
            # to the director rather than made here: flipping the row in the
            # edit-results grid does it (both players of a dissolved game
            # withdrawing is the case that wants it).
            return None
        if getattr(pairing, "result", None) is not None:
            raise ForfeitError(
                f"{entrant.name}'s round {round_num} game already has a result."
            )
        opponent = other
        pairing.delete()
    elif not record_absence:
        # No game to dissolve and nothing to record: the round is already blank
        # for this entrant, which is what OMIT wants.
        return None

    if opponent is not None:
        absence_row(division, round_pairings, opponent, forfeit=False)
    if record_absence:
        absence_row(division, round_pairings, entrant, forfeit=True)
    # Idempotent, and the one place a bye or forfeit slip is written.
    materialize_absences(division, round_num)
    round_pairings.update_status()
    return {
        "round": round_num,
        "opponent": opponent.key if opponent is not None else None,
    }


def _open_rounds(division):
    return division.round_pairings_set.filter(status__in=OPEN_STATUSES).order_by(
        "round"
    )


def _resolve_open_rounds(division, entrant, *, record_absence):
    """Repair every printed round the entrant is no longer going to play."""
    resolved = []
    for rp in _open_rounds(division):
        change = resolve_absence(
            division, rp, entrant, record_absence=record_absence
        )
        if change is not None:
            resolved.append(change)
    return resolved


def _invalidate_drafts(division):
    """Draft rounds were paired around a different field, so drop them.

    The same thing the entrants grid does on a roster change, and for the same
    reason: the lazy Pair-Rounds render re-pairs. Regenerating here would let a
    PairingError from an unrelated round abort the withdrawal.
    """
    division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()


@records_event("division_absence_settings_saved")
def save_absence_settings(tournament, actor, payload):
    """payload: {division, withdrawal, bye_spread} — how absences are scored.

    Its own command rather than a corner of ``division_settings_saved``, which
    is the *schedule's* writer: changing how a withdrawal is scored should not
    require sending a whole block list, and a block list is not something a
    settings form should be able to get wrong.
    """
    division = _division(tournament, payload["division"])
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.withdrawal = payload["withdrawal"]
    settings_obj.bye_spread = payload["bye_spread"]
    settings_obj.save(update_fields=["withdrawal", "bye_spread"])
    # Draft rounds were laid out under the old rule, so they are stale — a
    # division switching to FORFEIT wants its forfeit rows, and one switching
    # away wants them gone.
    _invalidate_drafts(division)
    return EventResult(payload=payload, division=division, result=settings_obj)


@records_event("entrant_withdrawn")
def withdraw_entrant(tournament, actor, payload):
    """payload: {division, player} — the entrant withdraws from here on.

    ``resolved`` is added to the recorded payload as the account of which
    printed rounds were repaired and against whom. Like
    ``result_starts_corrected``, replay recomputes it from the same state rather
    than reading it back, so the log stays a record of what happened and never
    becomes an instruction that could apply differently a second time.
    """
    division = _division(tournament, payload["division"])
    entrant = _entrant(division, payload["player"])
    record_absence = withdrawal_policy(division) == DivisionSettings.FORFEIT
    entrant.dropped = True
    entrant.save(update_fields=["dropped"])
    resolved = _resolve_open_rounds(
        division, entrant, record_absence=record_absence
    )
    _invalidate_drafts(division)
    return EventResult(
        payload={**payload, "resolved": resolved},
        division=division,
        result=entrant,
    )


@records_event("entrant_rejoined")
def rejoin_entrant(tournament, actor, payload):
    """payload: {division, player} — the entrant is pairable again.

    Nothing is undone. The rounds they missed keep the rows and slips written
    while they were out, which is the whole reason a withdrawal needs no "as of
    round N": the log already orders it against the publishes around it.
    """
    division = _division(tournament, payload["division"])
    entrant = _entrant(division, payload["player"])
    entrant.dropped = False
    entrant.save(update_fields=["dropped"])
    _invalidate_drafts(division)
    return EventResult(payload=payload, division=division, result=entrant)


@records_event("game_forfeited")
def forfeit_game(tournament, actor, payload):
    """payload: {division, round, player} — one player forfeits one printed game.

    Issue #57 scenario 1, and independent of the division's ``withdrawal``
    setting: that rule governs what a *withdrawal* records, while this is a
    director saying outright that this player forfeited this game.
    """
    division = _division(tournament, payload["division"])
    entrant = _entrant(division, payload["player"])
    round_num = payload["round"]
    rp = division.round_pairings_set.filter(round=round_num).first()
    if rp is None or rp.status not in OPEN_STATUSES:
        raise ForfeitError(
            f"Round {round_num} is not published, so it has no game to forfeit."
        )
    change = resolve_absence(division, rp, entrant, record_absence=True)
    if change is None:
        raise ForfeitError(
            f"{entrant.name} has no game to forfeit in round {round_num}."
        )
    return EventResult(
        payload={**payload, "resolved": [change]}, division=division, result=entrant
    )
