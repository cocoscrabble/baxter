"""Data transfer objects."""

from dataclasses import dataclass, fields

from dataclasses_json import DataClassJsonMixin


def parse_rows(dto_cls, rows, *validate_args):
    """Parse and validate a list of row dicts via a DTO class.

    The DTO must provide a ``from_json(row)`` classmethod returning None for
    missing/invalid input, and a ``validate(*args)`` method returning a list
    of error strings. Returns ``(validated, errors)`` with errors prefixed by
    row number.
    """
    errors = []
    validated = []
    for i, row in enumerate(rows):
        dto = dto_cls.from_json(row)
        if dto is None:
            errors.append(f"Row {i + 1}: all fields are required.")
            continue
        row_errors = dto.validate(*validate_args)
        if row_errors:
            errors.extend(f"Row {i + 1}: {e}" for e in row_errors)
        else:
            validated.append(dto)
    return validated, errors


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
        """Parse a JSON row dict. Returns None if required fields are missing."""
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        return cls.from_dict(row)

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

    @classmethod
    def from_json(cls, row: dict) -> "EntrantDTO | None":
        """Parse a JSON row dict. Returns None if required fields are missing."""
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        return cls.from_dict(row)

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
        """Return kwargs for Entrant.objects.create()."""
        return {
            "number": self.number,
            "player_id": self.player,
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
    table_number: int

    @classmethod
    def from_json(cls, row: dict) -> "FixedTableDTO | None":
        if any(row.get(f.name) is None for f in fields(cls)):
            return None
        try:
            return cls(
                round_number=int(row["round_number"]),
                entrant=int(row["entrant"]),
                table_number=int(row["table_number"]),
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
            errors.append(f"player already has a fixed table in round {self.round_number}.")
        else:
            seen.add(self.entrant)
        return errors

    def to_db_kwargs(self) -> dict:
        return {
            "round_number": self.round_number,
            "entrant_id": self.entrant,
            "table_number": self.table_number,
        }
