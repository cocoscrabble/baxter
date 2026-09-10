"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import Column, EditGrid, JsonBlobGrid

from .display import display_names, division_labels, label_entrants
from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO
from .models import (
    DivisionSettings,
    Entrant,
    FixedPairing,
    FixedTable,
    Pairing,
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


def _double_booking_errors(validated, names, bye_pk):
    """Rows that put one player in two games in the same round.

    The one invariant the results grid enforces across rows, and the whole
    condition on entering a result for a match nobody paired: a round is a set
    of simultaneous games, so a player is in at most one of them. Everything
    else about a hand-entered row — which board it lands on, what it dissolves —
    follows from the rows being consistent in this sense.

    The bye is exempt. It is every absent player's opponent, so it legitimately
    appears once per bye and forfeit in the round.
    """
    errors = []
    seen = {}
    for i, slip in enumerate(validated):
        for pk in (slip.winner, slip.loser):
            if pk == bye_pk:
                continue
            first = seen.setdefault((slip.round, pk), i)
            if first != i:
                errors.append(
                    f"Row {i + 1}: {names.get(pk, 'that player')} is in two games "
                    f"in round {slip.round} (also row {first + 1})."
                )
    return errors


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
        # Widths leave room for the header plus Tabulator's sort arrow, which
        # eats about 20px — too tight and the title truncates to "O…".
        Column("tentative", "Tent.", kind="flag", value_type="bool",
               new_row=False, width=90),
        Column("paid", "Paid", kind="flag", value_type="bool",
               new_row=False, width=85),
        Column("playing_up", "Up", kind="flag", value_type="bool",
               new_row=False, width=75),
        # Shown, not set. Withdrawing is more than a flag: it also has to
        # resolve the round already on the boards — dissolve the printed game,
        # give the opponent their bye, record the absence per the division's
        # rule — which is what ``forfeits.withdraw_entrant`` does and this grid
        # does not. Ticking it here used to set the flag alone and leave a
        # printed game nobody could play. Use Withdraw on the entrants page.
        Column("dropped", "Out", kind="flag", value_type="bool",
               new_row=False, width=85, editable=False),
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
            # The rows' players, not the whole roster: a save reads this twice
            # (once for the before snapshot the delta is diffed against), and
            # the roster is the central database's, thousands of rows deep.
            for p in Player.objects.filter(pk__in={r["player"] for r in rows})
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


    def portable_key(self, row):
        """The player. An entrant is one person's registration in a division, so
        the row is theirs however its number or flags change."""
        return row.get("player")

    portable_player_fields = ("player",)
    # "name" rides along for a replay into a fresh database; in the log it is
    # the label, not a column of its own.
    portable_identity_fields = ("player", "name")

    def portable_label(self, row, names):
        return names.get(row.get("player")) or row.get("name") or row.get("player")

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

    def portable_key(self, row):
        """The round and the pair, in either order — the fixture itself."""
        if row.get("entrant1") is None or row.get("entrant2") is None:
            return None
        return (row["round_number"], *sorted([row["entrant1"], row["entrant2"]]))

    portable_player_fields = ("entrant1", "entrant2")
    portable_identity_fields = ("round_number", "entrant1", "entrant2")

    def portable_label(self, row, names):
        first = names.get(row.get("entrant1"), row.get("entrant1"))
        second = names.get(row.get("entrant2"), row.get("entrant2"))
        return f"Round {row.get('round_number')}: {first} vs {second}"

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

    def portable_key(self, row):
        """One player's pinned table for one round; the label is the value."""
        if row.get("entrant") is None:
            return None
        return (row["round_number"], row["entrant"])

    portable_player_fields = ("entrant",)
    portable_identity_fields = ("round_number", "entrant")

    def portable_label(self, row, names):
        who = names.get(row.get("entrant"), row.get("entrant"))
        return f"Round {row.get('round_number')}: {who}"

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
    # Reconcile on the match — the round and the two entrants — so an edited row
    # keeps its pk and, crucially, its created_at (auto_now_add), which the
    # results export uses as submitted_on. A row whose match changed resolves to
    # a different key, i.e. delete + create, which is correct.
    #
    # Not on the pairing, which is what this keyed on while every row had to have
    # one: a hand-entered row has no pairing until ``persist`` builds it, so all
    # of them would key on None and collide with each other. The match is what
    # the director is editing in any case; the pairing follows from it, which is
    # why ``pairing_id`` is an updatable field rather than the identity.
    key_fields = ("round", "winner_id", "loser_id")
    update_fields = (
        "round",
        "winner_id",
        "winner_score",
        "loser_id",
        "loser_score",
        "winner_started",
        "pairing_id",
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

    def portable_key(self, row):
        """The match, the same identity ``_row_key`` uses in pks.

        Order-free, so a director correcting who won edits the row that is there
        rather than deleting one game and adding another — which is what the log
        should say happened.
        """
        if row.get("winner") is None or row.get("loser") is None:
            return None
        return (row["round"], *sorted([row["winner"], row["loser"]]))

    portable_player_fields = ("winner", "loser")
    # Winner and loser are the label *and* a pair of fields that can swap when a
    # director corrects who won, so they stay listed: the change is the point.
    portable_identity_fields = ("round", "winner", "loser")

    def portable_label(self, row, names):
        winner = names.get(row.get("winner"), row.get("winner"))
        loser = names.get(row.get("loser"), row.get("loser"))
        return f"Round {row.get('round')}: {winner} beat {loser}"

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
        """Build the slips, and the pairings for any match nobody paired.

        A row does not have to name a pairing the pairer generated. Directors
        correct the board as well as the scores — the case this exists for is
        reversing a forfeit, where the absence rows come out and the game the
        players actually sat down and played goes in — and a game that was
        played is a fact about the tournament whether or not it was scheduled.

        What has to hold is the thing a round *is*: when the save has landed, no
        player is in two games in the same round. That is checked across the
        rows here; a row naming a match with no pairing gets one built for it in
        ``persist``, and the boards it collides with — which by then can carry no
        result, or the check above would have rejected the payload — are
        dissolved there too.
        """
        pairing_lookup = division.pairings_by_round_pair()
        entrants = _result_entrants(division)
        bye_pk = next((e.pk for e in entrants if e.player.is_bye), None)
        names = {e.pk: e.player.name for e in entrants}
        errors = _double_booking_errors(validated, names, bye_pk)
        if errors:
            return [], errors
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
        # The same, for playoff games only. A playoff game is derived from the
        # bracket rather than recorded, so dissolving one does not stick: the
        # next regeneration builds it again and the player is in two games after
        # all. A hand-entered row that would supersede one is refused instead.
        in_playoff = {
            (round_num, pk)
            for round_num, first, second in division.pairings.filter(
                series__isnull=False
            ).values_list("round", "first_id", "second_id")
            for pk in (first, second)
        }
        instances, errors = [], []
        for i, slip in enumerate(validated):
            pairing = pairing_lookup.get(
                (slip.round, frozenset({slip.winner, slip.loser}))
            )
            kwargs = slip.to_db_kwargs()
            is_absence = bye_pk in (slip.winner, slip.loser)
            if is_absence:
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
            if pairing is not None:
                instances.append(
                    ResultSlip(division=division, pairing=pairing, **kwargs)
                )
                continue
            # A match with no pairing: the row *is* the pairing, and one is built
            # for it in ``persist`` — ``prepare`` runs before the save
            # transaction, and a pairing created here would outlive an error
            # raised further down the list.
            rp = round_containers.get(slip.round)
            if rp is None:
                if is_absence:
                    errors.append(
                        f"Row {i + 1}: round {slip.round} has no pairings yet, "
                        "so there is nothing to record a bye or forfeit against."
                    )
                else:
                    errors.append(
                        f"Row {i + 1}: round {slip.round} has not been paired, "
                        "so there is no round to record that game in."
                    )
                continue
            real = [pk for pk in (slip.winner, slip.loser) if pk != bye_pk]
            if is_absence and (slip.round, real[0]) in already_playing:
                # They are already in a game that round, so this row would be
                # their second. Moving a bye from one player to another is a
                # change to the printed board, not to the results — unpublish
                # the round, or forfeit the game they do have.
                errors.append(
                    f"Row {i + 1}: that player already has a game in round "
                    f"{slip.round}. Who gets a bye is set when the round is "
                    "paired, not here."
                )
                continue
            clashes = [pk for pk in real if (slip.round, pk) in in_playoff]
            if clashes:
                errors.append(
                    f"Row {i + 1}: {names.get(clashes[0], 'that player')} has a "
                    f"playoff game in round {slip.round}, and the bracket "
                    "decides those — this row would be a second game."
                )
                continue
            instance = ResultSlip(division=division, **kwargs)
            instance._new_pairing_in = rp
            instances.append(instance)
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
        """Build the pairing for every row that has none, dissolve whatever that
        supersedes, then reconcile.

        Here rather than in ``prepare`` because this runs inside the save
        transaction: a pairing created during validation would survive an error
        raised further down, leaving a game with no result.
        """
        from tournaments.generate_pairings import absence_row

        bye_pk = next(
            (e.pk for e in _result_entrants(division) if e.player.is_bye), None
        )
        unpaired = [s for s in prepared if getattr(s, "_new_pairing_in", None)]
        self._dissolve_superseded(division, prepared, unpaired, bye_pk)
        tables = self._last_tables(division)
        for slip in unpaired:
            rp = slip._new_pairing_in
            if bye_pk in (slip.winner_id, slip.loser_id):
                real = slip.winner if slip.loser_id == bye_pk else slip.loser
                slip.pairing = absence_row(
                    division, rp, real, forfeit=slip.winner_id == bye_pk
                )
                continue
            # The board this creates has to agree with the row about who went
            # first. It counts as published from the moment it exists, and a
            # published board owns the start (``tournaments/starts.py``): keyed
            # the other way round it would have the result rewritten to charge a
            # start nobody took.
            first, second = (
                (slip.winner, slip.loser)
                if slip.winner_started
                else (slip.loser, slip.winner)
            )
            tables[slip.round] = tables.get(slip.round, 0) + 1
            slip.pairing = Pairing.objects.create(
                division=division,
                round=slip.round,
                round_pairings=rp,
                first=first,
                second=second,
                table=tables[slip.round],
            )
        for slip in prepared:
            # An existing bye-shaped row whose direction the director changed:
            # the flag follows the score, or the board would keep saying "bye"
            # over a forfeit's result. This is the one way to convert one into
            # the other by hand — the case being both players of a dissolved
            # game withdrawing, where whether they both forfeit is a judgement
            # the director makes.
            pairing = slip.pairing
            if pairing is None or bye_pk not in (pairing.first_id, pairing.second_id):
                continue
            forfeit = slip.winner_id == bye_pk
            if pairing.forfeit != forfeit:
                pairing.forfeit = forfeit
                pairing.save(update_fields=["forfeit"])
        super().persist(division, prepared)

    def _dissolve_superseded(self, division, prepared, unpaired, bye_pk):
        """Delete the boards the hand-entered rows replace.

        A player named in a row with no pairing may already have one for that
        round — the forfeit being reversed, or the game they were scheduled for
        and did not play. Once this save lands it carries no result (``prepare``
        rejects a player with two results in a round), so it is a board that was
        never played, and leaving it would put the player in two games at once.

        Their *opponent* is left with no game that round, which is the honest
        state rather than something to invent a bye for: the director says what
        that player did by entering their row too.
        """
        claimed = {
            (slip.round, pk)
            for slip in unpaired
            for pk in (slip.winner_id, slip.loser_id)
            if pk != bye_pk
        }
        if not claimed:
            return
        kept = {
            (slip.round, frozenset({slip.winner_id, slip.loser_id}))
            for slip in prepared
        }
        for pairing in division.pairings.filter(
            round__in={round_num for round_num, _ in claimed}
        ):
            match = (pairing.round, frozenset({pairing.first_id, pairing.second_id}))
            if match in kept:
                continue
            if any(
                (pairing.round, pk) in claimed
                for pk in (pairing.first_id, pairing.second_id)
            ):
                pairing.delete()

    def _last_tables(self, division):
        """{round -> highest table number in use}, so a hand-entered game lands
        after the boards that were printed rather than on top of one."""
        from django.db.models import Max

        return {
            row["round"]: row["top"] or 0
            for row in division.pairings.values("round").annotate(top=Max("table"))
        }

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


# Which grid a grid-save event belongs to. Here rather than in ``replay`` because
# it has two readers now: replay drives the grid to apply the event, and the
# activity page asks it how to read the event's payload back to a human.
GRID_BY_EVENT = {
    "entrants_saved": EntrantsGrid(),
    "results_saved": ResultsGrid(),
    "fixed_pairings_saved": FixedPairingsGrid(),
    "fixed_tables_saved": FixedTablesGrid(),
    "board_tables_saved": BoardTableMapGrid(),
}
