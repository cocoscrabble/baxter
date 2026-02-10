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

    def add(self, name1, name2) -> int:
        key = tuple(sorted([name1, name2]))
        self.matches[key] += 1
        return self.matches[key]

    def get(self, name1, name2) -> int:
        key = tuple(sorted([name1, name2]))
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

    def register(self, starter, other, round) -> None:
        """Record a known start from a finished round."""
        self._record(starter, other, round, True)

    def add(self, name1, name2, round) -> bool:
        """Decide who starts and record the result. Returns True if p1 starts."""
        if name1.lower() == "bye":
            p1_starts = True
        elif name2.lower() == "bye":
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
        return p1_starts


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
    result_slips: QuerySet
    entrants: QuerySet
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


Pairings = list[tuple[Player, Player]]
Standings = list[Player]


def results_after_round(result_slips, round) -> Results:
    res = Results()
    for r in result_slips:
        if r.round <= round:
            res.add_result(r)
    return res


def seedings(entrants) -> Standings:
    entrants = list(entrants)
    entrants.sort(key=lambda x: -x.player.rating)
    return [Player(e.player.name) for e in entrants]


def standings_after_round(round: int, pd: PairingData) -> Standings:
    if round == 0:
        return seedings(pd.entrants)
    else:
        return results_after_round(pd.result_slips, round).standings()


def round_status(result_slips, entrants) -> dict[int, RoundStatus]:
    counts = defaultdict(lambda: RoundStatus.Empty)
    n_games = len(entrants) / 2
    for x in result_slips.values("round").annotate(Count("round")):
        round, count = x["round"], x["round__count"]
        if count == 0:
            counts[round] = RoundStatus.Empty
        elif count == n_games:
            counts[round] = RoundStatus.Finished
        else:
            counts[round] = RoundStatus.Partial
    return counts
