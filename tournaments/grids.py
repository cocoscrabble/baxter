"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import Column, EditGrid, JsonBlobGrid

from .display import display_names, division_labels, label_entrants
from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO
from .models import (
    DivisionSettings,
    Entrant,
    FixedPairing,
    FixedTable,
    Player,
    ResultSlip,
    RoundPairings,
    is_reserved_player_number,
)


def _entrant_values(division):
    """Entrant picker options, disambiguated within the division.

    A picker that offers the same label twice is unusable, so a shared name
    carries its player number here (tournaments/display.py).
    """
    entrants = list(
        division.entrants.select_related("player").order_by("player__name")
    )
    label_entrants(division_labels(division), entrants)
    return [{"id": e.pk, "label": e.display_name} for e in entrants]


def _entrant_key_map(division):
    """{entrant pk -> player number} for portable-payload conversion.

    Portable payloads identify a player by number, not name: they are replayed
    into a fresh database, where two entrants may legitimately share a name and
    a name-keyed row would resolve to whichever of them was found first.
    """
    return {
        e.pk: e.player.player_number
        for e in division.entrants.select_related("player")
    }


def _entrant_pk_by_key(division):
    """{player number -> entrant pk} — the inverse, for replay (from_portable)."""
    return {
        e.player.player_number: e.pk
        for e in division.entrants.select_related("player")
    }


def _fill_absence_scores(rows, division):
    """Default the scores on a row whose opponent is the bye.

    A bye and a forfeit have one score between them — the division's
    ``bye_spread``, to the winning side — so making a director type both was
    the "all fields are required" wall that issue #57 names. A typed score is
    still honoured: TSH lets a one-off forfeit be scored differently from the
    tournament default, and so does this.

    ``winner_started`` is filled in too, and then overruled in ``prepare``: on
    an absence row the bye is the notional starter, so the value is derived
    rather than chosen.
    """
    from tournaments.generate_pairings import absence_spread

    bye = next((e for e in _result_entrants(division) if e.player.is_bye), None)
    if bye is None:
        return rows
    spread = absence_spread(division)
    filled = []
    for row in rows:
        if bye.pk in (row.get("winner"), row.get("loser")):
            row = {
                **row,
                "winner_score": _or_default(row.get("winner_score"), spread),
                "loser_score": _or_default(row.get("loser_score"), 0),
                "winner_started": _or_default(row.get("winner_started"), False),
            }
        filled.append(row)
    return filled


def _or_default(value, default):
    """``default`` for a blank cell; anything the director typed wins."""
    return default if value is None or value == "" else value


def _result_entrants(division):
    """Every entrant a result slip may name — the real field *plus the bye*.

    The results grid is the one grid whose rows legitimately reference the bye
    entrant: ``materialize_byes`` writes a real slip for every bye, and the grid
    loads the division's whole result set. Everywhere else ``division.entrants``
    (the manager that hides the bye) is what is wanted, which is why this is not
    folded into ``_entrant_key_map`` and friends — a fixed pairing against the
    bye is meaningless and must stay unofferable.

    The bye is included only when it already exists. A division that has never
    had an odd round has no bye entrant, and creating one here — on a read —
    would put a competitor-shaped row in a division that never needed it.
    """
    entrants = list(
        Entrant.all_objects.filter(division=division).select_related("player")
    )
    real = sorted(
        (e for e in entrants if not e.player.is_bye), key=lambda e: e.player.name
    )
    # The bye sorts last rather than under "B": it is not a competitor, and a
    # picker that files it among the players invites picking it by accident.
    return real + [e for e in entrants if e.player.is_bye]


def resolve_player(key, name=None, rating=0, wespa_rating=None):
    """The Player with ``key`` (a player number), created if absent.

    Used by replay to rebuild a roster in a fresh database. The number is the
    identity, so a replayed player keeps the number the log recorded — including
    a ``T-`` number, which is portable precisely because it was minted locally.
    ``name`` and ``rating`` are creation data, never lookup keys.

    ``key=None`` falls back to matching on name, for the two name-keyed payloads
    that carry no numbers at all (``entrants_bulk_imported`` and
    ``division_imported``, whose payloads are the historical documents
    themselves). That path mints a fresh ``T-`` number for anyone new.
    """
    from .models import canonical_player_number, next_temp_player_number

    if key:
        player = Player.objects.filter(
            player_number=canonical_player_number(key)
        ).first()
        if player is not None:
            return player
    elif name is not None:
        player = Player.objects.filter(name__iexact=name).first()
        if player is not None:
            return player
    return Player.objects.create(
        name=name or key,
        player_number=key or next_temp_player_number(),
        rating=rating,
        wespa_rating=wespa_rating,
        is_provisional=not key or str(key).startswith("T-"),
    )


class EntrantsGrid(EditGrid):
    model = Entrant
    parent_field = "division"
    related_name = "entrants"
    scope = "entrants"
    dto_class = EntrantDTO
    dom_id = "entrants-table"
    event_type = "entrants_saved"
    js_module = "tournaments/js/edit_entrants.js"  # custom: create-player + import
    template_name = "tournaments/division_entrants_edit.html"
    focus_field = "player"
    # Reconcile on the player: keep each existing entrant (and its pairings /
    # results, which would otherwise cascade away on a wipe) and only apply
    # number changes, adds, and guarded removals.
    key_fields = ("player_id",)
    update_fields = (
        "number", "dropped", "rating", "rating_source",
        "tentative", "paid", "playing_up",
    )
    unique_within_parent = ("number",)  # (division, number) is unique
    columns = [
        # The entrant's number for this tournament — a seeding, derived from
        # the rating by commands.reseed_entrants and shown here, never typed.
        # auto_increment only covers the moment between a new row and the
        # renumber that follows the save.
        Column("number", "Seed", kind="display", width=70, auto_increment=True),
        Column("player", "Player", kind="choice", lookup="players", autocomplete=True),
        # Editing this makes the snapshot manual, server-side in prepare(); the
        # source column beside it is read-only so the two cannot disagree.
        Column("rating", "Rating", kind="number", min=0, width=100),
        Column("source", "Source", kind="display", width=90),
        Column(
            "tentative", "Tent.", kind="choice",
            values={False: "", True: "Tentative"}, value_type="bool",
            new_row=False, width=110,
        ),
        Column(
            "paid", "Paid", kind="choice",
            values={False: "", True: "Paid"}, value_type="bool",
            new_row=False, width=90,
        ),
        Column(
            "playing_up", "Up", kind="choice",
            values={False: "", True: "Playing up"}, value_type="bool",
            new_row=False, width=110,
        ),
        Column(
            "dropped", "Dropped", kind="choice",
            values={False: "", True: "Dropped"}, value_type="bool",
            new_row=False, width=110,
        ),
    ]

    def queryset(self, division):
        return division.entrants.select_related("player").order_by("number")

    def to_portable(self, rows, division):
        """The pk-free payload for the log.

        ``player`` is the number — the identity. Name and both player ratings
        ride along so a replay into a fresh DB can create a missing player
        correctly (pairing seeds off rating).

        The pinned snapshot is read back from the **database**, not from
        ``rows``: it is derived server-side in ``prepare``, so the client's rows
        do not contain it. ``on_saved`` runs after ``persist``, so what is in the
        database is exactly what was written — which is what the log should say.
        """
        players = {
            p.pk: (p.player_number, p.name, p.rating, p.wespa_rating)
            for p in Player.objects.all()
        }
        persisted = {
            e.player_id: e for e in division.entrants.select_related("player")
        }
        portable = []
        for r in rows:
            key, name, rating, wespa = players.get(
                r["player"], (None, None, 0, None)
            )
            entrant = persisted.get(r["player"])
            portable.append(
                {
                    "number": r["number"],
                    "player": key,
                    "name": name,
                    # The player's live CoCo rating, kept under this key exactly
                    # as it always was so older logs replay unchanged. It is
                    # creation data for a player a replay has to invent, not the
                    # entrant's pinned rating — that is "entrant_rating".
                    "rating": rating,
                    "wespa_rating": wespa,
                    "entrant_rating": entrant.rating if entrant else 0,
                    "rating_source": entrant.rating_source if entrant else "",
                    "dropped": r.get("dropped", False),
                    "tentative": r.get("tentative", False),
                    "paid": r.get("paid", False),
                    "playing_up": r.get("playing_up", False),
                }
            )
        return portable


    def from_portable(self, rows, division):
        # A v1 row's "player" is a name and carries no "name" key; a v2 row's is
        # a number. No schema upgrader is registered for this event because the
        # row is self-describing: the distinction is visible right here.
        return [
            {
                "number": r["number"],
                "player": resolve_player(
                    r["player"] if "name" in r else None,
                    r.get("name", r["player"]),
                    r.get("rating", 0),
                    r.get("wespa_rating"),
                ).pk,
                "dropped": r.get("dropped", False),
                # A payload written before these columns existed has none of
                # them, and replays to the same defaults it was recorded with.
                "rating": r.get("entrant_rating"),
                "rating_source": r.get("rating_source", ""),
                "tentative": r.get("tentative", False),
                "paid": r.get("paid", False),
                "playing_up": r.get("playing_up", False),
            }
            for r in rows
        ]

    def serialize_row(self, entrant):
        return {
            "number": entrant.number,
            "player": entrant.player_id,
            "dropped": entrant.dropped,
            "rating": entrant.rating,
            # ``source`` is the display column; ``rating_source`` is the value
            # the portable payload carries. Same thing, two audiences.
            "source": entrant.get_rating_source_display(),
            "rating_source": entrant.rating_source,
            "tentative": entrant.tentative,
            "paid": entrant.paid,
            "playing_up": entrant.playing_up,
        }

    def lookups(self, division):
        # The synthetic Bye player is never a real entrant, so keep it out of the
        # add-entrant picker (and out of the valid-id set below).
        #
        # Scope here is the *whole roster*, not the division: this picker offers
        # every player, so a name has to be judged ambiguous against all of them
        # — including two people who have never yet met in one division.
        players = list(Player.objects.filter(is_bye=False))
        labels = display_names(players)
        return {"players": [
            {
                "id": p.pk,
                "label": labels[p.player_number],
                # Both ratings and the cascade's answer, so a row added in the
                # grid can prefill its snapshot client-side. prepare() re-derives
                # it server-side regardless — the client is never trusted for it.
                "rating": p.rating,
                "wespa_rating": p.wespa_rating,
                "effective_rating": p.effective_rating[0],
            }
            for p in players
        ]}

    def validate_args(self, division):
        return (set(Player.objects.filter(is_bye=False).values_list("pk", flat=True)), set())

    def can_delete(self, entrant):
        # An entrant with pairings or results can't just be removed — deleting
        # it would cascade away those Pairing / ResultSlip rows. Registration-
        # period entrants with no dependents delete normally.
        if (
            entrant.pairings_as_first.exists()
            or entrant.pairings_as_second.exists()
            or entrant.wins.exists()
            or entrant.losses.exists()
        ):
            return (
                f"{entrant.player.name} has pairings or results — cannot be "
                "removed."
            )
        return None

    # Two entrants in one division may share a name. The guard that used to
    # reject that existed because the pairing engine keyed on the display name;
    # it now keys on the player number, so the collision is merely a display
    # problem — which is what the disambiguation rule handles.

    def prepare(self, division, validated):
        # The DTO's rating fields never reach to_db_kwargs; _pin_ratings is the
        # only thing that may set them, so carry them across by hand first.
        prepared, errors = super().prepare(division, validated)
        for row, dto in zip(prepared, validated):
            row.rating = dto.rating
            row.rating_source = dto.rating_source
        if not errors:
            self._pin_ratings(division, prepared)
        return prepared, errors

    def _pin_ratings(self, division, prepared):
        """Decide each row's rating snapshot.

        A ``rating_source`` on the row means a portable payload is being
        replayed: it is restoring a recorded snapshot rather than deciding one,
        so it is honoured verbatim. Without that, a replayed ``(0, "none")``
        entrant would come back ``manual``, because carrying a rating is
        otherwise what ``manual`` means.

        Otherwise, for an **existing** entrant only a rating that actually
        *differs* is a hand-edit — ``Entrant.is_rating_override``, which the
        registration page's edit form asks the same question of.

        For a **new** entrant, any rating supplied is a deliberate override and
        anything else snapshots the cascade. The client is never trusted for the
        derivation itself.
        """
        pinned = {
            e.player_id: (e.rating, e.rating_source)
            for e in division.entrants.all()
        }
        new_ids = [
            row.player_id for row in prepared
            if not row.rating_source and row.player_id not in pinned
        ]
        players = Player.objects.in_bulk(new_ids) if new_ids else {}
        for row in prepared:
            if row.rating_source:
                row.rating = row.rating or 0
                continue
            if row.player_id in pinned:
                current, source = pinned[row.player_id]
                if Entrant.is_rating_override(current, row.rating):
                    row.rating_source = Entrant.MANUAL
                else:
                    row.rating, row.rating_source = current, source
                continue
            if row.rating is not None:
                row.rating_source = Entrant.MANUAL
                continue
            player = players.get(row.player_id)
            if player is not None:
                row.rating, row.rating_source = player.effective_rating

    def _roster_signature(self, division):
        # (player, dropped, rating) per real entrant — what the pairing engine
        # keys off. A pure renumber doesn't change it (numbers don't affect
        # pairing), and neither do the registration flags.
        #
        # The rating belongs here now that the entrant pins it and this grid can
        # edit it: a director who corrects a rating and then publishes would
        # otherwise get a round paired off the *old* one, silently. The fuzzer
        # found exactly that — a bye handed to the wrong player.
        return frozenset(
            division.entrants.values_list("player_id", "dropped", "rating")
        )

    def persist(self, division, prepared):
        before = self._roster_signature(division)
        super().persist(division, prepared)
        if self._roster_signature(division) != before:
            # Roster membership or a dropped flag changed, so any draft pairings
            # are stale. Drop them (a plain DELETE — safe inside the save
            # transaction); the lazy _autogenerate_pairable_rounds re-pairs on
            # the next Pair Rounds render. Published/finished rounds are left
            # alone (unpublish handles those). Regenerating here is deliberately
            # avoided: a PairingError would poison the whole grid save.
            division.round_pairings_set.filter(
                status=RoundPairings.DRAFT
            ).delete()


class FixedPairingsGrid(EditGrid):
    model = FixedPairing
    parent_field = "division"
    related_name = "fixed_pairings"
    scope = "fixed_pairings"
    dto_class = FixedPairingDTO
    dom_id = "fixed-pairings-table"
    event_type = "fixed_pairings_saved"
    template_name = "tournaments/division_fixed_pairings_edit.html"
    focus_field = "round_number"
    columns = [
        Column("round_number", "Round", kind="number", min=1, width=100),
        Column("entrant1", "Player 1", kind="choice", lookup="entrantValues", autocomplete=True),
        Column("entrant2", "Player 2", kind="choice", lookup="entrantValues", autocomplete=True),
    ]

    def to_portable(self, rows, division):
        keys = _entrant_key_map(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant1": keys.get(r["entrant1"]),
                "entrant2": keys.get(r["entrant2"]),
            }
            for r in rows
        ]

    def from_portable(self, rows, division):
        pks = _entrant_pk_by_key(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant1": pks.get(r["entrant1"]),
                "entrant2": pks.get(r["entrant2"]),
            }
            for r in rows
        ]

    def serialize_row(self, fp):
        return {
            "round_number": fp.round_number,
            "entrant1": fp.entrant1_id,
            "entrant2": fp.entrant2_id,
        }

    def lookups(self, division):
        return {"entrantValues": _entrant_values(division)}

    def validate_args(self, division):
        return (set(division.entrants.values_list("pk", flat=True)), {})


class FixedTablesGrid(EditGrid):
    model = FixedTable
    parent_field = "division"
    related_name = "fixed_tables"
    scope = "fixed_tables"
    dto_class = FixedTableDTO
    dom_id = "fixed-tables-table"
    event_type = "fixed_tables_saved"
    template_name = "tournaments/division_fixed_tables_edit.html"
    focus_field = "round_number"
    columns = [
        Column("round_number", "Round", kind="choice", lookup="roundValues", width=100, new_row=-1),
        Column("entrant", "Player", kind="choice", lookup="entrantValues", autocomplete=True, min_width=200),
        Column("table_label", "Table", kind="text", value_type="str", width=100),
    ]

    def to_portable(self, rows, division):
        keys = _entrant_key_map(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant": keys.get(r["entrant"]),
                "table_label": r["table_label"],
            }
            for r in rows
        ]

    def from_portable(self, rows, division):
        pks = _entrant_pk_by_key(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant": pks.get(r["entrant"]),
                "table_label": r["table_label"],
            }
            for r in rows
        ]

    def serialize_row(self, ft):
        return {
            "round_number": ft.round_number,
            "entrant": ft.entrant_id,
            "table_label": ft.table_label,
        }

    def lookups(self, division):
        round_numbers = division.configured_round_numbers()
        round_values = [{"id": -1, "label": "All"}] + [
            {"id": r, "label": str(r)} for r in round_numbers
        ]
        return {
            "entrantValues": _entrant_values(division),
            "roundValues": round_values,
        }

    def validate_args(self, division):
        return (set(division.entrants.values_list("pk", flat=True)), {})


class ResultsGrid(EditGrid):
    model = ResultSlip
    parent_field = "division"
    related_name = "result_slips"
    scope = "results"
    dto_class = ResultSlipDTO
    dom_id = "results-table"
    event_type = "results_saved"
    template_name = "tournaments/division_edit_results.html"
    # Reconcile on the pairing so an edited row keeps its pk and, crucially, its
    # created_at (auto_now_add) — the results export uses it as submitted_on.
    # A row whose match changed resolves to a different pairing, i.e. delete +
    # create, which is correct.
    key_fields = ("pairing_id",)
    update_fields = (
        "round",
        "winner_id",
        "winner_score",
        "loser_id",
        "loser_score",
        "winner_started",
    )
    columns = [
        Column("round", "Round", kind="number", min=1, width=100, auto_increment=True),
        Column("winner", "Winner", kind="choice", lookup="entrants"),
        Column("winner_score", "W Score", kind="number", min=0, width=120),
        Column("loser", "Opponent", kind="choice", lookup="entrants"),
        Column("loser_score", "Opp Score", kind="number", min=0, width=130),
        Column("winner_started", "Started", kind="choice",
               values={True: "Winner", False: "Opponent"}, width=120,
               value_type="bool", new_row=True),
    ]

    def queryset(self, division):
        return division.result_slips.select_related("winner", "loser").order_by("round", "pk")

    def to_portable(self, rows, division):
        # Bye-inclusive, unlike the other grids: a bye row's opponent *is* the
        # bye entrant, and mapping it to None would record an event payload with
        # no opponent in it — a slip replay could not rebuild.
        keys = {e.pk: e.player.player_number for e in _result_entrants(division)}
        return [
            {
                "round": r["round"],
                "winner": keys.get(r["winner"]),
                "winner_score": r["winner_score"],
                "loser": keys.get(r["loser"]),
                "loser_score": r["loser_score"],
                "winner_started": r["winner_started"],
            }
            for r in rows
        ]

    def from_portable(self, rows, division):
        pks = {e.player.player_number: e.pk for e in _result_entrants(division)}

        def resolve(key):
            # A replayed payload can name the bye before this division has a bye
            # entrant, so it is created on demand here — the same lazy resolve
            # ``generate_pairings.resolve_entrant`` does, and idempotent.
            if is_reserved_player_number(key):
                return division.bye_entrant().pk
            return pks.get(key)

        return [
            {**r, "winner": resolve(r["winner"]), "loser": resolve(r["loser"])}
            for r in rows
        ]

    def serialize_row(self, slip):
        return slip.to_dict()

    def lookups(self, division):
        # Includes the bye, or a bye row loads with an empty Opponent cell: the
        # cell's value is an entrant pk the picker cannot resolve to a label, so
        # it renders blank and the row can no longer be saved.
        return {
            "entrants": [
                {"id": e.pk, "label": e.player.name} for e in _result_entrants(division)
            ]
        }

    def validate_args(self, division):
        return ({e.pk for e in _result_entrants(division)},)

    def validate(self, rows, division):
        return super().validate(_fill_absence_scores(rows, division), division)

    def prepare(self, division, validated):
        # Every row must correspond to an existing Pairing — results for
        # unpaired matches are not allowed via this flow.
        pairing_lookup = division.pairings_by_round_pair()
        bye_pk = next(
            (e.pk for e in _result_entrants(division) if e.player.is_bye), None
        )
        round_containers = {
            rp.round: rp for rp in division.round_pairings_set.all()
        }
        # (round, entrant pk) for everyone who already has a game that round.
        # A player has at most one, which is what stops a hand-entered bye from
        # quietly becoming a *second* game for somebody.
        already_playing = {
            (round_num, pk)
            for round_num, first, second in division.pairings.values_list(
                "round", "first_id", "second_id"
            )
            for pk in (first, second)
        }
        instances, errors = [], []
        for i, slip in enumerate(validated):
            pairing = pairing_lookup.get((slip.round, frozenset({slip.winner, slip.loser})))
            if pairing is None and bye_pk not in (slip.winner, slip.loser):
                errors.append(
                    f"Row {i + 1}: no pairing for that match in round {slip.round}."
                )
                continue
            kwargs = slip.to_db_kwargs()
            if bye_pk in (slip.winner, slip.loser):
                # The Started column is not a free choice on a bye or forfeit
                # row: the bye is the notional starter, so the real player is
                # charged no start. ``winner_started`` orients the pairing the
                # engine replays into its ledger
                # (``Pairings.add_result_slip``), so a director ticking
                # "Winner" here would charge them a start they never took — and
                # ``starts.correct_result_starts`` skips bye pairings, so
                # nothing downstream would put it back. Derive it instead, as
                # every other write path does.
                kwargs["winner_started"] = slip.winner == bye_pk
            if pairing is None and bye_pk in (slip.winner, slip.loser):
                # A bye or forfeit entered by hand for a round that never paired
                # it. The row *is* the pairing — there is no game it stands in
                # for — so one is built, but in ``persist``: ``prepare`` runs
                # before the save transaction, and a pairing created here would
                # outlive an error further down the list.
                rp = round_containers.get(slip.round)
                if rp is None:
                    errors.append(
                        f"Row {i + 1}: round {slip.round} has no pairings yet, "
                        "so there is nothing to record a bye or forfeit against."
                    )
                    continue
                real_pk = slip.loser if slip.winner == bye_pk else slip.winner
                if (slip.round, real_pk) in already_playing:
                    # They are already in a game that round, so this row would
                    # be their second. Moving a bye from one player to another
                    # is a change to the printed board, not to the results —
                    # unpublish the round, or forfeit the game they do have.
                    errors.append(
                        f"Row {i + 1}: that player already has a game in round "
                        f"{slip.round}. Who gets a bye is set when the round is "
                        "paired, not here."
                    )
                    continue
                instance = ResultSlip(division=division, **kwargs)
                instance._absence_round_pairings = rp
                instance._absence_is_forfeit = slip.winner == bye_pk
                instances.append(instance)
                continue
            instances.append(
                ResultSlip(division=division, pairing=pairing, **kwargs)
            )
        if errors:
            return [], errors
        # The grid replaces the division's whole result set, so it *is* the
        # prospective set the bracket would be derived from.
        from tournaments.playoff import conflicts_for_results, playoff_for

        playoff = playoff_for(division)
        if playoff is not None:
            from tournaments.pairing.base import ResultSlipData

            keys = _entrant_key_map(division)
            prospective = [
                ResultSlipData(
                    round=r.round,
                    winner_key=keys.get(r.winner_id),
                    loser_key=keys.get(r.loser_id),
                    winner_score=r.winner_score,
                    loser_score=r.loser_score,
                    winner_started=r.winner_started,
                )
                for r in instances
            ]
            errors = conflicts_for_results(playoff.config(), prospective)
            if errors:
                return [], errors
        return instances, self.reconcile_errors(division, instances)

    def persist(self, division, prepared):
        """Build any hand-entered absence row's pairing, then reconcile.

        Here rather than in ``prepare`` because this runs inside the save
        transaction: a pairing created during validation would survive an error
        raised further down, leaving a bye-shaped game with no result.
        """
        from tournaments.generate_pairings import absence_row

        for slip in prepared:
            rp = getattr(slip, "_absence_round_pairings", None)
            if rp is None:
                continue
            real = slip.winner if slip.loser_id == division.bye_entrant().pk else slip.loser
            slip.pairing = absence_row(
                division, rp, real, forfeit=slip._absence_is_forfeit
            )
        super().persist(division, prepared)

    def after_save(self, division):
        # Recreating the slips can change which rounds have results; refresh the
        # status of every round (update_status is idempotent).
        for rp in division.round_pairings_set.all():
            rp.update_status()
        # A result can clinch a series, which retires its remaining games.
        from tournaments.playoff import refresh_after_results

        refresh_after_results(division)


class BoardTableMapGrid(JsonBlobGrid):
    blob_model = DivisionSettings
    blob_fk = "division"
    blob_field = "board_table_map"
    scope = "board_table_map"
    dom_id = "board-table-map-table"
    event_type = "board_tables_saved"  # rows are label/board/table — no pks; default to_portable
    js_module = "tournaments/js/edit_board_table_map.js"  # custom: generate button
    template_name = "tournaments/division_board_table_map_edit.html"
    focus_field = "label"
    columns = [
        Column("label", "Table", kind="text", value_type="str", width=120),
        # Order index: kept in the row data (groups boards on a shared double
        # table, sorts pairings) but not shown to organizers.
        Column("table", "Order", kind="number", min=1, hidden=True),
        Column("board", "Board", kind="number", min=1, width=120, auto_increment=True),
    ]

    def validate(self, rows, division):
        errors = []
        seen_boards = set()
        validated = []
        for i, row in enumerate(rows):
            try:
                board = int(row["board"])
                table = int(row["table"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"Row {i + 1}: board and order must be integers.")
                continue
            if board < 1 or table < 1:
                errors.append(f"Row {i + 1}: board and order must be positive.")
                continue
            if board in seen_boards:
                errors.append(f"Row {i + 1}: duplicate board {board}.")
                continue
            label = str(row.get("label") or "").strip() or str(table)
            seen_boards.add(board)
            validated.append({"board": board, "table": table, "label": label})
        validated.sort(key=lambda r: r["board"])
        return validated, errors
