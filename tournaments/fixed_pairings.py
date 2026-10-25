"""Add/remove fixed pairings with the surrounding lifecycle bookkeeping.

A round with results is locked: you can't add or change a fixed pairing for it.
For a round robin, results don't lock the whole block — the played rounds are
fixed points of the round permutation, so a fixed pairing can still be added to
any *unplayed* round and only those rounds are reverted to draft and regenerated.
"""

from django.db.models import Q

from .generate_pairings import regenerate_pairings
from .models import Entrant, FixedPairing
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


# Round-robin family whose schedule is solved across a block of rounds (see
# pairing/roundrobin.rs). A fixed-pairing change re-solves only the block's
# *unplayed* rounds; the played rounds stay pinned. Charlottesville is a bipartite
# round robin and belongs here too.
_RR_FAMILY = {RP.RoundRobin, RP.DoubleRoundRobin, RP.Charlottesville}


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


# What the inline picker posts for the bye, in place of an entrant pk.
FIXED_BYE_CHOICE = "bye"


def bye_is_offered(division) -> bool:
    """Whether the fixed-pairing pickers offer the bye: the field is odd, so
    somebody sits out each round and the director may choose who."""
    return division.entrants.filter(dropped=False).count() % 2 == 1


def bye_entrant_ids(division) -> set[int]:
    return set(
        Entrant.all_objects.filter(division=division, player__is_bye=True)
        .values_list("pk", flat=True)
    )


def add_fixed_pairing(division, round_number, entrant1_id, entrant2_id) -> tuple[bool, str | None]:
    """Add a fixed pairing and regenerate. Returns (ok, error_message).

    One side may be the bye entrant: that fixes who gets the bye that round
    (the engine then hands out no other, as the rest of the field is even).
    """
    valid_ids = set(division.entrants.values_list("pk", flat=True))
    bye_ids = bye_entrant_ids(division)
    if entrant1_id in bye_ids:
        # Store real-player-first, as every bye row is.
        entrant1_id, entrant2_id = entrant2_id, entrant1_id
    if (
        entrant1_id not in valid_ids
        or entrant2_id not in valid_ids | bye_ids
        or entrant1_id == entrant2_id
    ):
        return False, None  # silent rejection (caller redirects without flash)

    if rounds_with_results(division, [round_number]):
        return False, (
            f"Round {round_number} already has results — "
            "fixed pairings cannot be changed."
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
    # The bye is every absent player's opponent, so it may be fixed more than once.
    already_fixed -= bye_ids
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


def boards_missing_fixtures(division, before, after) -> list[tuple[int, bool]]:
    """Printed rounds a fixed-pairings grid save did not reach.

    ``before``/``after`` are the grid's portable rows (round + player numbers).
    The grid only re-pairs drafts, so a fixture changed for a round already
    published leaves its board as printed. Returns ``(round, has_results)`` for
    each such round whose board lacks one of its fixtures now — a removed
    fixture leaves the board valid, so only a missing one is reported.
    """
    from .models import RoundPairings

    def pins(rows):
        out = {}
        for r in rows:
            out.setdefault(r["round_number"], set()).add(
                frozenset((r["entrant1"], r["entrant2"]))
            )
        return out

    old, new = pins(before), pins(after)
    touched = {r for r in old.keys() | new.keys() if old.get(r) != new.get(r)}
    printed = set(
        division.round_pairings_set.filter(round__in=touched)
        .exclude(status=RoundPairings.DRAFT)
        .values_list("round", flat=True)
    )
    played = rounds_with_results(division, printed)
    stale = []
    for round_number in sorted(printed):
        board = {
            frozenset((p.first.player.player_number, p.second.player.player_number))
            for p in division.pairings.filter(round=round_number).select_related(
                "first__player", "second__player"
            )
        }
        if not new.get(round_number, set()) <= board:
            stale.append((round_number, round_number in played))
    return stale
