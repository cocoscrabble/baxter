"""How a player's name is shown when it is not unique.

Names stopped being identities in plans/PLAN_PLAYER_IDENTITY.md, so two entrants
in one division may answer to the same one. A name renders bare; the player
number is appended only when someone else *in the same scope* shares it, so the
common case is unchanged and the ambiguous case is resolvable (decision 7).

Scope is the division for rosters, standings, pairings, results and scorecards —
what a reader is looking at is one division, and a clash they cannot see is not
a clash they need warning about. Scope is the whole roster for the player
pickers, where the candidate set really is every player.
"""


def display_names(players) -> dict[str, str]:
    """``{player_number: label}`` for ``players`` — bare name, or "Name (NUMBER)".

    ``players`` is any iterable of objects with ``name`` and ``player_number``.
    Comparison is case-insensitive, matching how names are matched everywhere
    else; the label keeps each player's own spelling.
    """
    players = list(players)
    counts: dict[str, int] = {}
    for player in players:
        counts[player.name.casefold()] = counts.get(player.name.casefold(), 0) + 1
    return {
        player.player_number: (
            f"{player.name} ({player.player_number})"
            if counts[player.name.casefold()] > 1
            else player.name
        )
        for player in players
    }


def division_labels(division) -> dict[int, str]:
    """``{player_id: label}`` for one division's entrants.

    Keyed on the player pk rather than the number because that is what the
    objects a template renders already carry (``slip.winner.player_id``), so
    stamping the labels on needs no extra query.
    """
    entrants = list(division.entrants.select_related("player"))
    by_number = display_names(e.player for e in entrants)
    return {e.player_id: by_number[e.player.player_number] for e in entrants}


def label_entrants(labels, *groups) -> None:
    """Stamp ``display_name`` onto every Entrant in ``groups``.

    Each group is an iterable of Entrants, or of ``None``. Entrants reached
    through different queries (a slip's winner, a pairing's first) are distinct
    Python objects for the same row, which is why the label is applied to the
    instances a template will actually render rather than looked up from one.
    """
    for group in groups:
        for entrant in group:
            if entrant is None:
                continue
            label = labels.get(entrant.player_id)
            if label:
                entrant.display_name = label


def label_standings(division, standings) -> None:
    """Rewrite each standings row's ``name`` to its disambiguated label.

    The standings type carries both ``key`` and ``name`` (the pairing layer
    groups on the key, templates render the name), so disambiguating is a matter
    of replacing the name — nothing downstream has to learn a new field.
    """
    entrants = list(division.entrants.select_related("player"))
    labels = display_names(e.player for e in entrants)
    for row in standings:
        label = labels.get(row.key)
        if label:
            row.name = label
