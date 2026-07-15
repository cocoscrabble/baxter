import random
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

import networkx as nx
from dataclasses_json import dataclass_json

from tournaments.pairing.round_pairing import (
    RoundPairing,
    normalize_round_robin_start_rounds,
)

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
    # A dropped entrant keeps their played results (they still count for
    # opponents) but is never paired again.
    dropped: bool = False


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

    # Round-by-round pairing configuration loaded from DivisionSettings.
    round_pairings: list[RoundPairing] = field(default_factory=list)

    # Fixed pairings keyed by round number. Each entry is a list of unordered (name1, name2)
    # pairs that must be matched regardless of what the pairing strategy would choose.
    fixed_pairings: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    # Temporary filter used by pair_round() while invoking a strategy for a round that has
    # fixed pairings. pair_round() sets this to the names of all fixed players before calling
    # the strategy, so that standings_after_round() omits them and the strategy only sees the
    # remaining entrants. pair_round() clears it again before returning.
    #
    # Strategies call standings_after_round(pd, ...) directly as a module-level function, so
    # this field is the least-invasive way to communicate the exclusion set to them without
    # modifying every strategy individually.
    excluded_names: set[str] = field(default_factory=set)

    @classmethod
    def for_division(cls, division) -> "PairingData":
        entrants = [
            EntrantData(PlayerData.from_db(e.player), dropped=e.dropped)
            for e in division.entrants.all()
        ]
        slips = [ResultSlipData.from_db(r) for r in division.result_slips.all()]
        fixed: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for fp in division.fixed_pairings.select_related("entrant1__player", "entrant2__player").all():
            fixed[fp.round_number].append((fp.entrant1.player.name, fp.entrant2.player.name))
        # A division with no settings row yet has no configured pairings.
        # Malformed blobs are rejected at write time (_validate_blocks), so a
        # missing row is the only thing to tolerate here. Imported locally to
        # keep this module free of module-level Django model dependencies.
        from tournaments.models import DivisionSettings

        try:
            raw_rps = division.settings.round_pairings or []
        except DivisionSettings.DoesNotExist:
            raw_rps = []
        rps = [RoundPairing.from_dict(x) for x in raw_rps]
        normalize_round_robin_start_rounds(rps)
        return cls(result_slips=slips, entrants=entrants, repeats=Repeats(), fixed_pairings=dict(fixed), round_pairings=rps)


class PairingError(Exception):
    """Raised when a round cannot be paired as configured — e.g. a set of fixed
    pairings that can't all be satisfied by a round-robin slot assignment. The
    message is surfaced to the organiser."""


def guard_no_dropped_in_block(pd, block_rounds, singular, plural) -> None:
    """Raise a clear ``PairingError`` if a withdrawn entrant already played a
    game in this block's rounds.

    Round-robin / quad blocks are a fixed template over a fixed field; once a
    player in the block has played, the block can't be re-paired around their
    withdrawal (the remaining templates still expect them). ``singular`` /
    ``plural`` name the block in the message, e.g. ``("round-robin",
    "round robins")``.
    """
    dropped = {e.player.name for e in pd.entrants if e.dropped}
    if not dropped:
        return
    for s in pd.result_slips:
        if s.round not in block_rounds:
            continue
        for name in (s.winner_name, s.loser_name):
            if name in dropped:
                raise PairingError(
                    f"{name} withdrew mid-{singular} — {plural} can't re-pair "
                    "around a withdrawal; convert the remaining rounds to "
                    "another strategy or enter forfeits."
                )


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

    @property
    def record(self) -> str:
        """Win-loss record as "12-4", or "10.5-3.5" when ties are involved.

        A tie counts half a win and half a loss for each player.
        """
        win_score = self.wins + 0.5 * self.ties
        loss_score = self.losses + 0.5 * self.ties

        def fmt(value: float) -> str:
            return f"{value:.1f}".rstrip("0").rstrip(".")

        return f"{fmt(win_score)}-{fmt(loss_score)}"


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
        # Rank by wins, then spread as the tiebreaker (standard Scrabble order:
        # among equal records, higher cumulative spread ranks higher).
        standings.sort(key=lambda x: (-x.score, -x.spread))
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
        # Non-mutating read: indexing the defaultdict would permanently insert
        # every probed key, and pair_no_repeats_blossom probes O(n²) pairs.
        return self.matches.get(key, 0)


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


def seedings(pd: PairingData, include_dropped: bool = False) -> Standings:
    # Dropped entrants are unpairable, so they never seed a round (the pairing
    # path leaves include_dropped False); the standings *display* passes True to
    # keep showing them.
    entrants = [e for e in pd.entrants if include_dropped or not e.dropped]
    entrants.sort(key=lambda x: -x.player.rating)
    return [Player(e.player.name) for e in entrants]


def standings_after_round(
    pd: PairingData, round: int, include_dropped: bool = False
) -> Standings:
    """Field standing after ``round``.

    The pairing engine calls this with ``include_dropped=False`` (the default):
    withdrawn players are removed from the pairable field and late entrants are
    appended as zero records so they start getting paired. The standings display
    passes ``include_dropped=True`` so withdrawn players stay visible (marked);
    their games always count for everyone else either way.
    """
    if round == 0:
        s = seedings(pd, include_dropped=include_dropped)
    else:
        s = results_after_round(pd, round).standings()
        # Withdrawn players still counted above — their games affect everyone
        # else's record/spread — but they can't be paired again.
        if not include_dropped:
            dropped = {e.player.name for e in pd.entrants if e.dropped}
            if dropped:
                s = [p for p in s if p.name not in dropped]
        # A late entrant has no result slips yet, so it never appears in
        # results-derived standings and would silently never be paired. Append
        # each recordless entrant as a zero record, in seeding (rating) order
        # among themselves, at the bottom of the field.
        present = {p.name for p in s}
        newcomers = [
            e
            for e in pd.entrants
            if e.player.name not in present and (include_dropped or not e.dropped)
        ]
        newcomers.sort(key=lambda e: -e.player.rating)
        s = s + [Player(e.player.name) for e in newcomers]
    # The bye is never a competitor: it must not appear in any pairing field or
    # in displayed standings. (It is added back as a forced pairing for an odd
    # field — see pair_round.)
    s = [p for p in s if not p.is_bye]
    if pd.excluded_names:
        s = [p for p in s if p.name not in pd.excluded_names]
    return s


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
