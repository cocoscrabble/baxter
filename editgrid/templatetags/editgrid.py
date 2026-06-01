"""Template tags for composing an editable grid into a host page.

The pieces are placed independently — the table, the presence banner, and
(optionally) the generic toolbar — all reading from one ``grid`` GridContext
the view supplies. The toolbar is otherwise convention-based: a host page can
hand-roll its toolbar (mixing custom and generic controls) as long as it uses
the well-known button ids the JS wires to.
"""

from django import template

register = template.Library()


@register.inclusion_tag("editgrid/_table.html", takes_context=True)
def editgrid_table(context, grid):
    return {"grid": grid, "csrf_token": context.get("csrf_token", "")}


@register.inclusion_tag("editgrid/_presence.html", takes_context=True)
def editgrid_presence(context, grid):
    return {"grid": grid, "csrf_token": context.get("csrf_token", "")}


@register.inclusion_tag("editgrid/_toolbar.html")
def editgrid_toolbar(add_label="Add Row"):
    """The standard generic controls. For grids with custom controls, skip this
    and hand-roll the toolbar with the same button ids."""
    return {"add_label": add_label}
