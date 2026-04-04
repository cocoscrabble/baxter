"""Pairing generation and round status management.

Generates pairings from the pairing algorithm, resolves fixed table assignments,
assigns table numbers, and persists RoundPairings + Pairing records.
"""

from .models import Pairing, RoundPairings
from .pairing.base import PairingData, standings_after_round
from .pairing.pair import pair


def get_fixed_table(fixed_table_lookup, entrant_id, round_num):
    """Return (table_number, is_all) for an entrant in a round, or None.

    Round-specific assignments take priority over 'all' (-1) assignments.
    """
    specific = fixed_table_lookup.get((entrant_id, round_num))
    if specific is not None:
        return (specific, False)
    all_val = fixed_table_lookup.get((entrant_id, -1))
    if all_val is not None:
        return (all_val, True)
    return None


def resolve_fixed_table(first_ft, second_ft, first_rank, second_rank):
    """Resolve the effective table number when both players have fixed tables.

    Round-specific beats 'all'. If both are the same type, the higher-standing
    (lower rank number) player's table wins.
    """
    if first_ft[1] and not second_ft[1]:
        return second_ft[0]  # second is round-specific
    if second_ft[1] and not first_ft[1]:
        return first_ft[0]   # first is round-specific
    return first_ft[0] if first_rank < second_rank else second_ft[0]


def update_round_status(pairing_obj):
    """Update RoundPairings status after a result is added or removed.

    Called after creating/deleting a ResultSlip linked to a Pairing.
    """
    if not pairing_obj or not pairing_obj.round_pairings:
        return
    rp = pairing_obj.round_pairings
    total = rp.pairings.count()
    with_results = rp.pairings.filter(result__isnull=False).count()
    if with_results == 0 and rp.status == RoundPairings.IN_PROGRESS:
        rp.status = RoundPairings.PUBLISHED
        rp.save(update_fields=["status"])
    elif 0 < with_results < total and rp.status == RoundPairings.PUBLISHED:
        rp.status = RoundPairings.IN_PROGRESS
        rp.save(update_fields=["status"])
    elif with_results == total and rp.status in (RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS):
        rp.status = RoundPairings.FINISHED
        rp.save(update_fields=["status"])


def regenerate_pairings(division):
    """Run the pairing algorithm and save results to the Pairing table.

    Only draft RoundPairings are deleted and recreated. Published, in-progress,
    and finished rounds are preserved.
    """
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
        division.pairings.filter(round_pairings__isnull=True).delete()
        return
    pairings = pair(pd)
    entrant_by_name = {
        e.player.name: e
        for e in division.entrants.select_related("player")
    }
    start_round_by_round = {rp.round: rp.start_round for rp in pd.round_pairings}
    fixed_table_lookup = {
        (ft.entrant_id, ft.round_number): ft.table_number
        for ft in division.fixed_tables.all()
    }
    # Only delete draft rounds (cascades to their Pairing objects).
    # Also clean up any legacy pairings not linked to a RoundPairings.
    division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
    division.pairings.filter(round_pairings__isnull=True).delete()

    for round_num, round_pairings in pairings:
        # Create the RoundPairings container for this round.
        rp_obj, _ = RoundPairings.objects.get_or_create(
            division=division,
            round=round_num,
            defaults={"status": RoundPairings.DRAFT},
        )
        # Skip rounds that already have a non-draft status (shouldn't happen
        # since pair() skips finished rounds, but be defensive).
        if rp_obj.status != RoundPairings.DRAFT:
            continue

        start_round = start_round_by_round.get(round_num, 0)
        standings = standings_after_round(pd, start_round)
        rank = {p.name: i + 1 for i, p in enumerate(standings)}

        # Resolve entrants and effective fixed table for each pairing.
        resolved = []
        for p in round_pairings:
            first_entrant = entrant_by_name.get(p.first.name)
            second_entrant = entrant_by_name.get(p.second.name)
            if not first_entrant or not second_entrant:
                continue
            first_ft = get_fixed_table(fixed_table_lookup, first_entrant.pk, round_num)
            second_ft = get_fixed_table(fixed_table_lookup, second_entrant.pk, round_num)
            if first_ft and second_ft:
                effective = resolve_fixed_table(
                    first_ft, second_ft,
                    rank[p.first.name], rank[p.second.name],
                )
            elif first_ft:
                effective = first_ft[0]
            elif second_ft:
                effective = second_ft[0]
            else:
                effective = None
            resolved.append((p, first_entrant, second_entrant, effective))

        # Assign table numbers: fixed pairings keep their numbers, free pairings
        # are sorted by standings and fill the remaining slots.
        n = len(resolved)
        used = {eff for _, _, _, eff in resolved if eff is not None}
        available = [i for i in range(1, n + 1) if i not in used]
        free = sorted(
            [(p, fe, se) for p, fe, se, eff in resolved if eff is None],
            key=lambda x: min(rank[x[0].first.name], rank[x[0].second.name]),
        )
        free_table = dict(zip((id(p) for p, _, _ in free), available))

        for p, first_entrant, second_entrant, effective in resolved:
            table_num = effective if effective is not None else free_table[id(p)]
            Pairing.objects.create(
                division=division,
                round=round_num,
                round_pairings=rp_obj,
                first=first_entrant,
                second=second_entrant,
                repeats=p.repeats,
                table=table_num,
            )
