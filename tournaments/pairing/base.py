from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from dataclasses_json import dataclass_json

from tournaments.pairing.round_pairing import (
    RoundPairing,
    normalize_round_robin_start_rounds,
)

# The bye's reserved player number, duplicated from tournaments.models rather
# than imported: this module is deliberately free of module-level Django
# dependencies. test_pairing_base pins the two together.
BYE_PLAYER_NUMBER = "BYE"

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
    # ``key`` is the identity (the player number); ``name`` is for humans only
    # — display, error messages, log lines. Two entrants may legitimately share
    # a name, so nothing may key on it. See plans/PLAN_PLAYER_IDENTITY.md.
    key: str
    name: str
    # The *pinned* rating, from the entrant, not the live one on the player.
    # Seeding reads this so a rating that drifts mid-tournament — a WESPA pull,
    # a roster sync — cannot reshuffle rounds that are already being played
    # (plans/PLAN_ENTRANTS.md decision 3).
    rating: int

    @classmethod
    def from_entrant(cls, entrant) -> "PlayerData":
        return cls(
            key=entrant.player.player_number,
            name=entrant.player.name,
            rating=entrant.rating,
        )


@dataclass
class EntrantData:
    player: PlayerData
    # A dropped entrant keeps their played results (they still count for
    # opponents) but is never paired again.
    dropped: bool = False


@dataclass
class ResultSlipData:
    # Keys, not names — the fields are spelled ``_key`` so nothing renders one
    # by accident.
    round: int
    winner_key: str
    loser_key: str
    winner_score: int
    loser_score: int
    winner_started: bool

    @classmethod
    def from_db(cls, r) -> "ResultSlipData":
        return cls(
            round=r.round,
            winner_key=r.winner_key,
            loser_key=r.loser_key,
            winner_score=r.winner_score,
            loser_score=r.loser_score,
            winner_started=r.winner_started,
        )

    @property
    def first_key(self) -> str:
        """Key of the player who went first."""
        return self.winner_key if self.winner_started else self.loser_key

    @property
    def second_key(self) -> str:
        """Key of the player who went second."""
        return self.loser_key if self.winner_started else self.winner_key


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

    # Fixed pairings keyed by round number. Each entry is a list of unordered
    # (key1, key2) pairs that must be matched regardless of what the pairing
    # strategy would choose.
    fixed_pairings: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    # Already-published pairings of every non-draft round, keyed by round number,
    # each stored as (first, second) — the orientation the players were handed.
    # The engine replays them into its repeat/start ledger, so a published round's
    # games and first/second assignments survive a regeneration even before any
    # result is entered; the round-robin solver additionally pins them, so an
    # in-progress round's printed-but-unplayed games are never recomputed or
    # duplicated elsewhere. Draft rounds are absent — they are free to re-pair.
    # Empty by default, so callers that build PairingData by hand (tests) need
    # not supply it.
    published_pairings: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    # Players (by key) sitting a round out entirely, keyed by round number: paired into no
    # game and given no bye, but not withdrawn either. Playoff participants are
    # reserved this way for the rounds their bracket owns, so the ordinary field
    # keeps pairing around them. Empty for a division with no playoff.
    inactive_players: dict[int, list[str]] = field(default_factory=dict)

    # Temporary filter used by pair_round() while invoking a strategy for a round that has
    # fixed pairings. pair_round() sets this to the keys of all fixed players before calling
    # the strategy, so that standings_after_round() omits them and the strategy only sees the
    # remaining entrants. pair_round() clears it again before returning.
    #
    # Strategies call standings_after_round(pd, ...) directly as a module-level function, so
    # this field is the least-invasive way to communicate the exclusion set to them without
    # modifying every strategy individually.
    excluded_keys: set[str] = field(default_factory=set)

    # Seed for the engine's random strategies. Carried into the Rust engine's
    # input; unused by the Python engine (which uses the global RNG).
    seed: int = 0

    # COP prize/tuning config (DivisionSettings.cop_config), passed through to the
    # engine when a round uses the COP strategy. Empty/None otherwise.
    cop_config: dict | None = None

    # Swiss tuning (DivisionSettings.swiss_config): swiss_weight, max_distance,
    # spr_split. None means the engine's built-in defaults.
    swiss_config: dict | None = None

    @classmethod
    def for_division(cls, division) -> "PairingData":
        # select_related the joined rows every DTO touches: without it, each
        # e.player / slip.winner.player.name is a separate query — an N+1 that
        # dominated for_division for finished divisions (hundreds of slips).
        # Ordered explicitly, because ``seedings`` below sorts these by rating
        # *stably* and so inherits this order as its tiebreak. It was relying on
        # ``Entrant.Meta.ordering`` to be ["number"] — true, but nothing said so,
        # and a change there would have quietly moved who gets paired with whom
        # when two entrants share a rating.
        entrants = [
            EntrantData(PlayerData.from_entrant(e), dropped=e.dropped)
            for e in division.entrants.select_related("player").order_by("number")
        ]
        slips = [
            ResultSlipData.from_db(r)
            for r in division.result_slips.select_related(
                "winner__player", "loser__player"
            )
        ]
        fixed: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for fp in division.fixed_pairings.select_related("entrant1__player", "entrant2__player").all():
            fixed[fp.round_number].append(
                (fp.entrant1.player.player_number, fp.entrant2.player.player_number)
            )
        # Published pairings of non-draft rounds: the engine replays them into its
        # start ledger and the round-robin solver pins them, so an in-progress
        # round's printed-but-unplayed games are honored. Draft rounds are excluded
        # (free to re-pair); null-round_pairings rows are ignored. Imported locally
        # to keep this module free of module-level Django model dependencies.
        from tournaments.models import RoundPairings

        published: dict[int, list[tuple[str, str]]] = defaultdict(list)
        published_qs = division.pairings.filter(
            round_pairings__status__in=[
                RoundPairings.PUBLISHED,
                RoundPairings.IN_PROGRESS,
                RoundPairings.FINISHED,
            ]
        ).select_related("first__player", "second__player")
        for p in published_qs:
            first, second = p.first.player, p.second.player
            # A bye row is stored real-player-first for readability, the opposite
            # of the ledger's convention that the bye opponent is the notional
            # starter. Put it back, or replaying the round would charge the byed
            # player a start they never took.
            if second.is_bye:
                first, second = second, first
            published[p.round].append((first.player_number, second.player_number))
        # A division with no settings row yet has no configured pairings.
        # Malformed blobs are rejected at write time (_validate_blocks), so a
        # missing row is the only thing to tolerate here. Imported locally to
        # keep this module free of module-level Django model dependencies.
        from tournaments.models import DivisionSettings

        try:
            settings = division.settings
            raw_rps = settings.round_pairings or []
            seed = settings.pairing_seed
            cop_config = settings.cop_config or None
            swiss_config = settings.swiss_config or None
        except DivisionSettings.DoesNotExist:
            raw_rps = []
            seed = 0
            cop_config = None
            swiss_config = None
        rps = [RoundPairing.from_dict(x) for x in raw_rps]
        normalize_round_robin_start_rounds(rps)
        return cls(
            result_slips=slips,
            entrants=entrants,
            repeats=Repeats(),
            fixed_pairings=dict(fixed),
            published_pairings=dict(published),
            round_pairings=rps,
            seed=seed,
            cop_config=cop_config,
            swiss_config=swiss_config,
        )


class PairingError(Exception):
    """Raised when a round cannot be paired as configured — e.g. a set of fixed
    pairings that can't all be satisfied by a round-robin slot assignment. The
    message is surfaced to the organiser."""


class DefaultDict(defaultdict):
    """A defaultdict that passes the missing key to the factory.

    e.g. DefaultDict(Player) will call Player(key) for missing keys,
    so players["0233"] creates Player("0233").
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
    """A player in the standings, identified by ``key`` and shown as ``name``.

    Both are carried because both are needed: the pairing machinery groups,
    de-duplicates and looks up on ``key`` (two players may share a name), while
    templates and exports render ``name``. A Player built from results alone
    has no name to hand, so ``name`` falls back to the key — a visibly wrong
    display rather than a silently blank one.
    """

    key: str
    name: str = ""
    wins: int = 0
    losses: int = 0
    ties: int = 0
    score: float = 0
    spread: int = 0
    starts: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.key

    @property
    def is_bye(self) -> bool:
        # The bye is identified by its reserved *number*, matching the engine's
        # case-insensitive compare in scrabble-pairing/src/standings.rs.
        return self.key.casefold() == BYE_PLAYER_NUMBER.casefold()

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
    key: str
    score: int
    opp_score: int
    start: bool

    @property
    def spread(self) -> int:
        return self.score - self.opp_score

    @classmethod
    def from_result_slip(cls, result_slip, winner) -> "Result":
        if winner:
            key = result_slip.winner_key
            score = result_slip.winner_score
            opp_score = result_slip.loser_score
            started = result_slip.winner_started
        else:
            key = result_slip.loser_key
            score = result_slip.loser_score
            opp_score = result_slip.winner_score
            started = not result_slip.winner_started
        return cls(key, score, opp_score, started)


@dataclass
class Results:
    players: dict[str, Player] = field(default_factory=lambda: DefaultDict(Player))
    rounds: dict[int, list[ResultSlipData]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # key -> display name. Result slips carry keys only, so without this the
    # standings would render numbers. Missing entries fall back to the key.
    names: dict[str, str] = field(default_factory=dict)

    def update_player(self, result) -> None:
        p = self.players[result.key]
        if p.name == p.key and result.key in self.names:
            p.name = self.names[result.key]
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
        return tuple(sorted([p.first.key, p.second.key]))

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

    def _record(self, key1, key2, round, p1_starts) -> None:
        if p1_starts:
            self.starts[key1] += 1
            self.recent_starts[key1] = round
            self.h2h[(key1, key2)] = True
            self.h2h[(key2, key1)] = False
        else:
            self.starts[key2] += 1
            self.recent_starts[key2] = round
            self.h2h[(key1, key2)] = False
            self.h2h[(key2, key1)] = True

    def register(self, p: Pairing, round: int) -> None:
        """Record a known start from a finished round."""
        self._record(p.first.key, p.second.key, round, True)

    def add(self, p: Pairing, round: int) -> Pairing:
        """Decide who starts and record the result. Returns Pairing(first, second)."""
        key1, key2 = p.first.key, p.second.key
        if p.first.is_bye:
            p1_starts = True
        elif p.second.is_bye:
            p1_starts = False
        elif self.fixed_starts.get((round, key1)):
            p1_starts = True
        elif self.fixed_starts.get((round, key2)):
            p1_starts = False
        else:
            starts1 = self.starts[key1]
            starts2 = self.starts[key2]
            if starts1 == starts2:
                # Whoever went first most recently should go second now.
                if (key1, key2) not in self.h2h:
                    p1_starts = self.recent_starts[key1] <= self.recent_starts[key2]
                else:
                    p1_starts = not self.h2h[(key1, key2)]
            else:
                p1_starts = starts1 < starts2
        self._record(key1, key2, round, p1_starts)
        return p if p1_starts else Pairing(p.second, p.first)


class Pairings:
    pairings: list[Pairing]

    def __init__(self):
        self.pairings = []

    def add(self, player1: Player, player2: Player) -> None:
        self.pairings.append(Pairing(player1, player2))

    def add_result_slip(self, r: ResultSlipData) -> None:
        winner = Player(r.winner_key)
        loser = Player(r.loser_key)
        if r.winner_started:
            self.add(winner, loser)
        else:
            self.add(loser, winner)

    def __iter__(self):
        return iter(self.pairings)

    def __len__(self) -> int:
        return len(self.pairings)


def results_after_round(pd: PairingData, round: int) -> Results:
    res = Results(names={e.player.key: e.player.name for e in pd.entrants})
    for r in pd.result_slips:
        if r.round <= round:
            res.add_result(r)
    return res


Standings = list[Player]


def seedings(pd: PairingData, include_dropped: bool = False) -> Standings:
    """The field in seeding order: by pinned rating, then as handed to us.

    This is the third place the phrase "seed order" appears, and the one that
    cannot say it the same way as the others. ``Entrant.seeding_for`` breaks a
    tie on the player number and ``entrants_for_display`` reads the stored
    entrant number, but neither number reaches this layer — ``EntrantData``
    carries a key, a name and a rating, because the engine deliberately pairs
    off none of the rest.

    So the tiebreak here is the order ``PairingData`` was built in, which
    ``for_division`` now sets explicitly to the stored seeding. The sort is
    stable, so equal ratings keep it.
    """
    # Dropped entrants are unpairable, so they never seed a round (the pairing
    # path leaves include_dropped False); the standings *display* passes True to
    # keep showing them.
    entrants = [e for e in pd.entrants if include_dropped or not e.dropped]
    entrants.sort(key=lambda x: -x.player.rating)
    return [Player(e.player.key, e.player.name) for e in entrants]


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
            dropped = {e.player.key for e in pd.entrants if e.dropped}
            if dropped:
                s = [p for p in s if p.key not in dropped]
        # A late entrant has no result slips yet, so it never appears in
        # results-derived standings and would silently never be paired. Append
        # each recordless entrant as a zero record, in seeding (rating) order
        # among themselves, at the bottom of the field.
        present = {p.key for p in s}
        newcomers = [
            e
            for e in pd.entrants
            if e.player.key not in present and (include_dropped or not e.dropped)
        ]
        newcomers.sort(key=lambda e: -e.player.rating)
        s = s + [Player(e.player.key, e.player.name) for e in newcomers]
    # The bye is never a competitor: it must not appear in any pairing field or
    # in displayed standings. (It is added back as a forced pairing for an odd
    # field — see pair_round.)
    s = [p for p in s if not p.is_bye]
    if pd.excluded_keys:
        s = [p for p in s if p.key not in pd.excluded_keys]
    return s
