"""Group a page's round tabs by day (``DivisionSettings.games_per_day``).

Every page with one tab per round carries a day picker; each tab is tagged
with its day and shown only when the picker reads "All" or that day. The
filtering is client-side, so the picker keeps its choice when a tab click
re-renders the tab bar.
"""

from django import template

register = template.Library()


@register.filter
def round_day(round, division):
    """The day ``round`` is played on in ``division``."""
    return division.day_of_round(int(round))


@register.inclusion_tag("tournaments/_day_picker.html")
def day_picker(division, last_round, selected_round=None):
    """A Day 1 / Day 2 / … / All picker for a tab bar running to ``last_round``.

    Renders nothing for a schedule that fits in one day. It starts on the day
    of ``selected_round`` and follows it: when the page re-renders with the
    selection on another day (say publishing the last round of day 1 moves on
    to round 8), the picker moves with it rather than hiding the tab in view.
    """
    last_day = division.day_of_round(int(last_round)) if last_round else 1
    if last_day < 2:
        return {"days": []}
    selected_day = (
        division.day_of_round(int(selected_round)) if selected_round else None
    )
    return {
        "days": range(1, last_day + 1),
        "selected_day": selected_day,
        "default_day": selected_day or last_day,
    }
