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
from tournaments.models import Division, DivisionSettings, ResultSlip, Tournament

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
# Fixed pairings (thin adapters; pk args translated to/from player names)
# ---------------------------------------------------------------------------


def _entrant(division, name):
    return division.entrants.get(player__name=name)


@records_event("fixed_pairing_added")
def add_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, name1, name2}."""
    from tournaments.fixed_pairings import add_fixed_pairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["name1"])
    e2 = _entrant(division, payload["name2"])
    ok, error = add_fixed_pairing(division, payload["round"], e1.pk, e2.pk)
    return EventResult(
        payload=payload, division=division, result=(ok, error), record=ok
    )


@records_event("fixed_pairing_removed")
def remove_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, name1, name2}."""
    from tournaments.fixed_pairings import remove_fixed_pairing
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["name1"])
    e2 = _entrant(division, payload["name2"])
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


def _find_pairing(division, round_number, first_name, second_name):
    names = {first_name, second_name}
    for p in division.pairings.filter(round=round_number).select_related(
        "first__player", "second__player"
    ):
        if {p.first.player.name, p.second.player.name} == names:
            return p
    return None


def _write_result(division, pairing, payload, instance):
    winner = (
        pairing.first
        if pairing.first.player.name == payload["winner_name"]
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
    # it (a replay must see the same status).
    if pairing.round_pairings_id:
        pairing.round_pairings.update_status()
    return slip


@records_event("result_added")
def add_result(tournament, actor, payload):
    """payload: {division, round, first_name, second_name, winner_name,
    winner_score, loser_score}. Records a new game result."""
    division = _division(tournament, payload["division"])
    pairing = _find_pairing(
        division, payload["round"], payload["first_name"], payload["second_name"]
    )
    slip = _write_result(division, pairing, payload, instance=None)
    return EventResult(payload=payload, division=division, result=slip)


@records_event("result_edited")
def edit_result(tournament, actor, payload):
    """payload as add_result. Updates the pairing's existing slip in place."""
    division = _division(tournament, payload["division"])
    pairing = _find_pairing(
        division, payload["round"], payload["first_name"], payload["second_name"]
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
    from tournaments.pairing.round_pairing import blocks_to_round_pairings

    division = _division(tournament, payload["division"])
    blocks = payload["blocks"]
    round_pairings = [rp.to_dict() for rp in blocks_to_round_pairings(blocks)]
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    settings_obj.pairing_blocks = blocks
    settings_obj.round_pairings = round_pairings
    settings_obj.save(update_fields=["pairing_blocks", "round_pairings"])
    return EventResult(payload=payload, division=division, result=settings_obj)


@records_event("entrants_bulk_imported")
def bulk_import_entrants(tournament, actor, payload):
    """payload: {division, csv} — the raw CSV text. New players are created by
    name; a replay resolves/creates them the same way."""
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
    in the parser) so a replay reproduces it. Returns an import summary."""
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
        player = resolve_player(e["player"], e.get("rating", 0))
        (matched if exists else created).append(player.name)
        ent_by_name[e["player"]] = Entrant.objects.create(
            division=division, player=player, number=e["number"]
        )

    def entrant(name):
        ent = ent_by_name.get(name)
        if ent is None:  # a result names a non-entrant — add them at the bottom
            ent = Entrant.objects.create(
                division=division, player=resolve_player(name),
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
        "first_name": slip.pairing.first.player.name,
        "second_name": slip.pairing.second.player.name,
        "winner_name": slip.winner.player.name,
        "loser_name": slip.loser.player.name,
        "winner_score": slip.winner_score,
        "loser_score": slip.loser_score,
        "winner_started": slip.winner_started,
    }


def _apply_sim_result(division, round_num, r):
    pairing = _find_pairing(division, round_num, r["first_name"], r["second_name"])
    slip = ResultSlip.objects.create(
        division=division,
        round=round_num,
        pairing=pairing,
        winner=_entrant(division, r["winner_name"]),
        winner_score=r["winner_score"],
        loser=_entrant(division, r["loser_name"]),
        loser_score=r["loser_score"],
        winner_started=r["winner_started"],
    )
    if pairing is not None and pairing.round_pairings_id:
        pairing.round_pairings.update_status()
    return slip


@records_event("match_simulated")
def simulate_match_cmd(tournament, actor, payload):
    """payload: {division, round, first_name, second_name}. The generated scores
    are recorded (the RNG is unseeded, so a replay applies them, not re-rolls)."""
    from tournaments.match_simulation import simulate_match

    division = _division(tournament, payload["division"])
    if "result" in payload:  # replay: apply the recorded result
        slip = _apply_sim_result(division, payload["round"], payload["result"])
        return EventResult(payload=payload, division=division, result=slip)
    first = _entrant(division, payload["first_name"])
    second = _entrant(division, payload["second_name"])
    slip = simulate_match(division, payload["round"], first, second)
    if slip.pairing_id and slip.pairing.round_pairings_id:
        slip.pairing.round_pairings.update_status()
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


@records_event("fixed_pairings_removed")
def remove_fixed_pairings_cmd(tournament, actor, payload):
    """payload: {division, kept: [[round, name1, name2], ...]} — the fixed
    pairings to keep; all others are removed."""
    from tournaments.fixed_pairings import remove_fixed_pairings
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    kept = {tuple(k) for k in payload.get("kept", [])}
    keep_ids = []
    for fp in division.fixed_pairings.select_related(
        "entrant1__player", "entrant2__player"
    ):
        n1, n2 = sorted([fp.entrant1.player.name, fp.entrant2.player.name])
        if (fp.round_number, n1, n2) in kept:
            keep_ids.append(fp.pk)
    error = remove_fixed_pairings(division, keep_ids)
    return EventResult(
        payload=payload, division=division, result=error, record=error is None
    )
