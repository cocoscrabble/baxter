"""Assign table numbers to pairings using a board->table map.

Boards are positional game slots (1..n) produced by the pairing algorithm.
The board->table map says which physical table each board sits at. Two boards
may share a table (one table holds two games); single tables hold one game.

Fixed table assignments override the natural board ordering: a pairing with a
fixed table claims a board at that table first, and the remaining pairings
fill the unused boards in order.
"""


def parse_board_table_map(raw):
    """Convert stored list-of-dicts form to {board: table} dict.

    Accepts the stored format `[{"board": 1, "table": 1}, ...]` or an already-
    converted dict. Returns a dict with int keys and int values.
    """
    if isinstance(raw, dict):
        return {int(k): int(v) for k, v in raw.items()}
    result = {}
    for row in raw or []:
        result[int(row["board"])] = int(row["table"])
    return result


def assign_tables(pairing_ids, fixed_table_by_pairing, board_table_map, n_games=None):
    """Assign a table number to each pairing.

    pairing_ids: ordered list of opaque pairing identifiers. Order is the
                 natural game order (typically by standings) used to walk
                 boards 1..n.
    fixed_table_by_pairing: {pairing_id: table_number} for pairings whose
                            table is forced by a FixedTable assignment.
    board_table_map: {board_number: table_number}. If empty, identity mapping
                     (board i -> table i) is used.
    n_games: optional; defaults to len(pairing_ids). The first n_games boards
             from the map are used.

    Returns {pairing_id: table_number}.

    Raises ValueError if the board map doesn't cover enough boards or if a
    fixed table has no matching board.
    """
    n = n_games if n_games is not None else len(pairing_ids)
    if not board_table_map:
        board_table_map = {i: i for i in range(1, n + 1)}
    if len(board_table_map) < n:
        raise ValueError(
            f"board_table_map covers {len(board_table_map)} boards but {n} games need tables"
        )

    sorted_boards = sorted(board_table_map.keys())[:n]
    slots = [board_table_map[b] for b in sorted_boards]
    used = [False] * len(slots)
    result = {}

    # Pass 1: place fixed-table pairings at the first matching free board.
    for pid in pairing_ids:
        if pid not in fixed_table_by_pairing:
            continue
        target = fixed_table_by_pairing[pid]
        for i, t in enumerate(slots):
            if not used[i] and t == target:
                used[i] = True
                result[pid] = t
                break
        else:
            raise ValueError(
                f"no free board for fixed table {target} (pairing {pid!r})"
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
