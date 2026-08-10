"""Who owns the start when a result contradicts the board it was played on.

A published pairing is a printed board: two names, and one of them going first.
The start ledger balances every later round against that assignment, so the two
records have to agree. When they don't, this module decides which one moves —
and rewrites the other, rather than leaving the disagreement for the engine to
resolve silently on every regeneration.
"""

from tournaments.events import EventResult, records_event
from tournaments.models import Division, RoundPairings

# Which record owns the start when a result slip and its published pairing
# disagree about who went first.
#
# True — the published pairing wins, and the slip is rewritten to match. The
# players were handed a board naming a starter; a result keyed the other way is
# treated as a mis-keyed start, not as a re-decision of who opened.
#
# False would make the entered result authoritative instead: the slip would
# stand and the board would be the thing that was wrong. Flipping this constant
# switches the rewrite off; the engine reads the same rule from its own constant
# (``PUBLISHED_ORIENTATION_WINS`` in ``scrabble-pairing/src/pair.rs``) and the
# two must agree.
PUBLISHED_PAIRING_OWNS_THE_START = True

# A published board is only authoritative once it has actually been handed out.
_PUBLISHED = (
    RoundPairings.PUBLISHED,
    RoundPairings.IN_PROGRESS,
    RoundPairings.FINISHED,
)


def start_conflicts(division):
    """Result slips whose ``winner_started`` contradicts their published pairing.

    Returns ``[(slip, corrected_winner_started), ...]``. A slip qualifies only
    when its round is published — a draft board is not a promise to anyone — and
    when neither side is the bye: a bye pairing is stored real-player-first for
    display, the opposite of the ledger's convention, so comparing it would
    "correct" every bye into charging its player a start.
    """
    if not PUBLISHED_PAIRING_OWNS_THE_START:
        return []
    slips = division.result_slips.filter(
        pairing__isnull=False, pairing__round_pairings__status__in=_PUBLISHED
    ).select_related(
        "pairing__first__player", "pairing__second__player",
        "winner__player", "loser__player",
    )
    conflicts = []
    for slip in slips:
        pairing = slip.pairing
        if pairing.first.player.is_bye or pairing.second.player.is_bye:
            continue
        started = slip.winner_id == pairing.first_id
        if slip.winner_started != started:
            conflicts.append((slip, started))
    return conflicts


@records_event("result_starts_corrected")
def correct_result_starts(tournament, actor, payload):
    """payload: {division}. Rewrites every result slip whose start contradicts
    its published pairing, and logs what it changed.

    The corrections are recomputed here rather than read from the payload, so a
    replay derives them from the same state the live run did; the payload
    carries them only as the record of what the rewrite did. Nothing to correct
    means no event — the log shows real rewrites, not every save that was fine.

    Only the results grid needs this: it takes the start from a column the
    director types. Every other path (``_write_result``, match simulation, the
    historical import) derives the start from the pairing and cannot disagree.
    """
    division = Division.objects.get(tournament=tournament, name=payload["division"])
    conflicts = start_conflicts(division)
    if not conflicts:
        return EventResult(payload=payload, division=division, record=False)
    corrections = []
    for slip, started in conflicts:
        slip.winner_started = started
        slip.save(update_fields=["winner_started"])
        corrections.append(
            {
                "round": slip.round,
                "winner": slip.winner.player.name,
                "loser": slip.loser.player.name,
                "winner_started": started,
            }
        )
    corrections.sort(key=lambda c: (c["round"], c["winner"], c["loser"]))
    return EventResult(
        payload={**payload, "corrections": corrections},
        division=division,
        result=corrections,
    )
