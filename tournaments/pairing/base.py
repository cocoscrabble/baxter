from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List

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
    def is_round_robin(name):
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
    score: int = 0
    spread: int = 0
    starts: int = 0

    @property
    def is_bye(self):
        return self.name.lower() == "bye"


@dataclass
class Result:
    name: str
    score: int
    opp_score: int
    start: bool

    @property
    def spread(self):
        return self.score - self.opp_score

    @classmethod
    def from_result_slip(cls, result_slip, winner):
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
    players: Dict[str, Player] = field(default_factory=lambda: DefaultDict(Player))
    rounds: Dict[int, List[ResultSlip]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def update_player(self, result):
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

    def add_result(self, result_slip):
        winner = Result.from_result_slip(result_slip, True)
        loser = Result.from_result_slip(result_slip, False)
        self.update_player(winner)
        self.update_player(loser)
        self.rounds[result_slip.round].append(result_slip)

    def standings(self):
        standings = list(self.players.values())
        standings.sort(key=lambda x: -x.score)
        return standings


class Repeats:
    def __init__(self):
        self.matches = defaultdict(lambda: 0)

    def add(self, name1, name2):
        key = tuple(sorted([name1, name2]))
        self.matches[key] += 1
        return self.matches[key]

    def get(self, name1, name2):
        key = tuple(sorted([name1, name2]))
        return self.matches[key]


@dataclass
class PairingData:
    result_slips: QuerySet
    entrants: QuerySet
    repeats: Repeats

    @classmethod
    def for_division(cls, division):
        return cls(
            result_slips=division.result_slips.all(),
            entrants=division.entrants.all(),
            repeats=Repeats(),
        )


@dataclass
class Pairing:
    first: Player
    second: Player
    repeats: int


def results_after_round(result_slips, round):
    res = Results()
    for r in result_slips:
        if r.round <= round:
            res.add_result(r)
    return res


def seedings(entrants):
    entrants = list(entrants)
    entrants.sort(key=lambda x: -x.player.rating)
    return [Player(e.player.name) for e in entrants]


def standings_after_round(round: int, pd: PairingData):
    if round == 0:
        return seedings(pd.entrants)
    else:
        return results_after_round(pd.result_slips, round).standings()


def round_status(result_slips, entrants):
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
