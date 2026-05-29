"""Pairing generation and round status management.

Generates pairings from the pairing algorithm, resolves fixed table assignments,
assigns table numbers, and persists RoundPairings + Pairing records.
"""

from .assign_tables import assign_tables, parse_board_table_map
from .models import DivisionSettings, Pairing, RoundPairings
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
    try:
        raw_btm = division.settings.board_table_map
    except DivisionSettings.DoesNotExist:
        raw_btm = []
    board_table_map = parse_board_table_map(raw_btm)
    # Only delete draft rounds (cascades to their Pairing objects).
    # Also clean up any legacy pairings not linked to a RoundPairings.
    division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
    division.pairings.filter(round_pairings__isnull=True).delete()

    # Seeding order, used as a fallback rank for entrants the round's standings
    # don't cover (see below).
    seed_rank = {p.name: i + 1 for i, p in enumerate(standings_after_round(pd, 0))}

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
        # A full round-robin schedule is generated up front, so later rounds
        # have no standings yet (no results played). Fall back to seeding order
        # for any entrant the standings don't cover, keeping board ordering and
        # fixed-table resolution well defined.
        for name, seed in seed_rank.items():
            rank.setdefault(name, len(standings) + seed)

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

        # Order pairings by min standings rank so the top game claims the
        # first board. Fixed-table pairings are placed at their forced table;
        # the rest fill remaining boards in this order.
        resolved.sort(
            key=lambda r: min(rank[r[0].first.name], rank[r[0].second.name])
        )
        ids = list(range(len(resolved)))
        fixed_by_id = {i: r[3] for i, r in enumerate(resolved) if r[3] is not None}
        table_by_id = assign_tables(ids, fixed_by_id, board_table_map)

        for i, (p, first_entrant, second_entrant, _) in enumerate(resolved):
            Pairing.objects.create(
                division=division,
                round=round_num,
                round_pairings=rp_obj,
                first=first_entrant,
                second=second_entrant,
                repeats=p.repeats,
                table=table_by_id[i],
            )
