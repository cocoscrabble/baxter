"""Pairing generation and round status management.

Generates pairings from the pairing algorithm, resolves fixed table assignments,
assigns table numbers, and persists RoundPairings + Pairing records.
"""

from collections import defaultdict

from django.db import transaction
from django.db.models import Q

from .assign_tables import assign_tables, parse_board_table_map
from .events import as_derived, derived_writes
from .models import (
    BYE_PLAYER_NUMBER,
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

# The spread a bye or forfeit is scored at, when a division has no settings row
# to say otherwise. Jurisdictional rather than universal — see
# ``DivisionSettings.bye_spread``, which is what every live path reads.
BYE_WINNER_SCORE = 50
BYE_LOSER_SCORE = 0


def absence_spread(division):
    """Points awarded for a bye and charged for a forfeit in this division."""
    try:
        return division.settings.bye_spread
    except DivisionSettings.DoesNotExist:
        return BYE_WINNER_SCORE


def withdrawal_policy(division):
    """How this division records the rounds a withdrawn entrant misses."""
    try:
        return division.settings.withdrawal
    except DivisionSettings.DoesNotExist:
        return DivisionSettings.OMIT


# The strategies that lay out several rounds at once over a *fixed* field: the
# block is a template — a rotation, or a grouping into quads — decided when it
# begins rather than round by round from results. Losing a player partway
# through must not re-cut it.
#
# These are exactly the strategies that call ``guard_no_dropped_in_block`` in the
# engine (`strategies/roundrobin.rs`, `strategies/quads.rs`), which is the list
# to check this against: a strategy that guards there and is missing here goes
# back to refusing outright, and one added here that does not guard there is
# being handed a field it never asked to keep.
_FIXED_FIELD_STRATEGIES = frozenset(
    str(rp)
    for rp in (
        RP.RoundRobin,
        RP.DoubleRoundRobin,
        RP.Charlottesville,
        RP.Quads_Clustered,
        RP.Quads_Distributed,
        RP.Quads_Equalized,
        RP.Sixes,
    )
)


def committed_field_rounds(division, pd):
    """Rounds whose field is fixed because their block has already started.

    A round robin over N players is an N-1 round rotation of a template; a quad
    block is a grouping into fours. Either way the schedule is decided when the
    block begins, and it belongs to the players who began it. Losing one of them
    partway through does not re-cut it — it voids that player's remaining
    fixtures and leaves everyone else's alone.

    So for a block already under way the engine is handed the field that
    *started* it, withdrawals included, and ``regenerate_pairings`` dissolves the
    withdrawn player's fixtures when the pairings come back. A block that has not
    started yet is not committed to anybody and is simply solved for whoever is
    left, which is the better tournament.

    Blocks are keyed as the engine keys them (`strategies/roundrobin.rs`): the
    rounds sharing a pairing *and* a start_round.
    """
    started = set(
        division.round_pairings_set.exclude(status=RoundPairings.DRAFT)
        .values_list("round", flat=True)
    )
    if not started:
        return set()
    blocks = defaultdict(set)
    for rp in pd.round_pairings:
        if str(rp.pairing) in _FIXED_FIELD_STRATEGIES:
            blocks[(str(rp.pairing), rp.start_round)].add(rp.round)
    return {
        round_num
        for rounds in blocks.values()
        if rounds & started
        for round_num in rounds
    }


def commit_withdrawn_to_field(pd, committed):
    """Restore withdrawn entrants to the field for ``committed`` rounds.

    Two edits to the engine input, no engine change (the rules here are
    Baxter's, not the pairer's):

    * the withdrawal is lifted, so the round-robin template is cut for the field
      that started the block and every other player's fixtures stay put;
    * the entrant is marked *inactive* in every round outside those blocks,
      which is the engine's existing "no game, no bye, not withdrawn" — exactly
      what a withdrawal means to an ordinary round, and what playoffs already
      use to pair around reserved players.

    Returns the keys restored, which are the ones whose fixtures the caller has
    to dissolve on the way back.
    """
    if not committed:
        return set()
    restored = {e.player.key for e in pd.entrants if e.dropped}
    if not restored:
        return set()
    for entrant in pd.entrants:
        if entrant.player.key in restored:
            entrant.dropped = False
    for rp in pd.round_pairings:
        if rp.round not in committed:
            pd.inactive_players.setdefault(rp.round, [])
            pd.inactive_players[rp.round] = list(
                dict.fromkeys([*pd.inactive_players[rp.round], *sorted(restored)])
            )
    return restored


def _absence_row(division, round_pairings, entrant, *, forfeit):
    """One bye-shaped row: a bye when ``forfeit`` is false, else a forfeit."""
    return Pairing.objects.create(
        division=division,
        round=round_pairings.round,
        round_pairings=round_pairings,
        first=entrant,
        second=division.bye_entrant(),
        table=0,
        forfeit=forfeit,
    )


def _is_bye_key(key):
    """The engine identifies the bye by the key it was handed, which is now the
    bye player's reserved *number* rather than its name."""
    return key.casefold() == BYE_PLAYER_NUMBER.casefold()


def materialize_absences(division, round_num):
    """Record the automatic result for each bye and forfeit in a round.

    Called when a round is published, so the round can reach 'finished' without
    the director entering either by hand. Idempotent: only pairings with no
    result yet are touched.

    A bye and a forfeit are the same row with the winner the other way round.
    The byed player wins by the division's ``bye_spread``; the *absent* player
    loses by it, to the bye entrant. Which one this is comes from ``Pairing.forfeit``, recorded
    when the round was paired — not from the entrant's current ``dropped``
    state, which a rejoin moves out from under an already-published round.

    In both directions the bye entrant is the notional starter, so no real
    player is charged a start: ``winner_started`` is true exactly when the bye
    is the winner.
    """
    spread = absence_spread(division)
    absences = (
        division.pairings.filter(round=round_num, result__isnull=True)
        .filter(Q(first__player__is_bye=True) | Q(second__player__is_bye=True))
        .select_related("first__player", "second__player")
    )
    # Derived state (not a command): the result is a consequence of publish,
    # re-derived on replay.
    with derived_writes():
        for p in absences:
            if p.first.player.is_bye:
                bye_entrant, real_entrant = p.first, p.second
            else:
                bye_entrant, real_entrant = p.second, p.first
            if p.forfeit:
                winner, loser = bye_entrant, real_entrant
            else:
                winner, loser = real_entrant, bye_entrant
            ResultSlip.objects.create(
                division=division,
                round=round_num,
                pairing=p,
                winner=winner,
                winner_score=spread,
                loser=loser,
                loser_score=BYE_LOSER_SCORE,
                winner_started=winner.player.is_bye,
                forfeit=p.forfeit,
            )


def publish_rounds(division, round_numbers=None):
    """Publish draft rounds, auto-record their byes, and refresh round status.

    ``round_numbers=None`` publishes every draft round. Returns the rounds
    actually published. Centralises publishing so a bye or forfeit is always
    recorded the moment its round goes live.
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
    # Flip the status, materialize the byes and forfeits, and refresh status
    # atomically: a crash partway through must not leave a PUBLISHED round whose
    # derived results were never recorded (it could then never reach FINISHED
    # without manual entry).
    with transaction.atomic():
        published = list(qs.values_list("round", flat=True))
        qs.update(status=RoundPairings.PUBLISHED)
        for round_num in published:
            materialize_absences(division, round_num)
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
        .played()
        .values_list("round", flat=True)
    )
    to_unpublish = [r for r in candidates if r not in rounds_with_real_results]
    if not to_unpublish:
        return []
    with transaction.atomic():
        ResultSlip.objects.filter(
            division=division, round__in=to_unpublish
        ).not_played().delete()
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
            EnginePlayer(slip.first_key), EnginePlayer(slip.second_key)
        )
        starts.register(pairing, slip.round)
        repeats.add(pairing)
    return starts, repeats


def _add_missing_playoff_games(
    division, rp_obj, games, entrant_by_key, series_rows, starts, repeats
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
        first_entrant = entrant_by_key.get(series.high)
        second_entrant = entrant_by_key.get(series.low)
        if not first_entrant or not second_entrant:
            continue
        oriented = starts.add(
            EnginePairing(EnginePlayer(series.high), EnginePlayer(series.low)),
            rp_obj.round,
        )
        if oriented.first.key != series.high:
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
        pd.inactive_players = bracket.reserved_keys_by_round()
    if not pd.round_pairings and bracket is None:
        division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
        division.pairings.filter(round_pairings__isnull=True).delete()
        return
    _ensure_cop_config(division, pd)
    # A round-robin block already under way keeps the field that started it; the
    # withdrawn player's remaining fixtures are dissolved below rather than
    # re-cut out of the template.
    committed = committed_field_rounds(division, pd)
    committed_keys = commit_withdrawn_to_field(pd, committed)
    engine_rounds = dict(pair_with_engine(pd)) if pd.round_pairings else {}
    playoff_games = bracket.scheduled_by_round() if bracket is not None else {}
    if bracket is not None and playoff.timing == Playoff.POSTSCRIPT:
        # The main event ended at the qualification round, so a reserved round
        # holds playoff games and nothing else.
        for round_num in bracket.rounds:
            engine_rounds.pop(round_num, None)
    entrant_by_key = {
        e.player.player_number: e
        for e in division.entrants.select_related("player")
    }
    # Withdrawn entrants whose absence this division records rather than omits.
    # The policy is the division's, not the entrant's (``DivisionSettings.
    # withdrawal``); ``dropped`` is the per-entrant half — an entrant still
    # being paired plays their games and has nothing to forfeit.
    forfeiting = (
        list(division.entrants.filter(dropped=True))
        if withdrawal_policy(division) == DivisionSettings.FORFEIT
        else []
    )
    # Lazily resolve the bye opponent (created on first odd round) and map its
    # engine key to the division's bye entrant.
    bye_entrant = None

    def resolve_entrant(key):
        nonlocal bye_entrant
        if _is_bye_key(key):
            if bye_entrant is None:
                bye_entrant = division.bye_entrant()
            return bye_entrant
        return entrant_by_key.get(key)
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
    # Auto-created bye and forfeit results live in draft rounds until published;
    # drop them so the round can be re-paired cleanly (a draft round with a
    # lingering derived result would read as Partial and block regeneration).
    ResultSlip.objects.filter(
        division=division, round__in=draft_rounds
    ).not_played().delete()
    division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
    division.pairings.filter(round_pairings__isnull=True).delete()

    # Seeding order, used as a fallback rank for entrants the round's standings
    # don't cover (see below).
    seed_rank = {p.key: i + 1 for i, p in enumerate(standings_after_round(pd, 0))}
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
                    entrant_by_key, series_rows, starts, repeats,
                )
            continue

        start_round = start_round_by_round.get(round_num, 0)
        standings = standings_after_round(pd, start_round)
        rank = {p.key: i + 1 for i, p in enumerate(standings)}
        # A full round-robin schedule is generated up front, so later rounds
        # have no standings yet (no results played). Fall back to seeding order
        # for any entrant the standings don't cover, keeping board ordering and
        # fixed-table resolution well defined.
        for key, seed in seed_rank.items():
            rank.setdefault(key, len(standings) + seed)

        # Resolve entrants and effective fixed table for each pairing. Each entry
        # carries a sort key: playoff games take the top boards, best seed first,
        # then ordinary games by standings rank. Bye pairings are set aside: they
        # get no table and don't participate in the board-ordering sort.
        resolved = []
        bye_pairings = []
        # Fixtures the withdrawn player's absence dissolves in a committed
        # round-robin round: the opponent takes the bye, the absentee a forfeit.
        # Same shape as every other absence, so nothing downstream can tell a
        # dissolved fixture from a withdrawal that predated the pairing.
        dissolved = []
        for p in engine_rounds.get(round_num, []):
            first_entrant = resolve_entrant(p.first.key)
            second_entrant = resolve_entrant(p.second.key)
            if not first_entrant or not second_entrant:
                continue
            if _is_bye_key(p.first.key) or _is_bye_key(p.second.key):
                if p.first.key in committed_keys or p.second.key in committed_keys:
                    # The template handed the withdrawn player the round's bye.
                    # It is theirs to forfeit; nobody else gains one.
                    absent = (
                        first_entrant
                        if p.first.key in committed_keys
                        else second_entrant
                    )
                    dissolved.append((absent, None))
                    continue
                bye_pairings.append((p, first_entrant, second_entrant))
                continue
            if p.first.key in committed_keys or p.second.key in committed_keys:
                if p.first.key in committed_keys:
                    absent, opponent = first_entrant, second_entrant
                else:
                    absent, opponent = second_entrant, first_entrant
                dissolved.append((absent, opponent))
                continue
            ranks = (rank[p.first.key], rank[p.second.key])
            effective = effective_fixed_table(
                first_entrant, second_entrant, round_num, ranks
            )
            resolved.append(
                (p.repeats, first_entrant, second_entrant, effective,
                 (1, min(ranks)), None, None)
            )

        for series, game in playoff_games.get(round_num, []):
            # A series only schedules games once both participants are known,
            # but be defensive: an entrant withdrawn out from under the snapshot
            # should skip its game, not crash the whole regeneration.
            if series.high is None or series.low is None:
                continue
            first_entrant = entrant_by_key.get(series.high)
            second_entrant = entrant_by_key.get(series.low)
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
            if oriented.first.key != series.high:
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
        # is published (see materialize_absences). Show the real player first for
        # readability — orientation is display-only, the result encodes the win.
        for p, first_entrant, second_entrant in bye_pairings:
            if _is_bye_key(p.first.key):
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

        # A dissolved fixture: the opponent's bye, then the absentee's forfeit
        # (only if this division records absences at all).
        record_absence = withdrawal_policy(division) == DivisionSettings.FORFEIT
        for absent, opponent in dissolved:
            if opponent is not None:
                _absence_row(division, rp_obj, opponent, forfeit=False)
            if record_absence:
                _absence_row(division, rp_obj, absent, forfeit=True)

        # A withdrawn entrant whose absence is recorded as forfeits gets the same
        # shape, flagged. These are added *on top* of what the engine returned
        # rather than mixed into it: the engine never saw the withdrawn entrant
        # (``dropped`` removes them from the pairable field), so the odd-field
        # bye above was computed on the remaining players and adding these
        # cannot disturb its parity. A committed round has already dealt with
        # them just above, so it is skipped here.
        if round_num not in committed:
            for entrant in forfeiting:
                _absence_row(division, rp_obj, entrant, forfeit=True)

    if bracket is not None:
        # Published rounds are not rebuilt above, so a correction that retired
        # one of their games is applied here.
        prune_unnecessary_pairings(division, bracket)
