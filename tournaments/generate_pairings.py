"""Pairing generation and round status management.

Generates pairings from the pairing algorithm, resolves fixed table assignments,
assigns table numbers, and persists RoundPairings + Pairing records.
"""

from django.db import transaction
from django.db.models import Q

from .assign_tables import assign_tables, parse_board_table_map
from .events import as_derived, derived_writes
from .models import (
    BYE_PLAYER_NAME,
    DivisionSettings,
    Pairing,
    Playoff,
    ResultSlip,
    RoundPairings,
    default_cop_config,
)
from .pairing.base import PairingData, Starts, standings_after_round
from .pairing.base import Pairing as EnginePairing
from .pairing.base import Player as EnginePlayer
from .pairing.base import Repeats as EngineRepeats
from .pairing.engine import pair_with_engine
from .pairing.round_pairing import RP
from .playoff import (
    build_bracket,
    playoff_for,
    prune_unnecessary_pairings,
    sync_series,
)

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
    # Derived state (not a command): the bye result is a consequence of publish,
    # re-derived on replay.
    with derived_writes():
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
    if playoff_for(division) is not None:
        # A playoff round's contents depend on results that may have landed
        # since the schedule was last rendered — a series that went 1–1 needs its
        # decider, which a stale draft round wouldn't hold. Regenerating here
        # (drafts only, and idempotent) means publishing never ships a stale
        # window, and makes the live path identical to replay, which regenerates
        # before every publish event.
        regenerate_pairings(division)
    qs = division.round_pairings_set.filter(status=RoundPairings.DRAFT)
    if round_numbers is not None:
        qs = qs.filter(round__in=round_numbers)
    # Flip the status, materialize byes, and refresh status atomically: a crash
    # partway through must not leave a PUBLISHED round whose byes were never
    # recorded (it could then never reach FINISHED without manual entry).
    with transaction.atomic():
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


def _ensure_cop_config(division, pd):
    """Seed default COP config the first time a division with a COP round is
    paired. COP can't pair without ``cop_config``; rather than fail, drop in the
    defaults (persisted, so the organizer can then tune them on the settings tab)
    and use them for this pairing. A division already configured is left alone;
    a schedule with no COP round never gets one."""
    if pd.cop_config or not any(rp.pairing == RP.COP for rp in pd.round_pairings):
        return
    cfg = default_cop_config()
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.cop_config = cfg
    settings_obj.save(update_fields=["cop_config"])
    pd.cop_config = cfg


def _playoff_history(pd):
    """Starts and repeats replayed from every played game.

    Playoff games are created here rather than by the engine, so they need the
    same first/second rule applied by hand — Baxter's ordinary one: fewest
    starts, then head-to-head, then recency. Seeding both trackers from the
    result history makes a series alternate naturally and keeps a playoff game's
    ``repeats`` count on the same footing as an ordinary pairing's.
    """
    starts = Starts()
    repeats = EngineRepeats()
    for slip in sorted(pd.result_slips, key=lambda s: s.round):
        pairing = EnginePairing(
            EnginePlayer(slip.first_name), EnginePlayer(slip.second_name)
        )
        starts.register(pairing, slip.round)
        repeats.add(pairing)
    return starts, repeats


def _add_missing_playoff_games(
    division, rp_obj, games, entrant_by_name, series_rows, starts, repeats
):
    """Create any scheduled playoff game a published round is missing.

    Published rounds are never rebuilt, so this only adds — existing pairings and
    their results are untouched. New games take the boards after the ones already
    assigned.
    """
    existing = {
        (p.series_id, p.game_number)
        for p in rp_obj.pairings.all()
        if p.series_id is not None
    }
    next_table = max(
        (p.table for p in rp_obj.pairings.all()), default=0
    )
    for series, game in games:
        series_row = series_rows.get((series.key, series.position))
        if series_row is None or (series_row.pk, game.number) in existing:
            continue
        first_entrant = entrant_by_name.get(series.high)
        second_entrant = entrant_by_name.get(series.low)
        if not first_entrant or not second_entrant:
            continue
        oriented = starts.add(
            EnginePairing(EnginePlayer(series.high), EnginePlayer(series.low)),
            rp_obj.round,
        )
        if oriented.first.name != series.high:
            first_entrant, second_entrant = second_entrant, first_entrant
        next_table += 1
        Pairing.objects.create(
            division=division,
            round=rp_obj.round,
            round_pairings=rp_obj,
            first=first_entrant,
            second=second_entrant,
            repeats=repeats.add(oriented),
            table=next_table,
            series=series_row,
            game_number=game.number,
        )
    rp_obj.update_status()


@transaction.atomic
@as_derived
def regenerate_pairings(division):
    """Run the pairing algorithm and save results to the Pairing table.

    Only draft RoundPairings are deleted and recreated. Published, in-progress,
    and finished rounds are preserved. Atomic, so a PairingError (unsatisfiable
    fixed pairings) raised by ``pair()`` rolls back cleanly, leaving the existing
    schedule untouched for the caller to surface the error.

    A division with a playoff also gets its bracket's games. Those come from the
    derived bracket, not the engine: only the games the bracket says are needed
    are created, which is what keeps a clinched-away game from ever existing.
    """
    pd = PairingData.for_division(division)
    playoff = playoff_for(division)
    bracket = None
    playoff_config = None
    series_rows = {}
    if playoff is not None:
        playoff_config = playoff.config()
        bracket = build_bracket(playoff_config, pd.result_slips)
        series_rows = sync_series(playoff, bracket)
        # Reserved players sit out ordinary pairing for the whole playoff.
        pd.inactive_players = bracket.reserved_names_by_round()
    if not pd.round_pairings and bracket is None:
        division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
        division.pairings.filter(round_pairings__isnull=True).delete()
        return
    _ensure_cop_config(division, pd)
    engine_rounds = dict(pair_with_engine(pd)) if pd.round_pairings else {}
    playoff_games = bracket.scheduled_by_round() if bracket is not None else {}
    if bracket is not None and playoff.timing == Playoff.POSTSCRIPT:
        # The main event ended at the qualification round, so a reserved round
        # holds playoff games and nothing else.
        for round_num in bracket.rounds:
            engine_rounds.pop(round_num, None)
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
    # Playoff games are oriented (and their repeats counted) here rather than by
    # the engine, from the same history the engine would have used.
    starts, repeats = _playoff_history(pd)

    def effective_fixed_table(first_entrant, second_entrant, round_num, ranks):
        """The table two players are pinned to this round, or None."""
        first_ft = get_fixed_table(fixed_table_lookup, first_entrant.pk, round_num)
        second_ft = get_fixed_table(fixed_table_lookup, second_entrant.pk, round_num)
        if first_ft and second_ft:
            return resolve_fixed_table(first_ft, second_ft, *ranks)
        if first_ft:
            return first_ft[0]
        if second_ft:
            return second_ft[0]
        return None

    rounds_to_build = set(engine_rounds) | set(playoff_games)
    if bracket is not None:
        # Every reserved round gets a container even when it holds no games —
        # a window whose series all clinched early still exists, and says so.
        rounds_to_build |= set(bracket.rounds)

    for round_num in sorted(rounds_to_build):
        # Create the RoundPairings container for this round.
        rp_obj, _ = RoundPairings.objects.get_or_create(
            division=division,
            round=round_num,
            defaults={"status": RoundPairings.DRAFT},
        )
        # Skip rounds that already have a non-draft status (shouldn't happen
        # since pair() skips finished rounds, but be defensive) — except that a
        # published playoff round can still *gain* a game: a director may
        # publish a whole window before a series goes 1–1 and needs its decider.
        # Adding the missing game is the mirror of the pruner removing one a
        # clinch retired; both keep a published window true to the bracket.
        if rp_obj.status != RoundPairings.DRAFT:
            if playoff_games.get(round_num) and rp_obj.status in (
                RoundPairings.PUBLISHED,
                RoundPairings.IN_PROGRESS,
            ):
                _add_missing_playoff_games(
                    division, rp_obj, playoff_games[round_num],
                    entrant_by_name, series_rows, starts, repeats,
                )
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

        # Resolve entrants and effective fixed table for each pairing. Each entry
        # carries a sort key: playoff games take the top boards, best seed first,
        # then ordinary games by standings rank. Bye pairings are set aside: they
        # get no table and don't participate in the board-ordering sort.
        resolved = []
        bye_pairings = []
        for p in engine_rounds.get(round_num, []):
            first_entrant = resolve_entrant(p.first.name)
            second_entrant = resolve_entrant(p.second.name)
            if not first_entrant or not second_entrant:
                continue
            if _is_bye_name(p.first.name) or _is_bye_name(p.second.name):
                bye_pairings.append((p, first_entrant, second_entrant))
                continue
            ranks = (rank[p.first.name], rank[p.second.name])
            effective = effective_fixed_table(
                first_entrant, second_entrant, round_num, ranks
            )
            resolved.append(
                (p.repeats, first_entrant, second_entrant, effective,
                 (1, min(ranks)), None, None)
            )

        for series, game in playoff_games.get(round_num, []):
            # A series only schedules games once both participants are known,
            # but be defensive: an entrant renamed out from under the snapshot
            # should skip its game, not crash the whole regeneration.
            if series.high is None or series.low is None:
                continue
            first_entrant = entrant_by_name.get(series.high)
            second_entrant = entrant_by_name.get(series.low)
            if not first_entrant or not second_entrant:
                continue
            # Who goes first: Baxter's ordinary starts rule, so a series
            # alternates and a participant's tournament-wide starts stay level.
            oriented = starts.add(
                EnginePairing(
                    EnginePlayer(series.high), EnginePlayer(series.low)
                ),
                round_num,
            )
            if oriented.first.name != series.high:
                first_entrant, second_entrant = second_entrant, first_entrant
            reps = repeats.add(oriented)
            seeds = [
                seed
                for seed in (
                    playoff_config.seed_of(series.high),
                    playoff_config.seed_of(series.low),
                )
                if seed
            ]
            ranks = (rank.get(series.high, 1), rank.get(series.low, 1))
            effective = effective_fixed_table(
                first_entrant, second_entrant, round_num, ranks
            )
            resolved.append(
                (reps, first_entrant, second_entrant, effective,
                 (0, min(seeds, default=0)),
                 series_rows.get((series.key, series.position)), game.number)
            )

        # Order pairings so the top game claims the first board. Fixed-table
        # pairings are placed at their forced table; the rest fill remaining
        # boards in this order.
        resolved.sort(key=lambda r: r[4])
        ids = list(range(len(resolved)))
        fixed_by_id = {i: r[3] for i, r in enumerate(resolved) if r[3] is not None}
        table_by_id = assign_tables(ids, fixed_by_id, board_table_map)

        for i, entry in enumerate(resolved):
            reps, first_entrant, second_entrant, _, _, series_row, game_number = entry
            table_order, table_label = table_by_id[i]
            Pairing.objects.create(
                division=division,
                round=round_num,
                round_pairings=rp_obj,
                first=first_entrant,
                second=second_entrant,
                repeats=reps,
                table=table_order,
                table_label=table_label,
                series=series_row,
                game_number=game_number,
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

    if bracket is not None:
        # Published rounds are not rebuilt above, so a correction that retired
        # one of their games is applied here.
        prune_unnecessary_pairings(division, bracket)
