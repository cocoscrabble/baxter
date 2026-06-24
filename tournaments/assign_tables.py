"""Assign tables to pairings using a board->table map.

Boards are positional game slots (1..n) produced by the pairing algorithm.
The board->table map says which physical table each board sits at. Two boards
may share a table (one table holds two games); single tables hold one game.

A table has two attributes: an integer ``order`` (used to sort pairings and to
express which boards share a physical double table) and a display ``label`` (a
string shown to organizers, e.g. "S1" for a streamed table or "4" for an
ordinary one). Labels are unique across the map by construction.

Fixed table assignments override the natural board ordering: a pairing with a
fixed table (referenced by label) claims a board at that table first, and the
remaining pairings fill the unused boards in order.
"""


def parse_board_table_map(raw):
    """Convert the stored board->table map to {board: {"table", "label"}}.

    Accepts the stored format `[{"board": 1, "table": 1, "label": "1"}, ...]`
    or an already-converted dict. Rows without a ``label`` (legacy maps stored
    before streamed tables existed) default the label to ``str(table)``.
    Returns a dict with int board keys.
    """
    result = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            table = int(v)
            result[int(k)] = {"table": table, "label": str(table)}
        return result
    for row in raw or []:
        table = int(row["table"])
        label = str(row["label"]) if row.get("label") not in (None, "") else str(table)
        result[int(row["board"])] = {"table": table, "label": label}
    return result


def assign_tables(pairing_ids, fixed_table_by_pairing, board_table_map, n_games=None):
    """Assign a table (order, label) to each pairing.

    pairing_ids: ordered list of opaque pairing identifiers. Order is the
                 natural game order (typically by standings) used to walk
                 boards 1..n.
    fixed_table_by_pairing: {pairing_id: table_label} for pairings whose
                            table is forced by a FixedTable assignment. Matched
                            against the display label of each board's table.
    board_table_map: {board_number: {"table": int, "label": str}}. If empty,
                     an identity mapping (board i -> table i, label str(i)) is
                     used.
    n_games: optional; defaults to len(pairing_ids). The first n_games boards
             from the map are used.

    Returns {pairing_id: (table_order, table_label)}.

    Raises ValueError if the board map doesn't cover enough boards or if a
    fixed table label has no matching board.
    """
    n = n_games if n_games is not None else len(pairing_ids)
    if not board_table_map:
        board_table_map = {i: {"table": i, "label": str(i)} for i in range(1, n + 1)}
    if len(board_table_map) < n:
        raise ValueError(
            f"board_table_map covers {len(board_table_map)} boards but {n} games need tables"
        )

    sorted_boards = sorted(board_table_map.keys())[:n]
    slots = [
        (board_table_map[b]["table"], board_table_map[b]["label"])
        for b in sorted_boards
    ]
    used = [False] * len(slots)
    result = {}

    # Pass 1: place fixed-table pairings at the first matching free board.
    for pid in pairing_ids:
        if pid not in fixed_table_by_pairing:
            continue
        target = str(fixed_table_by_pairing[pid])
        for i, (order, label) in enumerate(slots):
            if not used[i] and label == target:
                used[i] = True
                result[pid] = (order, label)
                break
        else:
            raise ValueError(
                f"no free board for fixed table {target!r} (pairing {pid!r})"
            )

    # Pass 2: free pairings fill remaining boards in board order.
    remaining = [i for i, u in enumerate(used) if not u]
    free_iter = iter(remaining)
    for pid in pairing_ids:
        if pid in result:
            continue
        try:
            i = next(free_iter)
        except StopIteration:
            raise ValueError("ran out of boards for free pairings")
        used[i] = True
        result[pid] = slots[i]

    return result
