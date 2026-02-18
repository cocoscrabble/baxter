import random
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

import networkx as nx
from dataclasses_json import dataclass_json

# ---------------------------------------------------------------------------
# Snapshot of db objects
# ---------------------------------------------------------------------------

# These classes duplicate the structure of some of the tournament db models.
# The idea is to disconnect the pairings code from the rest of the application.
# We call some accessors and methods on the Model classes in the classmethod
# constructors, relying on duck typing so that we do not need access to the
# django class definitions for type annotations.


@dataclass
class PlayerData:
    name: str
    rating: int

    @classmethod
    def from_db(cls, player) -> "PlayerData":
        return cls(name=player.name, rating=player.rating)


@dataclass
class EntrantData:
    player: PlayerData


@dataclass
class ResultSlipData:
    round: int
    winner_name: str
    loser_name: str
    winner_score: int
    loser_score: int
    winner_started: bool

    @classmethod
    def from_db(cls, r) -> "ResultSlipData":
        return cls(
            round=r.round,
            winner_name=r.winner_name,
            loser_name=r.loser_name,
            winner_score=r.winner_score,
            loser_score=r.loser_score,
            winner_started=r.winner_started,
        )

    @property
    def first_name(self) -> str:
        """Name of the player who went first."""
        return self.winner_name if self.winner_started else self.loser_name

    @property
    def second_name(self) -> str:
        """Name of the player who went second."""
        return self.loser_name if self.winner_started else self.winner_name


@dataclass
class PairingData:
    """The raw data that gets passed to each of the pairing algorithms."""

    # Populated from db data in for_division
    result_slips: list[ResultSlipData]
    entrants: list[EntrantData]

    # We have a `repeats` field here because some pairings (e.g. swiss) depend on it as an input.
    # It is populated in pairings.pair() before pair_round() is called.
    repeats: Repeats

    @classmethod
    def for_division(cls, division) -> "PairingData":
        entrants = [
            EntrantData(PlayerData.from_db(e.player)) for e in division.entrants.all()
        ]
        slips = [ResultSlipData.from_db(r) for r in division.result_slips.all()]
        return cls(result_slips=slips, entrants=entrants, repeats=Repeats())


class DefaultDict(defaultdict):
    """A defaultdict that passes the missing key to the factory.

    e.g. DefaultDict(Player) will call Player(key) for missing keys,
    so players["Alice"] creates Player("Alice").
    """

    def __missing__(self, key):
        ret = self.default_factory(key)
        self[key] = ret
        return ret


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


@dataclass(frozen=True)
class Pairing:
    first: Player
    second: Player


@dataclass(frozen=True)
class DisplayPairing(Pairing):
    repeats: int = 0


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
    rounds: dict[int, list[ResultSlipData]] = field(
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
        self.matches = defaultdict(int)

    def _key(self, p: Pairing) -> tuple:
        return tuple(sorted([p.first.name, p.second.name]))

    def add(self, p: Pairing) -> int:
        key = self._key(p)
        self.matches[key] += 1
        return self.matches[key]

    def get(self, p: Pairing) -> int:
        key = self._key(p)
        return self.matches[key]


class Starts:
    def __init__(self, fixed_starts=None):
        self.starts = defaultdict(int)
        self.h2h = {}
        self.recent_starts = defaultdict(int)
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

    def register(self, p: Pairing, round: int) -> None:
        """Record a known start from a finished round."""
        self._record(p.first.name, p.second.name, round, True)

    def add(self, p: Pairing, round: int) -> Pairing:
        """Decide who starts and record the result. Returns Pairing(first, second)."""
        name1, name2 = p.first.name, p.second.name
        if p.first.is_bye:
            p1_starts = True
        elif p.second.is_bye:
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
        return p if p1_starts else Pairing(p.second, p.first)


class Byes:
    def __init__(self):
        self.byes = defaultdict(int)

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
        self.byes = defaultdict(int)


class Pairings:
    pairings: list[Pairing]

    def __init__(self):
        self.pairings = []

    def add(self, player1: Player, player2: Player) -> None:
        self.pairings.append(Pairing(player1, player2))

    def add_result_slip(self, r: ResultSlipData) -> None:
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


def blossom(edges) -> list[tuple]:
    # The nx implementation of blossom does not like negative weights.
    m = min(x[2] for x in edges) if edges else 0
    edges = [[v1, v2, w - m] for v1, v2, w in edges]
    g = nx.Graph()
    g.add_weighted_edges_from(edges)
    return list(sorted(nx.max_weight_matching(g, maxcardinality=True)))


def pair_no_repeats_blossom(players: Standings, repeats: Repeats) -> Pairings:
    """Blossom matching to minimize repeat opponents with random tiebreaking."""
    edges = []
    names = {}
    inames = {}
    for i, player in enumerate(players):
        names[player.name] = i
        inames[i] = player.name
    for p1 in players:
        for p2 in players:
            if p1.name < p2.name:
                reps = repeats.get(Pairing(p1, p2))
                weight = -(10 * reps + random.random())
                v1 = names[p1.name]
                v2 = names[p2.name]
                edges.append([v1, v2, weight])
    b = blossom(edges)
    pairings = Pairings()
    for v1, v2 in b:
        pairings.add(Player(inames[v1]), Player(inames[v2]))
    return pairings
