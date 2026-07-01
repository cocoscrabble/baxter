"""Pairing generation and round status management.

Generates pairings from the pairing algorithm, resolves fixed table assignments,
assigns table numbers, and persists RoundPairings + Pairing records.
"""

from django.db import transaction
from django.db.models import Q

from .assign_tables import assign_tables, parse_board_table_map
from .models import BYE_PLAYER_NAME, DivisionSettings, Pairing, ResultSlip, RoundPairings
from .pairing.base import PairingData, standings_after_round
from .pairing.pair import pair

# A bye is scored as a win with a fixed +50 spread (50–0), no game played.
BYE_WINNER_SCORE = 50
BYE_LOSER_SCORE = 0


def _is_bye_name(name):
    return name.lower() == BYE_PLAYER_NAME.lower()


def materialize_byes(division, round_num):
    """Record the automatic win for each byed player in a round (idempotent).

    Called when a round is published, so the round can reach 'finished' without
    the director entering the bye by hand. The byed (real) player wins at a fixed
    spread; the bye entrant is the notional starter, so the real player is not
    charged a start.
    """
    bye_pairings = (
        division.pairings.filter(round=round_num, result__isnull=True)
        .filter(Q(first__player__is_bye=True) | Q(second__player__is_bye=True))
        .select_related("first__player", "second__player")
    )
    for p in bye_pairings:
        if p.first.player.is_bye:
            bye_entrant, real_entrant = p.first, p.second
        else:
            bye_entrant, real_entrant = p.second, p.first
        ResultSlip.objects.create(
            division=division,
            round=round_num,
            pairing=p,
            winner=real_entrant,
            winner_score=BYE_WINNER_SCORE,
            loser=bye_entrant,
            loser_score=BYE_LOSER_SCORE,
            winner_started=False,
        )


def publish_rounds(division, round_numbers=None):
    """Publish draft rounds, auto-record their byes, and refresh round status.

    ``round_numbers=None`` publishes every draft round. Returns the rounds
    actually published. Centralises publishing so a bye is always recorded the
    moment its round goes live.
    """
    qs = division.round_pairings_set.filter(status=RoundPairings.DRAFT)
    if round_numbers is not None:
        qs = qs.filter(round__in=round_numbers)
    published = list(qs.values_list("round", flat=True))
    qs.update(status=RoundPairings.PUBLISHED)
    for round_num in published:
        materialize_byes(division, round_num)
        rp = division.round_pairings_set.filter(round=round_num).first()
        if rp:
            rp.update_status()
    return published


def unpublish_rounds(division, round_numbers=None):
    """Revert published rounds with no real results back to draft.

    A round is eligible when it is PUBLISHED or IN_PROGRESS and carries no
    director-entered results — an auto-materialized bye (the only result a
    freshly published round can have) does not count. The bye slips are deleted
    so the round becomes a clean draft that ``regenerate_pairings`` can re-pair,
    and its status is restored to DRAFT so the editor treats it as pairable
    again. ``round_numbers=None`` considers every published round.

    Returns the rounds actually unpublished (empty if none were eligible).
    """
    qs = division.round_pairings_set.filter(
        status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS]
    )
    if round_numbers is not None:
        qs = qs.filter(round__in=round_numbers)
    candidates = list(qs.values_list("round", flat=True))
    if not candidates:
        return []
    rounds_with_real_results = set(
        division.result_slips.filter(round__in=candidates)
        .exclude(loser__player__is_bye=True)
        .values_list("round", flat=True)
    )
    to_unpublish = [r for r in candidates if r not in rounds_with_real_results]
    if not to_unpublish:
        return []
    with transaction.atomic():
        ResultSlip.objects.filter(
            division=division, round__in=to_unpublish, loser__player__is_bye=True
        ).delete()
        division.round_pairings_set.filter(round__in=to_unpublish).update(
            status=RoundPairings.DRAFT
        )
    return to_unpublish


def get_fixed_table(fixed_table_lookup, entrant_id, round_num):
    """Return (table_label, is_all) for an entrant in a round, or None.

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


@transaction.atomic
def regenerate_pairings(division):
    """Run the pairing algorithm and save results to the Pairing table.

    Only draft RoundPairings are deleted and recreated. Published, in-progress,
    and finished rounds are preserved. Atomic, so a PairingError (unsatisfiable
    fixed pairings) raised by ``pair()`` rolls back cleanly, leaving the existing
    schedule untouched for the caller to surface the error.
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
    # Lazily resolve the bye opponent (created on first odd round) and map its
    # engine name to the division's bye entrant.
    bye_entrant = None

    def resolve_entrant(name):
        nonlocal bye_entrant
        if _is_bye_name(name):
            if bye_entrant is None:
                bye_entrant = division.bye_entrant()
            return bye_entrant
        return entrant_by_name.get(name)
    start_round_by_round = {rp.round: rp.start_round for rp in pd.round_pairings}
    fixed_table_lookup = {
        (ft.entrant_id, ft.round_number): ft.table_label
        for ft in division.fixed_tables.all()
    }
    try:
        raw_btm = division.settings.board_table_map
    except DivisionSettings.DoesNotExist:
        raw_btm = []
    board_table_map = parse_board_table_map(raw_btm)
    # Only delete draft rounds (cascades to their Pairing objects).
    # Also clean up any legacy pairings not linked to a RoundPairings.
    draft_rounds = list(
        division.round_pairings_set.filter(status=RoundPairings.DRAFT)
        .values_list("round", flat=True)
    )
    # Auto-created bye results live in draft rounds until published; drop them so
    # the round can be re-paired cleanly (a draft round with a lingering bye
    # result would read as Partial and block regeneration).
    ResultSlip.objects.filter(
        division=division, round__in=draft_rounds, loser__player__is_bye=True
    ).delete()
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

        # Resolve entrants and effective fixed table for each pairing. Bye
        # pairings are set aside: they get no table and don't participate in the
        # board-ordering sort.
        resolved = []
        bye_pairings = []
        for p in round_pairings:
            first_entrant = resolve_entrant(p.first.name)
            second_entrant = resolve_entrant(p.second.name)
            if not first_entrant or not second_entrant:
                continue
            if _is_bye_name(p.first.name) or _is_bye_name(p.second.name):
                bye_pairings.append((p, first_entrant, second_entrant))
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
            table_order, table_label = table_by_id[i]
            Pairing.objects.create(
                division=division,
                round=round_num,
                round_pairings=rp_obj,
                first=first_entrant,
                second=second_entrant,
                repeats=p.repeats,
                table=table_order,
                table_label=table_label,
            )

        # Bye pairings carry no table; the bye result is recorded when the round
        # is published (see materialize_byes). Show the real player first for
        # readability — orientation is display-only, the result encodes the win.
        for p, first_entrant, second_entrant in bye_pairings:
            if _is_bye_name(p.first.name):
                first_entrant, second_entrant = second_entrant, first_entrant
            Pairing.objects.create(
                division=division,
                round=round_num,
                round_pairings=rp_obj,
                first=first_entrant,
                second=second_entrant,
                repeats=p.repeats,
                table=0,
            )
