"""Serialize a finished tournament into a portable JSON bundle for the registry.

This is the "after" half of the registry sync: once a tournament is run, Baxter
emits a bundle describing the tournament, its divisions, the entrants in each,
and every result. The registry ingests it to record results and compute ratings.

Players are referenced everywhere by ``player_number`` — the registry's canonical
identity. Players created locally in Baxter carry a temporary ``T-`` number and
are flagged ``provisional``; the registry replaces those with real numbers on
upload and returns a mapping (handled by the import side, not here).

Test and deleted divisions are excluded — only real, live divisions are exported.
"""

from dataclasses import dataclass, field

from dataclasses_json import dataclass_json

from .models import Player


@dataclass_json
@dataclass
class ExportPlayer:
    player_number: str
    name: str
    rating: int
    provisional: bool

    @classmethod
    def from_db(cls, player) -> "ExportPlayer":
        return cls(
            player_number=player.player_number,
            name=player.name,
            rating=player.rating,
            provisional=player.is_provisional,
        )


@dataclass_json
@dataclass
class ExportEntrant:
    number: int
    player_number: str

    @classmethod
    def from_db(cls, entrant) -> "ExportEntrant":
        return cls(number=entrant.number, player_number=entrant.player.player_number)


@dataclass_json
@dataclass
class ExportResult:
    round: int
    winner: str  # player_number
    winner_score: int
    loser: str  # player_number
    loser_score: int
    winner_started: bool

    @classmethod
    def from_db(cls, slip) -> "ExportResult":
        return cls(
            round=slip.round,
            winner=slip.winner.player.player_number,
            winner_score=slip.winner_score,
            loser=slip.loser.player.player_number,
            loser_score=slip.loser_score,
            winner_started=slip.winner_started,
        )


@dataclass_json
@dataclass
class ExportDivision:
    name: str
    entrants: list[ExportEntrant] = field(default_factory=list)
    results: list[ExportResult] = field(default_factory=list)

    @classmethod
    def from_db(cls, division) -> "ExportDivision":
        return cls(
            name=division.name,
            entrants=[
                ExportEntrant.from_db(e)
                for e in division.entrants.select_related("player")
            ],
            results=[
                ExportResult.from_db(r)
                for r in division.result_slips.select_related(
                    "winner__player", "loser__player"
                )
            ],
        )


@dataclass_json
@dataclass
class ExportTournament:
    name: str
    location: str
    start_date: str  # ISO 8601 date
    players: list[ExportPlayer] = field(default_factory=list)
    divisions: list[ExportDivision] = field(default_factory=list)

    @classmethod
    def from_db(cls, tournament) -> "ExportTournament":
        divisions = list(tournament.divisions.filter(is_test=False))
        # One ExportPlayer per distinct player across all exported divisions, so
        # a player entered in two divisions is described only once.
        players = Player.objects.filter(
            entries__division__in=divisions
        ).distinct().order_by("player_number")
        return cls(
            name=tournament.name,
            location=tournament.location,
            start_date=tournament.start_date.isoformat(),
            players=[ExportPlayer.from_db(p) for p in players],
            divisions=[ExportDivision.from_db(d) for d in divisions],
        )


def export_tournament(tournament):
    """Return the tournament bundle as a JSON string."""
    return ExportTournament.from_db(tournament).to_json(indent=2)
