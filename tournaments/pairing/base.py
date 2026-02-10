from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto

from dataclasses_json import dataclass_json
from django.db.models import Count
from django.db.models.query import QuerySet
from tournaments.models import ResultSlip


class DefaultDict(defaultdict):
    def __missing__(self, key):
        ret = self.default_factory(key)
        self[key] = ret
        return ret


@dataclass_json
@dataclass
class RoundPairing:
    round: int
    start_round: int
    pairing: str


class RP(Enum):
    KotH = auto()
    QotH = auto()
    Swiss = auto()
    RoundRobin = auto()
    Quads_Clustered = auto()
    Quads_Distributed = auto()
    Quads_Evans = auto()
    Charlottesville = auto()

    @staticmethod
    def is_round_robin(name) -> bool:
        return name in (RP.RoundRobin.name, RP.Charlottesville.name)


class RoundStatus(Enum):
    Empty = 1
    Partial = 2
    Finished = 3


@dataclass_json
@dataclass
class Player:
    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    score: float = 0
    spread: int = 0
    starts: int = 0

    @property
    def is_bye(self) -> bool:
        return self.name.lower() == "bye"


@dataclass
class Result:
    name: str
    score: int
    opp_score: int
    start: bool

    @property
    def spread(self) -> int:
        return self.score - self.opp_score

    @classmethod
    def from_result_slip(cls, result_slip, winner) -> "Result":
        if winner:
            name = result_slip.winner_name
            score = result_slip.winner_score
            opp_score = result_slip.loser_score
            started = result_slip.winner_started
        else:
            name = result_slip.loser_name
            score = result_slip.loser_score
            opp_score = result_slip.winner_score
            started = not result_slip.winner_started
        return cls(name, score, opp_score, started)


@dataclass
class Results:
    players: dict[str, Player] = field(default_factory=lambda: DefaultDict(Player))
    rounds: dict[int, list[ResultSlip]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def update_player(self, result) -> None:
        p = self.players[result.name]
        p.spread += result.spread
        if result.spread > 0:
            p.wins += 1
        elif result.spread == 0:
            p.ties += 1
        else:
            p.losses += 1
        p.score = p.wins + 0.5 * p.ties
        p.starts += result.start

    def add_result(self, result_slip) -> None:
        winner = Result.from_result_slip(result_slip, True)
        loser = Result.from_result_slip(result_slip, False)
        self.update_player(winner)
        self.update_player(loser)
        self.rounds[result_slip.round].append(result_slip)

    def standings(self) -> Standings:
        standings = list(self.players.values())
        standings.sort(key=lambda x: -x.score)
        return standings


class Repeats:
    def __init__(self):
        self.matches = defaultdict(lambda: 0)

    def add(self, p1: Player, p2: Player) -> int:
        key = tuple(sorted([p1.name, p2.name]))
        self.matches[key] += 1
        return self.matches[key]

    def get(self, p1: Player, p2: Player) -> int:
        key = tuple(sorted([p1.name, p2.name]))
        return self.matches[key]


class Starts:
    def __init__(self, fixed_starts=None):
        self.starts = defaultdict(lambda: 0)
        self.h2h = {}
        self.recent_starts = defaultdict(lambda: 0)
        self.fixed_starts = fixed_starts or {}

    def _record(self, name1, name2, round, p1_starts) -> None:
        if p1_starts:
            self.starts[name1] += 1
            self.recent_starts[name1] = round
            self.h2h[(name1, name2)] = True
            self.h2h[(name2, name1)] = False
        else:
            self.starts[name2] += 1
            self.recent_starts[name2] = round
            self.h2h[(name1, name2)] = False
            self.h2h[(name2, name1)] = True

    def register(self, starter: Player, other: Player, round: int) -> None:
        """Record a known start from a finished round."""
        self._record(starter.name, other.name, round, True)

    def add(self, p1: Player, p2: Player, round: int) -> tuple[Player, Player]:
        """Decide who starts and record the result. Returns (first, second)."""
        name1, name2 = p1.name, p2.name
        if p1.is_bye:
            p1_starts = True
        elif p2.is_bye:
            p1_starts = False
        elif self.fixed_starts.get((round, name1)):
            p1_starts = True
        elif self.fixed_starts.get((round, name2)):
            p1_starts = False
        else:
            starts1 = self.starts[name1]
            starts2 = self.starts[name2]
            if starts1 == starts2:
                # Whoever went first most recently should go second now.
                if (name1, name2) not in self.h2h:
                    p1_starts = self.recent_starts[name1] <= self.recent_starts[name2]
                else:
                    p1_starts = not self.h2h[(name1, name2)]
            else:
                p1_starts = starts1 < starts2
        self._record(name1, name2, round, p1_starts)
        return (p1, p2) if p1_starts else (p2, p1)


class Byes:
    def __init__(self):
        self.byes = defaultdict(lambda: 0)

    def add(self, name) -> None:
        self.byes[name] += 1

    def get(self, name) -> int:
        return self.byes[name]

    def update(self, pairing) -> None:
        if pairing.first.is_bye:
            self.add(pairing.second.name)
        if pairing.second.is_bye:
            self.add(pairing.first.name)

    def reset(self) -> None:
        self.byes = defaultdict(lambda: 0)


@dataclass
class PairingData:
    """The raw data that gets passed to each of the pairing algorithms."""

    # Populated from the database
    result_slips: QuerySet
    entrants: QuerySet

    # We have a `repeats` field here because some pairings (e.g. swiss) depend on it as an input.
    # It is populated in pairings.pair() before pair_round() is called.
    repeats: Repeats

    @classmethod
    def for_division(cls, division) -> "PairingData":
        return cls(
            result_slips=division.result_slips.all(),
            entrants=division.entrants.all(),
            repeats=Repeats(),
        )


@dataclass
class Pairing:
    first: Player
    second: Player


@dataclass
class DisplayPairing(Pairing):
    repeats: int = 0


class Pairings:
    pairings: list[tuple[Player, Player]]

    def __init__(self):
        self.pairings = []

    def add(self, player1: Player, player2: Player) -> None:
        self.pairings.append((player1, player2))

    def add_result_slip(self, r: ResultSlip) -> None:
        winner = Player(r.winner_name)
        loser = Player(r.loser_name)
        if r.winner_started:
            self.add(winner, loser)
        else:
            self.add(loser, winner)

    def __iter__(self):
        return iter(self.pairings)

    def __len__(self) -> int:
        return len(self.pairings)


def results_after_round(pd: PairingData, round: int) -> Results:
    res = Results()
    for r in pd.result_slips:
        if r.round <= round:
            res.add_result(r)
    return res


Standings = list[Player]


def seedings(pd: PairingData) -> Standings:
    entrants = list(pd.entrants)
    entrants.sort(key=lambda x: -x.player.rating)
    return [Player(e.player.name) for e in entrants]


def standings_after_round(pd: PairingData, round: int) -> Standings:
    if round == 0:
        return seedings(pd)
    else:
        return results_after_round(pd, round).standings()


def round_status(pd: PairingData) -> dict[int, RoundStatus]:
    counts = defaultdict(lambda: RoundStatus.Empty)
    n_games = len(pd.entrants) / 2
    for x in pd.result_slips.values("round").annotate(Count("round")):
        round, count = x["round"], x["round__count"]
        if count == 0:
            counts[round] = RoundStatus.Empty
        elif count == n_games:
            counts[round] = RoundStatus.Finished
        else:
            counts[round] = RoundStatus.Partial
    return counts
