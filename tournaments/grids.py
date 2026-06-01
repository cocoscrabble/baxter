"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import EditGrid

from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO
from .models import Entrant, FixedPairing, FixedTable, Player


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

    def serialize_row(self, ft):
        return {
            "round_number": ft.round_number,
            "entrant": ft.entrant_id,
            "table_number": ft.table_number,
        }

    def lookups(self, division):
        round_numbers = division.configured_round_numbers()
        round_values = [{"value": -1, "label": "All"}] + [
            {"value": r, "label": str(r)} for r in round_numbers
        ]
        return {
            "entrantValues": _entrant_values(division),
            "roundValues": round_values,
        }

    def validate_args(self, division):
        return (set(division.entrants.values_list("pk", flat=True)), {})
