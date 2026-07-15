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


def _entrant_name_map(division):
    """{entrant pk -> player name} for portable-payload conversion."""
    return {
        e.pk: e.player.name
        for e in division.entrants.select_related("player")
    }


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
        # Carry name + rating so a replay into a fresh DB can create a missing
        # player with the right rating (pairing seeds off rating).
        players = {
            p.pk: (p.name, p.rating) for p in Player.objects.all()
        }
        portable = []
        for r in rows:
            name, rating = players.get(r["player"], (None, 0))
            portable.append(
                {
                    "number": r["number"],
                    "player": name,
                    "rating": rating,
                    "dropped": r.get("dropped", False),
                }
            )
        return portable

    def serialize_row(self, entrant):
        return {
            "number": entrant.number,
            "player": entrant.player_id,
            "dropped": entrant.dropped,
        }

    def lookups(self, division):
        return {"players": [
            {"id": p.pk, "label": p.name, "rating": p.rating}
            for p in Player.objects.all()
        ]}

    def validate_args(self, division):
        return (set(Player.objects.values_list("pk", flat=True)), set())

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
        names = _entrant_name_map(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant1": names.get(r["entrant1"]),
                "entrant2": names.get(r["entrant2"]),
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
        names = _entrant_name_map(division)
        return [
            {
                "round_number": r["round_number"],
                "entrant": names.get(r["entrant"]),
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
        names = _entrant_name_map(division)
        return [
            {
                "round": r["round"],
                "winner": names.get(r["winner"]),
                "winner_score": r["winner_score"],
                "loser": names.get(r["loser"]),
                "loser_score": r["loser_score"],
                "winner_started": r["winner_started"],
            }
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
        return instances, self.reconcile_errors(division, instances)

    def after_save(self, division):
        # Recreating the slips can change which rounds have results; refresh the
        # status of every round (update_status is idempotent).
        for rp in division.round_pairings_set.all():
            rp.update_status()


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
