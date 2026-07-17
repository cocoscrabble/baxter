"""Parse a historical division for the "what if" sandbox.

Two input shapes are accepted, sniffed by content:

- a **JSON bundle** as emitted by ``tournament_export.py`` (players with
  ratings, entrants, results with ``winner_started``); may hold several
  divisions.
- the **coco-ratings CSV** produced by ``results_export.py``
  (``Submitted On, Round, Winner, Winners Score, Opponent, Opponents Score``) —
  names and scores only.

Both are reduced to the same *portable division* dict — name-keyed, pk-free —
which the ``division_imported`` command turns into a sandbox division. Parsing
does no DB writes: byes are inferred and players are resolved/created later, in
the command, so a replay reproduces them.

Portable division shape::

    {
        "name": str,
        "entrants": [{"player": name, "rating": int, "number": int}, ...],
        "results": [{"round": int, "winner": name, "loser": name,
                     "winner_score": int, "loser_score": int,
                     "winner_started": bool}, ...],
    }
"""

import csv
import io

from tournaments.results_export import HEADERS as CSV_HEADERS


class ImportParseError(Exception):
    """The uploaded data could not be parsed into a division."""


def parse_import(text: str) -> tuple[str | None, list[dict]]:
    """Sniff and parse ``text`` into ``(tournament_name, [portable_division])``.

    ``tournament_name`` is the bundle's name for JSON, ``None`` for CSV (which
    carries no tournament name). Raises :class:`ImportParseError` on malformed
    input; nothing is written.
    """
    stripped = text.lstrip()
    if not stripped:
        raise ImportParseError("The file is empty.")
    if stripped[0] == "{":
        return _parse_json_bundle(text)
    return None, [_parse_csv(text)]


def _parse_json_bundle(text: str) -> tuple[str, list[dict]]:
    from tournaments.tournament_export import ExportTournament

    try:
        bundle = ExportTournament.from_json(text)
    except Exception as e:  # dataclass_json raises KeyError/ValueError on bad shape
        raise ImportParseError(f"Not a valid tournament JSON bundle: {e}") from e
    if not bundle.divisions:
        raise ImportParseError("The bundle contains no divisions.")

    by_number = {p.player_number: p for p in bundle.players}

    def name_of(player_number: str) -> str:
        p = by_number.get(player_number)
        if p is None:
            raise ImportParseError(
                f"A result or entrant references unknown player {player_number!r}."
            )
        return p.name

    divisions = []
    for d in bundle.divisions:
        entrants = [
            {"player": name_of(e.player_number), "rating": by_number[e.player_number].rating,
             "number": e.number}
            for e in d.entrants
        ]
        results = [
            {
                "round": r.round,
                "winner": name_of(r.winner),
                "loser": name_of(r.loser),
                "winner_score": r.winner_score,
                "loser_score": r.loser_score,
                "winner_started": r.winner_started,
            }
            for r in d.results
        ]
        divisions.append({"name": d.name, "entrants": entrants, "results": results})
    return bundle.name, divisions


def _parse_csv(text: str) -> dict:
    from tournaments.models import Player

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ImportParseError("The CSV is empty.")
    header = [cell.strip().lower() for cell in rows[0][: len(CSV_HEADERS)]]
    if header != [h.lower() for h in CSV_HEADERS]:
        raise ImportParseError(
            "Unexpected CSV header — expected the coco-ratings columns: "
            + ", ".join(CSV_HEADERS)
        )

    results = []
    names: dict[str, str] = {}  # lowercase -> first-seen display name
    for i, row in enumerate(rows[1:], start=2):
        cells = [cell.strip() for cell in row]
        if len(cells) < len(CSV_HEADERS):
            raise ImportParseError(f"Row {i}: expected {len(CSV_HEADERS)} columns.")
        _submitted, round_s, winner, wscore_s, opponent, oscore_s = cells[:6]
        if not winner or not opponent:
            raise ImportParseError(f"Row {i}: winner and opponent names are required.")
        try:
            round_num, wscore, oscore = int(round_s), int(wscore_s), int(oscore_s)
        except ValueError:
            raise ImportParseError(
                f"Row {i}: round and scores must be whole numbers."
            ) from None
        for name in (winner, opponent):
            names.setdefault(name.lower(), name)
        results.append({
            "round": round_num,
            # Canonicalize to the first-seen casing so result names match entrant
            # names exactly (the command keys entrants by name).
            "winner": names[winner.lower()],
            "loser": names[opponent.lower()],
            "winner_score": wscore,
            "loser_score": oscore,
            # The CSV never recorded who went first; default and note the limit
            # (matchings are unaffected — only display orientation is approximate).
            "winner_started": True,
        })
    if not results:
        raise ImportParseError("The CSV has a header but no result rows.")

    # Ratings come from the Player roster (unknown names seed at 0); entrant
    # numbers are assigned by rating, descending, then name for a stable order.
    display_names = list(names.values())
    ratings = {
        name: (
            p.rating
            if (p := Player.objects.filter(name__iexact=name).first())
            else 0
        )
        for name in display_names
    }
    ordered = sorted(display_names, key=lambda n: (-ratings[n], n.lower()))
    entrants = [
        {"player": name, "rating": ratings[name], "number": i}
        for i, name in enumerate(ordered, start=1)
    ]
    return {"name": "Division 1", "entrants": entrants, "results": results}
