"""Database-backed match simulation for test divisions.

Generates random ResultSlip records using rating-weighted win probabilities.
The pure (DB-free) simulator lives in ``simulate.py``.
"""

import random

from .models import ResultSlip


def _random_outcome(r1: int, r2: int) -> tuple[bool, int, int]:
    """Roll a rating-weighted match outcome.

    Returns (first_wins, winner_score, loser_score).
    """
    first_wins = random.random() < r1 / (r1 + r2)
    loser_score = random.randint(200, 450)
    winner_score = random.randint(loser_score, 600)
    return first_wins, winner_score, loser_score


def _build_slip(division, round_num, first_entrant, second_entrant, pairing_obj) -> ResultSlip:
    first_wins, winner_score, loser_score = _random_outcome(
        first_entrant.player.rating, second_entrant.player.rating
    )
    if first_wins:
        winner, loser = first_entrant, second_entrant
        winner_started = True
    else:
        winner, loser = second_entrant, first_entrant
        winner_started = False
    return ResultSlip.objects.create(
        division=division,
        round=round_num,
        pairing=pairing_obj,
        winner=winner,
        winner_score=winner_score,
        loser=loser,
        loser_score=loser_score,
        winner_started=winner_started,
    )


def simulate_match(division, round_num, first_entrant, second_entrant) -> ResultSlip:
    """Simulate one match and persist a ResultSlip.

    Looks up the matching Pairing object (if any) and links the slip to it.
    Caller is responsible for calling ``update_status`` on the round.
    """
    pairing_obj = division.pairings_by_round_pair().get(
        (round_num, frozenset({first_entrant.pk, second_entrant.pk}))
    )
    return _build_slip(division, round_num, first_entrant, second_entrant, pairing_obj)


def simulate_round(division, round_num) -> int:
    """Simulate every unplayed pairing in a round. Returns the count created."""
    played = frozenset(
        frozenset({slip.winner_id, slip.loser_id})
        for slip in division.result_slips.filter(round=round_num)
    )
    pairings = division.pairings.filter(round=round_num).select_related(
        "first__player", "second__player"
    )
    created = 0
    for pairing in pairings:
        if frozenset({pairing.first_id, pairing.second_id}) in played:
            continue
        # Byes are auto-resolved when the round is published, not simulated.
        if pairing.first.player.is_bye or pairing.second.player.is_bye:
            continue
        _build_slip(division, round_num, pairing.first, pairing.second, pairing)
        created += 1

    rp_obj = division.round_pairings_set.filter(round=round_num).first()
    if rp_obj:
        rp_obj.update_status()
    return created
