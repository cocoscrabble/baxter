"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import Column, EditGrid, JsonBlobGrid

from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO
from .models import DivisionSettings, Entrant, FixedPairing, FixedTable, Player, ResultSlip


def _entrant_values(division):
    entrants = division.entrants.select_related("player").order_by("number")
    return [{"id": e.pk, "label": e.player.name} for e in entrants]


class EntrantsGrid(EditGrid):
    model = Entrant
    parent_field = "division"
    related_name = "entrants"
    scope = "entrants"
    dto_class = EntrantDTO
    dom_id = "entrants-table"
    js_module = "tournaments/js/edit_entrants.js"
    template_name = "tournaments/division_entrants_edit.html"
    columns = [
        Column("number", "#", kind="display", width=60, align="center"),
        Column("player", "Player", kind="choice", lookup="players", autocomplete=True),
    ]

    def queryset(self, division):
        return division.entrants.select_related("player").order_by("number")

    def serialize_row(self, entrant):
        return {"number": entrant.number, "player": entrant.player_id}

    def lookups(self, division):
        return {"players": [{"id": p.pk, "label": p.name} for p in Player.objects.all()]}

    def validate_args(self, division):
        return (set(Player.objects.values_list("pk", flat=True)), set())


class FixedPairingsGrid(EditGrid):
    model = FixedPairing
    parent_field = "division"
    related_name = "fixed_pairings"
    scope = "fixed_pairings"
    dto_class = FixedPairingDTO
    dom_id = "fixed-pairings-table"
    js_module = "tournaments/js/edit_fixed_pairings.js"
    template_name = "tournaments/division_fixed_pairings_edit.html"
    columns = [
        Column("round_number", "Round", kind="number", min=1, width=100, align="center"),
        Column("entrant1", "Player 1", kind="choice", lookup="entrantValues", autocomplete=True),
        Column("entrant2", "Player 2", kind="choice", lookup="entrantValues", autocomplete=True),
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
    js_module = "tournaments/js/edit_fixed_tables.js"
    template_name = "tournaments/division_fixed_tables_edit.html"
    columns = [
        Column("round_number", "Round", kind="choice", lookup="roundValues", width=100, align="center"),
        Column("entrant", "Player", kind="choice", lookup="entrantValues", autocomplete=True, min_width=200),
        Column("table_number", "Table", kind="number", min=1, width=100, align="center"),
    ]

    def serialize_row(self, ft):
        return {
            "round_number": ft.round_number,
            "entrant": ft.entrant_id,
            "table_number": ft.table_number,
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
    js_module = "tournaments/js/edit_results.js"
    template_name = "tournaments/division_edit_results.html"
    columns = [
        Column("round", "Round", kind="number", min=1, width=100),
        Column("winner", "Winner", kind="choice", lookup="entrants"),
        Column("winner_score", "W Score", kind="number", min=0, width=120),
        Column("loser", "Opponent", kind="choice", lookup="entrants"),
        Column("loser_score", "Opp Score", kind="number", min=0, width=130),
        Column("winner_started", "Started", kind="choice",
               values={True: "Winner", False: "Opponent"}, width=120, value_type="bool"),
    ]

    def queryset(self, division):
        return division.result_slips.select_related("winner", "loser").order_by("round", "pk")

    def serialize_row(self, slip):
        return slip.to_dict()

    def lookups(self, division):
        entrants = division.entrants.select_related("player").order_by("number")
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
        return instances, []

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
    js_module = "tournaments/js/edit_board_table_map.js"
    template_name = "tournaments/division_board_table_map_edit.html"
    columns = [
        Column("board", "Board", kind="number", min=1, width=120, align="center"),
        Column("table", "Table", kind="number", min=1, width=120, align="center"),
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
                errors.append(f"Row {i + 1}: board and table must be integers.")
                continue
            if board < 1 or table < 1:
                errors.append(f"Row {i + 1}: board and table must be positive.")
                continue
            if board in seen_boards:
                errors.append(f"Row {i + 1}: duplicate board {board}.")
                continue
            seen_boards.add(board)
            validated.append({"board": board, "table": table})
        validated.sort(key=lambda r: r["board"])
        return validated, errors
