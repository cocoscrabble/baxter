"""Add/remove fixed pairings with the surrounding lifecycle bookkeeping.

A round with results is locked: you can't add or change a fixed pairing for it.
For a round robin, results don't lock the whole block — the played rounds are
fixed points of the round permutation, so a fixed pairing can still be added to
any *unplayed* round and only those rounds are reverted to draft and regenerated.
"""

from django.db.models import Q

from .generate_pairings import regenerate_pairings
from .models import FixedPairing
from .pairing.base import PairingData, PairingError
from .pairing.round_pairing import RP


def rounds_with_results(division, round_numbers) -> set[int]:
    """Return the subset of round_numbers that already have result slips."""
    if not round_numbers:
        return set()
    return set(
        division.result_slips
        .filter(round__in=round_numbers)
        .values_list("round", flat=True)
        .distinct()
    )


# Round-robin family whose schedule is a permutation of round templates (see
# pairing.basic). A fixed-pairing change re-permutes only the block's *unplayed*
# rounds; the played rounds stay put as fixed points.
_RR_FAMILY = {RP.RoundRobin, RP.DoubleRoundRobin}


def _round_strategy(division, round_number) -> str | None:
    """The pairing strategy configured for ``round_number``, or None if unset.

    A plain ``str`` (comparable to the ``RP`` StrEnum members)."""
    rps = PairingData.for_division(division).round_pairings
    rp = next((r for r in rps if r.round == round_number), None)
    return rp.pairing if rp is not None else None


def round_robin_block_rounds(division, round_number) -> list[int] | None:
    """The rounds of the round-robin block containing ``round_number``, or None if
    that round isn't part of a round-robin block. The block is the run of
    consecutive rounds sharing the strategy and start_round in the normalized
    schedule."""
    rps = PairingData.for_division(division).round_pairings
    this = next((rp for rp in rps if rp.round == round_number), None)
    if this is None or this.pairing not in _RR_FAMILY:
        return None
    return [
        rp.round
        for rp in rps
        if rp.pairing == this.pairing and rp.start_round == this.start_round
    ]


def _rounds_to_regenerate(division, round_number) -> list[int]:
    """Rounds to revert+regenerate for a fixed-pairing change at ``round_number``.

    A plain round touches only itself. A round-robin round touches the block's
    *unplayed* rounds — played rounds are fixed points of the permutation and must
    keep their pairings."""
    block = round_robin_block_rounds(division, round_number)
    if block is None:
        return [round_number]
    played = rounds_with_results(division, block)
    return [r for r in block if r not in played]


def _already_played(division, entrant1_id, entrant2_id) -> bool:
    """Whether these two entrants have already played each other (in a round robin
    they meet exactly once, so a past meeting can't be re-timed)."""
    return division.result_slips.filter(
        Q(winner_id=entrant1_id, loser_id=entrant2_id)
        | Q(winner_id=entrant2_id, loser_id=entrant1_id)
    ).exists()


def add_fixed_pairing(division, round_number, entrant1_id, entrant2_id) -> tuple[bool, str | None]:
    """Add a fixed pairing and regenerate. Returns (ok, error_message)."""
    valid_ids = set(division.entrants.values_list("pk", flat=True))
    if (
        entrant1_id not in valid_ids
        or entrant2_id not in valid_ids
        or entrant1_id == entrant2_id
    ):
        return False, None  # silent rejection (caller redirects without flash)

    if rounds_with_results(division, [round_number]):
        return False, (
            f"Round {round_number} already has results — "
            "fixed pairings cannot be changed."
        )

    # Interim: Charlottesville honors fixed pairings via the exclude-and-pair-the-
    # rest path, which corrupts its rotation schedule. Reject until the solver
    # supports it (PLAN_ROUND_ROBIN Phase 5) rather than silently mispair.
    if _round_strategy(division, round_number) == RP.Charlottesville:
        return False, (
            "Fixed pairings are not yet supported for Charlottesville blocks."
        )

    # In a round robin the two players meet exactly once; if they already have,
    # their meeting can't be moved to another round.
    if round_robin_block_rounds(division, round_number) is not None and _already_played(
        division, entrant1_id, entrant2_id
    ):
        return False, "Those two players have already played each other."

    already_fixed = set()
    for fp in division.fixed_pairings.filter(round_number=round_number):
        already_fixed.update([fp.entrant1_id, fp.entrant2_id])
    if entrant1_id in already_fixed or entrant2_id in already_fixed:
        return False, "One or both players already have a fixed pairing for this round."

    fp = FixedPairing.objects.create(
        division=division,
        round_number=round_number,
        entrant1_id=entrant1_id,
        entrant2_id=entrant2_id,
    )
    rounds = _rounds_to_regenerate(division, round_number)
    division.round_pairings_set.revert_published_to_draft(rounds)
    try:
        regenerate_pairings(division)
    except PairingError as e:
        fp.delete()
        regenerate_pairings(division)  # restore the prior schedule
        return False, str(e)
    except Exception:
        fp.delete()
        regenerate_pairings(division)
        return False, "Could not regenerate pairings with this fixed pairing."
    return True, None


def remove_fixed_pairing(division, fp_id) -> tuple[bool, str | None]:
    """Remove a single fixed pairing and regenerate. Returns (ok, error_message)."""
    fp = division.fixed_pairings.filter(pk=fp_id).first()
    if fp is None:
        return False, None  # silent: already gone or wrong division

    # A plain round with results is locked; a round-robin round's results don't
    # lock removal — only its unplayed rounds re-permute.
    block = round_robin_block_rounds(division, fp.round_number)
    if block is None and rounds_with_results(division, [fp.round_number]):
        return False, (
            f"Round {fp.round_number} already has results — "
            "fixed pairings cannot be changed."
        )

    rounds = _rounds_to_regenerate(division, fp.round_number)
    fp.delete()
    division.round_pairings_set.revert_published_to_draft(rounds)
    regenerate_pairings(division)
    return True, None


def remove_fixed_pairings(division, keep_ids) -> str | None:
    """Remove all fixed pairings not in keep_ids. Returns error message, or None."""
    to_remove = division.fixed_pairings.exclude(pk__in=keep_ids)
    fp_rounds = set(to_remove.values_list("round_number", flat=True))
    locked = set()
    rounds = set()
    for r in fp_rounds:
        if round_robin_block_rounds(division, r) is None and rounds_with_results(
            division, [r]
        ):
            locked.add(r)
        else:
            rounds.update(_rounds_to_regenerate(division, r))
    if locked:
        return (
            "Cannot remove fixed pairings — rounds with results: "
            f"{', '.join(str(r) for r in sorted(locked))}."
        )
    to_remove.delete()
    division.round_pairings_set.revert_published_to_draft(rounds)
    regenerate_pairings(division)
    return None
