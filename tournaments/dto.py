"""Data transfer objects."""

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
