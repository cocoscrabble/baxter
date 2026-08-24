"""The command hub.

Commands are the only functions that mutate tournament state, each wrapped in
``@records_event`` so the mutation and its log entry commit together. Most
command *bodies* live in their natural domain modules (fixed_pairings.py,
generate_pairings.py, …); this module holds the registry, the CRUD commands that
have no other home (tournament/division lifecycle), and imports of the domain
modules so their decorators register on load.

Every command has the signature ``(tournament, actor, payload) -> EventResult``.
The payload is natural-key based (names, not pks) so it survives replay into a
fresh database.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.db.models import Q

from tournaments.events import EventResult, command_context, records_event
from tournaments.models import (
    Division,
    DivisionSettings,
    ResultSlip,
    Tournament,
    default_cop_config,
)
from tournaments.playoff import refresh_after_results

# Imported for its @records_event side effect: the start-correction command lives
# with the policy it enforces, but has to be registered before a replay can
# dispatch it.
from tournaments import starts  # noqa: F401

User = get_user_model()


def _resolve_editors(usernames):
    return list(User.objects.filter(username__in=usernames))


def _apply_seed(division, payload):
    """Ensure the division's settings (hence pairing_seed) exist, and reconcile
    the seed with the payload.

    A fresh division draws a random seed; record it so a replay reproduces the
    same random-strategy pairings. A replayed division carries the seed in its
    payload; set it explicitly. Returns the (possibly seed-augmented) payload.
    """
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    seed = payload.get("pairing_seed")
    if seed is None:
        return {**payload, "pairing_seed": settings_obj.pairing_seed}
    if settings_obj.pairing_seed != seed:
        settings_obj.pairing_seed = seed
        settings_obj.save(update_fields=["pairing_seed"])
    return payload


# ---------------------------------------------------------------------------
# Tournament lifecycle
# ---------------------------------------------------------------------------


@records_event("tournament_created")
def create_tournament(tournament, actor, payload):
    """payload: {name, location, start_date (ISO), editors: [username],
    is_fake, default_division: {name, pairing_seed}}. ``actor`` becomes the
    owner. ``is_fake`` marks a sandbox tournament (e.g. a what-if import)."""
    t = Tournament.objects.create(
        name=payload["name"],
        location=payload["location"],
        start_date=date.fromisoformat(payload["start_date"]),
        owner=actor,
        is_fake=payload.get("is_fake", False),
    )
    editors = set(_resolve_editors(payload.get("editors", [])))
    editors.add(actor)
    t.editors.set(editors)

    # Every tournament starts with one division. Record its name + seed so a
    # replay reproduces it identically.
    default = dict(payload.get("default_division") or {})
    div = Division.objects.create(tournament=t, name=default.get("name", "Division 1"))
    default = {**default, "name": div.name, **_apply_seed(div, default)}
    out_payload = {**payload, "default_division": default}
    return EventResult(payload=out_payload, tournament=t, result=t)


@records_event("tournament_updated")
def update_tournament(tournament, actor, payload):
    """payload: {name, location, start_date (ISO), editors: [username]}."""
    tournament.name = payload["name"]
    tournament.location = payload["location"]
    tournament.start_date = date.fromisoformat(payload["start_date"])
    tournament.save()
    editors = set(_resolve_editors(payload.get("editors", [])))
    editors.add(tournament.owner)
    tournament.editors.set(editors)
    return EventResult(payload=payload, tournament=tournament, result=tournament)


def delete_tournament(tournament, actor, payload):
    """Hard delete. Can't record an event — a tournament_deleted row would
    cascade away with the tournament — so this runs in a command context (for
    the write guard) without logging."""
    with command_context():
        tournament.delete()
    return None


# ---------------------------------------------------------------------------
# Division lifecycle
# ---------------------------------------------------------------------------


@records_event("division_created")
def create_division(tournament, actor, payload):
    """payload: {name, is_test, pairing_seed}."""
    div = Division.objects.create(
        tournament=tournament,
        name=payload["name"],
        is_test=payload.get("is_test", False),
    )
    out_payload = _apply_seed(div, payload)
    return EventResult(payload=out_payload, division=div, result=div)


@records_event("division_renamed")
def rename_division(tournament, actor, payload):
    """payload: {old_name, new_name}."""
    div = Division.objects.get(tournament=tournament, name=payload["old_name"])
    div.name = payload["new_name"]
    div.save(update_fields=["name", "slug"])
    return EventResult(payload=payload, division=div, result=div)


@records_event("division_deleted")
def delete_division(tournament, actor, payload):
    """payload: {name}. Soft delete."""
    div = Division.objects.get(tournament=tournament, name=payload["name"])
    div.soft_delete()
    return EventResult(payload=payload, division=div, result=div)


@records_event("division_restored")
def restore_division(tournament, actor, payload):
    """payload: {name}. Restore a soft-deleted division."""
    div = Division.all_objects.get(tournament=tournament, name=payload["name"])
    div.restore()
    return EventResult(payload=payload, division=div, result=div)


# ---------------------------------------------------------------------------
# Publishing (thin adapters over generate_pairings; the bodies stay there)
# ---------------------------------------------------------------------------


def _division(tournament, name):
    return Division.objects.get(tournament=tournament, name=name)


@records_event("rounds_published")
def publish_all_rounds(tournament, actor, payload):
    """payload: {division}. Publishes every draft round; records which."""
    from tournaments.generate_pairings import publish_rounds

    division = _division(tournament, payload["division"])
    published = publish_rounds(division)
    out = {**payload, "rounds": published}
    return EventResult(
        payload=out, division=division, result=published, record=bool(published)
    )


@records_event("round_published")
def publish_round(tournament, actor, payload):
    """payload: {division, round}."""
    from tournaments.generate_pairings import publish_rounds

    division = _division(tournament, payload["division"])
    published = publish_rounds(division, [payload["round"]])
    return EventResult(
        payload=payload, division=division, result=published, record=bool(published)
    )


@records_event("round_unpublished")
def unpublish_round(tournament, actor, payload):
    """payload: {division, round}."""
    from tournaments.generate_pairings import unpublish_rounds

    division = _division(tournament, payload["division"])
    unpublished = unpublish_rounds(division, [payload["round"]])
    return EventResult(
        payload=payload, division=division, result=unpublished, record=bool(unpublished)
    )


# ---------------------------------------------------------------------------
# Fixed pairings (thin adapters; pk args translated to/from player numbers)
# ---------------------------------------------------------------------------


def _entrant(division, key):
    """The entrant a payload's player *number* refers to.

    Payloads are pk-free so they replay into a fresh database, but they are not
    name-keyed: two entrants may share a name, and a payload that named one of
    them would be ambiguous forever after. See plans/PLAN_PLAYER_IDENTITY.md.
    """
    return division.entrants.get(player__player_number=key)


@records_event("fixed_pairing_added")
def add_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, player1, player2} — players by number."""
    from tournaments.fixed_pairings import add_fixed_pairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["player1"])
    e2 = _entrant(division, payload["player2"])
    ok, error = add_fixed_pairing(division, payload["round"], e1.pk, e2.pk)
    return EventResult(
        payload=payload, division=division, result=(ok, error), record=ok
    )


@records_event("fixed_pairing_removed")
def remove_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, player1, player2} — players by number."""
    from tournaments.fixed_pairings import remove_fixed_pairing
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["player1"])
    e2 = _entrant(division, payload["player2"])
    fp = FixedPairing.objects.filter(
        division=division, round_number=payload["round"]
    ).filter(
        Q(entrant1=e1, entrant2=e2) | Q(entrant1=e2, entrant2=e1)
    ).first()
    if fp is None:
        return EventResult(payload=payload, result=(False, None), record=False)
    ok, error = remove_fixed_pairing(division, fp.pk)
    return EventResult(
        payload=payload, division=division, result=(ok, error), record=ok
    )


# ---------------------------------------------------------------------------
# Single result entry (ResultSlipCreateView; the form only validates now)
# ---------------------------------------------------------------------------


def _find_pairing(division, round_number, first_key, second_key):
    keys = {first_key, second_key}
    for p in division.pairings.filter(round=round_number).select_related(
        "first__player", "second__player"
    ):
        if {p.first.key, p.second.key} == keys:
            return p
    return None


def _write_result(division, pairing, payload, instance):
    winner = (
        pairing.first
        if pairing.first.key == payload["winner_player"]
        else pairing.second
    )
    loser = pairing.second if winner.pk == pairing.first_id else pairing.first
    fields = dict(
        division=division,
        round=pairing.round,
        pairing=pairing,
        winner=winner,
        winner_score=payload["winner_score"],
        loser=loser,
        loser_score=payload["loser_score"],
        winner_started=winner.pk == pairing.first_id,
    )
    if instance is not None:
        for name, value in fields.items():
            setattr(instance, name, value)
        instance.save()
        slip = instance
    else:
        slip = ResultSlip.objects.create(**fields)
    # Recompute round status inside the command so the recorded digest reflects
    # it (a replay must see the same status). Same for retiring the playoff games
    # this result may just have made unnecessary.
    if pairing.round_pairings_id:
        pairing.round_pairings.update_status()
    refresh_after_results(division)
    return slip


@records_event("result_added")
def add_result(tournament, actor, payload):
    """payload: {division, round, first_player, second_player, winner_player,
    winner_score, loser_score} — players by number. Records a new game result."""
    division = _division(tournament, payload["division"])
    pairing = _find_pairing(
        division, payload["round"], payload["first_player"], payload["second_player"]
    )
    slip = _write_result(division, pairing, payload, instance=None)
    return EventResult(payload=payload, division=division, result=slip)


@records_event("result_edited")
def edit_result(tournament, actor, payload):
    """payload as add_result. Updates the pairing's existing slip in place."""
    division = _division(tournament, payload["division"])
    pairing = _find_pairing(
        division, payload["round"], payload["first_player"], payload["second_player"]
    )
    instance = getattr(pairing, "result", None)
    slip = _write_result(division, pairing, payload, instance=instance)
    return EventResult(payload=payload, division=division, result=slip)


# ---------------------------------------------------------------------------
# Settings, bulk import, simulation
# ---------------------------------------------------------------------------


@records_event("division_settings_saved")
def save_settings(tournament, actor, payload):
    """payload: {division, blocks}. round_pairings are derived from the blocks."""
    from tournaments.pairing.round_pairing import RP, blocks_to_round_pairings

    division = _division(tournament, payload["division"])
    blocks = payload["blocks"]
    round_pairings = [rp.to_dict() for rp in blocks_to_round_pairings(blocks)]
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.pairing_blocks = blocks
    settings_obj.round_pairings = round_pairings
    update_fields = ["pairing_blocks", "round_pairings"]
    if not settings_obj.cop_config and any(
        pairing["pairing"] == str(RP.COP) for pairing in round_pairings
    ):
        settings_obj.cop_config = default_cop_config()
        update_fields.append("cop_config")
    settings_obj.save(update_fields=update_fields)
    return EventResult(payload=payload, division=division, result=settings_obj)


@records_event("division_cop_config_saved")
def save_cop_config(tournament, actor, payload):
    """payload: {division, cop_config} — COP's prize/tuning config for the division."""
    division = _division(tournament, payload["division"])
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.cop_config = payload["cop_config"]
    settings_obj.save(update_fields=["cop_config"])
    return EventResult(payload=payload, division=division, result=settings_obj)


# ---------------------------------------------------------------------------
# Playoffs
# ---------------------------------------------------------------------------

# The playoff's whole recorded intent. Everything else about a bracket — who
# meets whom, series scores, which games are still needed, final placements — is
# derived from this plus the division's results, so these three commands are the
# only playoff events the log ever carries.


def _seeds_with_keys(division, seeds):
    """Seed entries guaranteed to carry a ``key``.

    A payload written before the bracket keyed on player numbers has only
    ``player``. Resolving it by name is exact for those payloads — the schema
    they were written under enforced globally unique names — and is the one
    place that assumption is relied on.
    """
    by_name = None
    out = []
    for seed in seeds:
        if seed.get("key"):
            out.append(seed)
            continue
        if by_name is None:
            by_name = {
                e.player.name: e.player.player_number
                for e in division.entrants.select_related("player")
            }
        out.append({**seed, "key": by_name.get(seed["player"], seed["player"])})
    return out


def _playoff_payload(seeds, payload):
    """Normalize a create/update payload into a PlayoffConfig, or raise."""
    from tournaments.playoff import PlayoffConfig, validate_config

    config = PlayoffConfig(
        qualification_round=int(payload["qualification_round"]),
        qualifier_count=int(payload["qualifier_count"]),
        timing=payload["timing"],
        stage_games={k: int(v) for k, v in payload["stage_games"].items()},
        seeds=tuple(s["key"] for s in seeds),
    )
    errors = validate_config(config)
    if errors:
        raise ValueError("; ".join(errors))
    return config


def _save_playoff(division, payload):
    from tournaments.models import Playoff
    from tournaments.playoff import schedule_conflicts

    seeds = _seeds_with_keys(division, payload["seeds"])
    config = _playoff_payload(seeds, payload)
    errors = schedule_conflicts(division, config)
    if errors:
        raise ValueError("; ".join(errors))
    playoff, _ = Playoff.objects.update_or_create(
        division=division,
        defaults={
            "qualification_round": int(payload["qualification_round"]),
            "qualifier_count": int(payload["qualifier_count"]),
            "timing": payload["timing"],
            "stage_games": {k: int(v) for k, v in payload["stage_games"].items()},
            "seeds": seeds,
        },
    )
    return playoff


@records_event("playoff_created")
def create_playoff(tournament, actor, payload):
    """payload: {division, qualification_round, qualifier_count, timing,
    stage_games: {series key: games}, seeds: [{seed, key, player, wins, spread}]}.

    ``seeds`` is the confirmed qualification snapshot — the director may have
    overridden it, and freezing it here is what makes the bracket reproducible
    when two qualifiers were exactly level. ``key`` is the identity the bracket
    derives from; ``player`` rides along so the record stays readable."""
    division = _division(tournament, payload["division"])
    playoff = _save_playoff(division, payload)
    return EventResult(payload=payload, division=division, result=playoff)


@records_event("playoff_updated")
def update_playoff(tournament, actor, payload):
    """payload as create_playoff. Reconfigures a playoff that has not yet been
    played; the caller checks that no playoff game has a result."""
    division = _division(tournament, payload["division"])
    playoff = _save_playoff(division, payload)
    return EventResult(payload=payload, division=division, result=playoff)


@records_event("playoff_deleted")
def delete_playoff(tournament, actor, payload):
    """payload: {division}. Removes the playoff and its series rows; the draft
    playoff rounds regenerate away."""
    from tournaments.models import Playoff

    division = _division(tournament, payload["division"])
    deleted, _ = Playoff.objects.filter(division=division).delete()
    return EventResult(
        payload=payload, division=division, result=deleted, record=bool(deleted)
    )


@records_event("entrants_bulk_imported")
def bulk_import_entrants(tournament, actor, payload):
    """payload: {division, csv} — the raw CSV text, verbatim.

    Deliberately *not* number-keyed: the payload is the document the director
    pasted, and re-keying it would mean inventing numbers for rows that have
    none. New players are created by name and a replay resolves/creates them the
    same way, which is exactly reproducible because the CSV is byte-identical.
    Ambiguity in that CSV is the importer's problem to report, not the log's to
    hide (see phase 4)."""
    from tournaments.import_entrants import import_entrants

    division = _division(tournament, payload["division"])
    result, errors = import_entrants(division, payload["csv"])
    if errors:
        return EventResult(payload=payload, result=(None, errors), record=False)
    return EventResult(payload=payload, division=division, result=(result, errors))


@records_event("division_imported")
def import_division(tournament, actor, payload):
    """payload: a portable division (see ``whatif_import``): {name, entrants:
    [{player, rating, number}], results: [{round, winner, loser, winner_score,
    loser_score, winner_started}]}.

    Reconstructs a finished sandbox division from historical results: entrants,
    ``Pairing`` + ``ResultSlip`` rows derived from the results (a 50–0 bye
    inferred for any entrant idle in a played round), and a nominal Swiss
    schedule with every round FINISHED. The division is ``is_test`` — hidden from
    non-editors and excluded from registry export. Bye inference lives here (not
    in the parser) so a replay reproduces it. Returns an import summary.

    Name-keyed, like ``entrants_bulk_imported`` and for the same reason: the
    payload *is* the historical document, and those documents identify people by
    name because that is all they have. ``resolve_player`` mints a ``T-`` number
    for anyone new, so the sandbox division ends up number-keyed like any other
    — but the log records what was imported, not a re-keyed rewrite of it."""
    from collections import defaultdict

    from tournaments.generate_pairings import BYE_LOSER_SCORE, BYE_WINNER_SCORE
    from tournaments.grids import resolve_player
    from tournaments.models import Entrant, Pairing, Player, RoundPairings
    from tournaments.pairing.round_pairing import blocks_to_round_pairings

    division, _ = Division.objects.get_or_create(
        tournament=tournament, name=payload["name"]
    )
    if not division.is_test:
        division.is_test = True
        division.save(update_fields=["is_test"])

    ent_by_name = {}
    created, matched = [], []
    for e in payload["entrants"]:
        exists = Player.objects.filter(name__iexact=e["player"]).exists()
        player = resolve_player(None, e["player"], e.get("rating", 0))
        (matched if exists else created).append(player.name)
        ent_by_name[e["player"]] = Entrant.objects.create(
            division=division, player=player, number=e["number"]
        )

    def entrant(name):
        ent = ent_by_name.get(name)
        if ent is None:  # a result names a non-entrant — add them at the bottom
            ent = Entrant.objects.create(
                division=division, player=resolve_player(None, name),
                number=1000 + len(ent_by_name),
            )
            ent_by_name[name] = ent
        return ent

    by_round = defaultdict(list)
    for r in payload["results"]:
        by_round[r["round"]].append(r)

    inferred_byes = []
    for round_num in sorted(by_round):
        rp = RoundPairings.objects.create(
            division=division, round=round_num, status=RoundPairings.FINISHED
        )
        played = set()
        games = []
        for r in by_round[round_num]:
            w, l = entrant(r["winner"]), entrant(r["loser"])
            games.append(
                (w, l, r["winner_score"], r["loser_score"], r["winner_started"])
            )
            played.update({w.pk, l.pk})
        # The top game (lowest entrant number in the pair) claims board 1.
        games.sort(key=lambda g: min(g[0].number, g[1].number))
        for table, (w, l, wscore, lscore, wstarted) in enumerate(games, start=1):
            first, second = (w, l) if wstarted else (l, w)
            pairing = Pairing.objects.create(
                division=division, round=round_num, round_pairings=rp,
                first=first, second=second, table=table,
            )
            ResultSlip.objects.create(
                division=division, round=round_num, pairing=pairing,
                winner=w, winner_score=wscore, loser=l, loser_score=lscore,
                winner_started=wstarted,
            )
        # Infer a bye for every non-dropped entrant idle in this played round.
        bye_ent = None
        for e in list(ent_by_name.values()):
            if e.pk in played or e.dropped:
                continue
            if bye_ent is None:
                bye_ent = division.bye_entrant()
            pairing = Pairing.objects.create(
                division=division, round=round_num, round_pairings=rp,
                first=e, second=bye_ent, table=0,
            )
            ResultSlip.objects.create(
                division=division, round=round_num, pairing=pairing,
                winner=e, winner_score=BYE_WINNER_SCORE,
                loser=bye_ent, loser_score=BYE_LOSER_SCORE, winner_started=False,
            )
            inferred_byes.append([round_num, e.player.name])

    # A nominal schedule so the Pair-rounds page renders; every round is already
    # FINISHED, so nothing is regenerated.
    max_round = max(by_round) if by_round else 0
    blocks = [{"pairing": "Swiss", "rounds": max_round, "pair_from": 1}]
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.pairing_blocks = blocks
    settings_obj.round_pairings = [
        rp.to_dict() for rp in blocks_to_round_pairings(blocks)
    ]
    settings_obj.save()

    summary = {
        "division": division.name,
        "entrants": len(ent_by_name),
        "results": len(payload["results"]),
        "created_players": sorted(set(created)),
        "matched_players": sorted(set(matched)),
        "inferred_byes": inferred_byes,
    }
    return EventResult(payload=payload, division=division, result=summary)


def _sim_result_dict(slip):
    return {
        "first_player": slip.pairing.first.key,
        "second_player": slip.pairing.second.key,
        "winner_player": slip.winner.key,
        "loser_player": slip.loser.key,
        "winner_score": slip.winner_score,
        "loser_score": slip.loser_score,
        "winner_started": slip.winner_started,
    }


def _apply_sim_result(division, round_num, r):
    pairing = _find_pairing(
        division, round_num, r["first_player"], r["second_player"]
    )
    slip = ResultSlip.objects.create(
        division=division,
        round=round_num,
        pairing=pairing,
        winner=_entrant(division, r["winner_player"]),
        winner_score=r["winner_score"],
        loser=_entrant(division, r["loser_player"]),
        loser_score=r["loser_score"],
        winner_started=r["winner_started"],
    )
    if pairing is not None and pairing.round_pairings_id:
        pairing.round_pairings.update_status()
    refresh_after_results(division)
    return slip


@records_event("match_simulated")
def simulate_match_cmd(tournament, actor, payload):
    """payload: {division, round, first_player, second_player} — players by
    number. The generated scores are recorded (the RNG is unseeded, so a replay
    applies them, not re-rolls)."""
    from tournaments.match_simulation import simulate_match

    division = _division(tournament, payload["division"])
    if "result" in payload:  # replay: apply the recorded result
        slip = _apply_sim_result(division, payload["round"], payload["result"])
        return EventResult(payload=payload, division=division, result=slip)
    first = _entrant(division, payload["first_player"])
    second = _entrant(division, payload["second_player"])
    slip = simulate_match(division, payload["round"], first, second)
    if slip.pairing_id and slip.pairing.round_pairings_id:
        slip.pairing.round_pairings.update_status()
    refresh_after_results(division)
    out = {**payload, "result": _sim_result_dict(slip)}
    return EventResult(payload=out, division=division, result=slip)


@records_event("round_simulated")
def simulate_round_cmd(tournament, actor, payload):
    """payload: {division, round}. Records every generated result for replay."""
    from tournaments.match_simulation import simulate_round

    division = _division(tournament, payload["division"])
    round_num = payload["round"]
    if "results" in payload:  # replay: apply the recorded results
        for r in payload["results"]:
            _apply_sim_result(division, round_num, r)
        # Mirror the live path even when there was nothing to apply: simulating
        # an empty round still settles its status (a playoff window round whose
        # series all clinched early is finished, not merely published).
        rp = division.round_pairings_set.filter(round=round_num).first()
        if rp:
            rp.update_status()
        refresh_after_results(division)
        return EventResult(payload=payload, division=division, result=None)
    before = set(
        division.result_slips.filter(round=round_num).values_list("pk", flat=True)
    )
    simulate_round(division, round_num)
    created = (
        division.result_slips.filter(round=round_num)
        .exclude(pk__in=before)
        .select_related("pairing__first__player", "pairing__second__player",
                        "winner__player", "loser__player")
    )
    out = {**payload, "results": [_sim_result_dict(s) for s in created]}
    return EventResult(payload=out, division=division, result=None)


# ---------------------------------------------------------------------------
# Player identity
# ---------------------------------------------------------------------------


@records_event("player_number_changed")
def change_player_number(tournament, actor, payload):
    """payload: {old, new} — rewrite a player's number in place.

    Identity is point-in-time (plans/PLAN_PLAYER_IDENTITY.md decision 2): what
    matters is that it is consistent at any given moment, not that it is
    immutable. The case this exists for is number resolution — a guest enters as
    ``T-7``, a CoCo admin assigns them ``0412`` centrally, Baxter pulls it and
    the director confirms — and the event is what keeps the append-only log
    truthful across the rewrite, since every earlier payload names the old
    number and every later one names the new.

    Recorded against the tournament whose director resolved it. A player in two
    tournaments gets one event in each log, and either log replays on its own to
    a consistent state.

    Applying the registry's whole id_map after an upload is *not* here — that
    belongs with the upload transport. This is the log half, ready before it.
    """
    from tournaments.models import (
        TEMP_NUMBER_PREFIX,
        Player,
        canonical_player_number,
    )

    old = canonical_player_number(payload["old"])
    new = canonical_player_number(payload["new"])
    if old == new:
        return EventResult(payload=payload, tournament=tournament, record=False)

    player = Player.objects.filter(player_number=old).first()
    if player is None:
        raise ValueError(f"No player with number {payload['old']!r}.")
    holder = Player.objects.filter(player_number=new).exclude(pk=player.pk).first()
    if holder is not None:
        # Replaying a log that contains a rename into a database that already
        # holds the renamed player lands here, because Player rows are global:
        # the replay rebuilt the old number as a fresh row while the original
        # already carries the new one. That is not a state a replay can
        # reconcile — the two rows are different people as far as the database
        # is concerned — so say so rather than merging them.
        raise ValueError(
            f"Player number {new!r} is already taken (by {holder.name!r}). "
            f"A log containing a number change replays only into a database "
            f"that does not already hold the new number."
        )

    player.player_number = new
    # A number outside the local T- namespace is one the registry issued, so the
    # player is no longer provisional.
    player.is_provisional = new.startswith(TEMP_NUMBER_PREFIX)
    player.save(update_fields=["player_number", "is_provisional"])
    return EventResult(
        payload={"old": old, "new": new},
        tournament=tournament,
        result=player,
    )


@records_event("fixed_pairings_removed")
def remove_fixed_pairings_cmd(tournament, actor, payload):
    """payload: {division, kept: [[round, player1, player2], ...]} — the fixed
    pairings to keep, by player number; all others are removed."""
    from tournaments.fixed_pairings import remove_fixed_pairings
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    kept = {tuple(k) for k in payload.get("kept", [])}
    keep_ids = []
    for fp in division.fixed_pairings.select_related(
        "entrant1__player", "entrant2__player"
    ):
        k1, k2 = sorted([fp.entrant1.key, fp.entrant2.key])
        if (fp.round_number, k1, k2) in kept:
            keep_ids.append(fp.pk)
    error = remove_fixed_pairings(division, keep_ids)
    return EventResult(
        payload=payload, division=division, result=error, record=error is None
    )
