"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import Column, EditGrid, JsonBlobGrid

from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO
from .models import (
    DivisionSettings,
    Entrant,
    FixedPairing,
    FixedTable,
    Player,
    ResultSlip,
    RoundPairings,
)


def _entrant_values(division):
    entrants = division.entrants.select_related("player").order_by("player__name")
    return [{"id": e.pk, "label": e.player.name} for e in entrants]


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


def resolve_player(key, name=None, rating=0):
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
    update_fields = ("number", "dropped")
    unique_within_parent = ("number",)  # (division, number) is unique
    columns = [
        Column("number", "#", kind="display", width=60, auto_increment=True),
        Column("player", "Player", kind="choice", lookup="players", autocomplete=True),
        Column(
            "dropped", "Dropped", kind="choice",
            values={False: "", True: "Dropped"}, value_type="bool",
            new_row=False, width=110,
        ),
    ]

    def queryset(self, division):
        return division.entrants.select_related("player").order_by("number")

    def to_portable(self, rows, division):
        # ``player`` is the number — the identity. Name and rating ride along so
        # a replay into a fresh DB can create a missing player correctly
        # (pairing seeds off rating).
        players = {
            p.pk: (p.player_number, p.name, p.rating) for p in Player.objects.all()
        }
        portable = []
        for r in rows:
            key, name, rating = players.get(r["player"], (None, None, 0))
            portable.append(
                {
                    "number": r["number"],
                    "player": key,
                    "name": name,
                    "rating": rating,
                    "dropped": r.get("dropped", False),
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
                ).pk,
                "dropped": r.get("dropped", False),
            }
            for r in rows
        ]

    def serialize_row(self, entrant):
        return {
            "number": entrant.number,
            "player": entrant.player_id,
            "dropped": entrant.dropped,
        }

    def lookups(self, division):
        # The synthetic Bye player is never a real entrant, so keep it out of the
        # add-entrant picker (and out of the valid-id set below).
        return {"players": [
            {"id": p.pk, "label": p.name, "rating": p.rating}
            for p in Player.objects.filter(is_bye=False)
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

    def prepare(self, division, validated):
        prepared, errors = super().prepare(division, validated)
        if errors:
            return prepared, errors
        return prepared, self._duplicate_name_errors(prepared)

    def _duplicate_name_errors(self, prepared):
        # The pairing engine keys entrants by display name, so two *different*
        # players sharing a name (case-insensitively) in one division would
        # silently corrupt entrant_by_name. Player names aren't DB-unique
        # (registry sync can introduce collisions), so guard here.
        player_ids = [inst.player_id for inst in prepared]
        names = dict(
            Player.objects.filter(pk__in=player_ids).values_list("pk", "name")
        )
        seen, reported, errors = {}, set(), []
        for pid in player_ids:
            name = names.get(pid, "")
            key = name.casefold()
            if key in seen and seen[key] != pid and key not in reported:
                errors.append(
                    f"Two different players are both named “{name}” — entrant "
                    "names must be unique within a division."
                )
                reported.add(key)
            seen.setdefault(key, pid)
        return errors

    def _roster_signature(self, division):
        # (player, dropped) per real entrant — what the pairing engine keys off.
        # A pure renumber doesn't change it (numbers don't affect pairing).
        return frozenset(division.entrants.values_list("player_id", "dropped"))

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
        keys = _entrant_key_map(division)
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
        pks = _entrant_pk_by_key(division)
        return [
            {**r, "winner": pks.get(r["winner"]), "loser": pks.get(r["loser"])}
            for r in rows
        ]

    def serialize_row(self, slip):
        return slip.to_dict()

    def lookups(self, division):
        entrants = division.entrants.select_related("player").order_by("player__name")
        return {"entrants": [{"id": e.pk, "label": e.player.name} for e in entrants]}

    def validate_args(self, division):
        return (set(division.entrants.values_list("pk", flat=True)),)

    def prepare(self, division, validated):
        # Every row must correspond to an existing Pairing — results for
        # unpaired matches are not allowed via this flow.
        pairing_lookup = division.pairings_by_round_pair()
        instances, errors = [], []
        for i, slip in enumerate(validated):
            pairing = pairing_lookup.get((slip.round, frozenset({slip.winner, slip.loser})))
            if pairing is None:
                errors.append(
                    f"Row {i + 1}: no pairing for that match in round {slip.round}."
                )
                continue
            instances.append(
                ResultSlip(division=division, pairing=pairing, **slip.to_db_kwargs())
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
