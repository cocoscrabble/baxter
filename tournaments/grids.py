"""Concrete editgrid configs for Baxter's editable grids."""

from editgrid.grids import EditGrid

from .dto import EntrantDTO
from .models import Entrant, Player


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
