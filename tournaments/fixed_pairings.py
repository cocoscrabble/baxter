"""Add/remove fixed pairings with the surrounding lifecycle bookkeeping.

Rounds with results are locked. Removing a fixed pairing from a published
round drops the round back to draft so regenerate_pairings can rebuild it.
"""

from .generate_pairings import regenerate_pairings
from .models import FixedPairing


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
    division.round_pairings_set.revert_published_to_draft([round_number])
    try:
        regenerate_pairings(division)
    except Exception:
        fp.delete()
        return False, "Could not regenerate pairings with this fixed pairing."
    return True, None


def remove_fixed_pairings(division, keep_ids) -> str | None:
    """Remove all fixed pairings not in keep_ids. Returns error message, or None."""
    to_remove = division.fixed_pairings.exclude(pk__in=keep_ids)
    affected_rounds = set(to_remove.values_list("round_number", flat=True))
    locked = rounds_with_results(division, affected_rounds)
    if locked:
        return (
            "Cannot remove fixed pairings — rounds with results: "
            f"{', '.join(str(r) for r in sorted(locked))}."
        )
    to_remove.delete()
    division.round_pairings_set.revert_published_to_draft(affected_rounds)
    regenerate_pairings(division)
    return None
