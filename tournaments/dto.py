"""Data transfer objects.

The generic ``parse_rows`` helper that drives these now lives in
``editgrid.grids`` — these DTOs supply the domain ``from_json``/``validate``/
``to_db_kwargs`` protocol it expects.
"""

from dataclasses import dataclass, fields

from dataclasses_json import DataClassJsonMixin


@dataclass
class ResultSlipDTO(DataClassJsonMixin):
    round: int
    winner: int
    winner_score: int
    loser: int
    loser_score: int
    winner_started: bool

    @classmethod
    def from_json(cls, row: dict) -> "ResultSlipDTO | None":
        """Parse a JSON row dict. Returns None if a field is missing/invalid.

        Coerce the numeric fields explicitly (mirroring ``FixedPairingDTO``) so a
        string-typed number from the client behaves the same as it does in the
        fixed-pairings grid. ``winner_started`` is serialized as a bool.
        """
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        try:
            return cls(
                round=int(row["round"]),
                winner=int(row["winner"]),
                winner_score=int(row["winner_score"]),
                loser=int(row["loser"]),
                loser_score=int(row["loser_score"]),
                winner_started=bool(row["winner_started"]),
            )
        except (ValueError, TypeError):
            return None

    def validate(self, entrant_ids: set[int]) -> list[str]:
        """Return list of validation error strings."""
        errors = []
        if self.winner == self.loser:
            errors.append("winner and loser must be different.")
        if self.winner not in entrant_ids:
            errors.append("invalid winner.")
        if self.loser not in entrant_ids:
            errors.append("invalid loser.")
        return errors

    def to_db_kwargs(self) -> dict:
        """Return kwargs for ResultSlip.objects.create()."""
        return {
            "round": self.round,
            "winner_id": self.winner,
            "winner_score": self.winner_score,
            "loser_id": self.loser,
            "loser_score": self.loser_score,
            "winner_started": self.winner_started,
        }


@dataclass
class EntrantDTO(DataClassJsonMixin):
    number: int
    player: int
    dropped: bool = False
    # Registration state. All optional with defaults, so a payload written
    # before these columns existed still parses.
    #
    # ``rating`` is None when the row did not carry one, which means "derive the
    # snapshot from the player". A row that *does* carry one is a director
    # setting it by hand, which makes it manual.
    rating: int | None = None
    # Set only by a portable payload being replayed, which is restoring a
    # recorded snapshot rather than deciding one — so it is honoured verbatim,
    # source and all. A browser never sends this.
    rating_source: str = ""
    tentative: bool = False
    paid: bool = False
    playing_up: bool = False

    @classmethod
    def from_json(cls, row: dict) -> "EntrantDTO | None":
        """Parse a JSON row dict. Returns None if a field is missing/invalid.

        Numeric fields are coerced explicitly (mirroring ``FixedPairingDTO``) so
        string-typed numbers behave identically across grids. ``dropped`` is
        optional (defaults False) so older payloads without the column still
        parse.
        """
        if row.get("number") is None or row.get("player") is None:
            return None
        raw_rating = row.get("rating")
        try:
            return cls(
                number=int(row["number"]),
                player=int(row["player"]),
                dropped=bool(row.get("dropped", False)),
                rating=None if raw_rating in (None, "") else int(raw_rating),
                rating_source=str(row.get("rating_source") or ""),
                tentative=bool(row.get("tentative", False)),
                paid=bool(row.get("paid", False)),
                playing_up=bool(row.get("playing_up", False)),
            )
        except (ValueError, TypeError):
            return None

    def validate(self, valid_player_ids: set[int], seen_players: set[int]) -> list[str]:
        """Return list of validation error strings. Adds player to seen_players if valid."""
        errors = []
        if self.player not in valid_player_ids:
            errors.append("player not found.")
        elif self.player in seen_players:
            errors.append("duplicate player.")
        else:
            seen_players.add(self.player)
        return errors

    def to_db_kwargs(self) -> dict:
        """Return kwargs for Entrant.objects.create().

        ``rating`` is left out: the grid pins it server-side in ``prepare``,
        which is the only place that may decide it.
        """
        return {
            "number": self.number,
            "player_id": self.player,
            "dropped": self.dropped,
            "tentative": self.tentative,
            "paid": self.paid,
            "playing_up": self.playing_up,
        }


@dataclass
class FixedPairingDTO(DataClassJsonMixin):
    round_number: int
    entrant1: int  # entrant pk
    entrant2: int  # entrant pk

    @classmethod
    def from_json(cls, row: dict) -> "FixedPairingDTO | None":
        """Parse a JSON row dict. Returns None if any required field is missing/invalid."""
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        try:
            return cls(
                round_number=int(row["round_number"]),
                entrant1=int(row["entrant1"]),
                entrant2=int(row["entrant2"]),
            )
        except (ValueError, TypeError):
            return None

    def validate(self, valid_entrant_ids: set[int], seen_per_round: dict[int, set[int]]) -> list[str]:
        """Return list of validation error strings."""
        errors = []
        if self.entrant1 == self.entrant2:
            errors.append("entrant1 and entrant2 must be different.")
            return errors
        if self.entrant1 not in valid_entrant_ids:
            errors.append("player 1 not found in division.")
        if self.entrant2 not in valid_entrant_ids:
            errors.append("player 2 not found in division.")
        if errors:
            return errors
        seen = seen_per_round.setdefault(self.round_number, set())
        if self.entrant1 in seen:
            errors.append(f"player 1 already paired in round {self.round_number}.")
        elif self.entrant2 in seen:
            errors.append(f"player 2 already paired in round {self.round_number}.")
        else:
            seen.add(self.entrant1)
            seen.add(self.entrant2)
        return errors

    def to_db_kwargs(self) -> dict:
        """Return kwargs for FixedPairing.objects.create()."""
        return {
            "round_number": self.round_number,
            "entrant1_id": self.entrant1,
            "entrant2_id": self.entrant2,
        }


@dataclass
class FixedTableDTO(DataClassJsonMixin):
    round_number: int
    entrant: int  # entrant pk
    table_label: str

    @classmethod
    def from_json(cls, row: dict) -> "FixedTableDTO | None":
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        label = str(row["table_label"]).strip()
        if not label:
            return None
        try:
            return cls(
                round_number=int(row["round_number"]),
                entrant=int(row["entrant"]),
                table_label=label,
            )
        except (ValueError, TypeError):
            return None

    def validate(self, valid_entrant_ids: set[int], seen_per_round: dict[int, set[int]]) -> list[str]:
        errors = []
        if self.entrant not in valid_entrant_ids:
            errors.append("player not found in division.")
            return errors
        seen = seen_per_round.setdefault(self.round_number, set())
        if self.entrant in seen:
            errors.append(f"player already has a fixed table assignment in round {self.round_number}.")
        else:
            seen.add(self.entrant)
        return errors

    def to_db_kwargs(self) -> dict:
        return {
            "round_number": self.round_number,
            "entrant_id": self.entrant,
            "table_label": self.table_label,
        }
