"""Playoff bracket derivation, plus the thin ORM-facing lifecycle around it.

The top half of this module is pure — no ORM, no writes — in the same spirit as
``tournaments/pairing/base.py``. The bottom half ("Lifecycle") is the small set
of functions that read or write the database: taking the qualification snapshot,
keeping ``PlayoffSeries`` rows in step with the derived bracket, dropping games a
clinch has made unnecessary, and guarding a result edit that would rewrite a
bracket whose later games are already played.

## The derivation

The bracket is *derived*, never stored (see ``plans/PLAN_PLAYOFFS.md``). Its only
inputs are a playoff's configuration plus its confirmed seed snapshot — the
intent recorded in the event log — and the division's result slips. Everything
else (who is in which series, the series score, which games still need playing,
who finished third) is computed here. So a corrected result recomputes the whole
bracket, an unnecessary game is never generated in the first place, and a replay
reproduces the bracket by construction.

``build_bracket`` is the single entry point. ``final_placements`` turns a bracket
plus ordinary standings into the division's finishing order.
"""

from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Timing(StrEnum):
    """When the playoff runs relative to the main tournament."""

    # The main event ends at the qualification round; only playoff participants
    # play the reserved rounds.
    POSTSCRIPT = "postscript"
    # Everyone else continues the configured schedule while the bracket runs.
    CONCURRENT = "concurrent"


# Series keys. A key plus a 0-based position identifies a series within a
# playoff, and is what ``PlayoffSeries`` rows and ``Pairing.series`` hang off.
QUARTERFINAL = "quarterfinal"
SEMIFINAL = "semifinal"
CONSOLATION_SEMIFINAL = "consolation_semifinal"
CHAMPIONSHIP = "championship"
THIRD_PLACE = "third_place"
FIFTH_PLACE = "fifth_place"
SEVENTH_PLACE = "seventh_place"

SERIES_LABELS = {
    QUARTERFINAL: "Quarterfinal",
    SEMIFINAL: "Semifinal",
    CONSOLATION_SEMIFINAL: "5th–8th semifinal",
    CHAMPIONSHIP: "Championship",
    THIRD_PLACE: "Third place",
    FIFTH_PLACE: "Fifth place",
    SEVENTH_PLACE: "Seventh place",
}


# ---------------------------------------------------------------------------
# Bracket templates
# ---------------------------------------------------------------------------

# A participant source: a qualification seed, or the winner/loser of an earlier
# series. Everything about advancement lives in these templates — there is no
# advancement logic anywhere else.


@dataclass(frozen=True)
class Seed:
    """The nth qualifier (1-based) from the confirmed seed snapshot."""

    n: int


@dataclass(frozen=True)
class Winner:
    key: str
    position: int = 0


@dataclass(frozen=True)
class Loser:
    key: str
    position: int = 0


Source = Seed | Winner | Loser


@dataclass(frozen=True)
class SeriesSpec:
    """One series in a bracket template.

    ``a``/``b`` are its two participant sources; which of the two is the *high*
    seed is resolved at derivation time (a third-place series' participants
    aren't known until the semifinals are). ``places`` marks a terminal series:
    the places its winner and loser take. ``placement`` marks a series that may
    be turned off in postscript mode, and ``requires`` chains that off-switch
    (disabling the consolation semifinals disables 5th and 7th place with them).
    """

    key: str
    position: int
    a: Source
    b: Source
    places: tuple[int, int] | None = None
    placement: bool = False
    requires: str | None = None


# qualifier count -> ordered windows, each a list of series played in parallel.
BRACKETS: dict[int, list[list[SeriesSpec]]] = {
    2: [
        [SeriesSpec(CHAMPIONSHIP, 0, Seed(1), Seed(2), places=(1, 2))],
    ],
    4: [
        [
            SeriesSpec(SEMIFINAL, 0, Seed(1), Seed(4)),
            SeriesSpec(SEMIFINAL, 1, Seed(2), Seed(3)),
        ],
        [
            SeriesSpec(
                CHAMPIONSHIP,
                0,
                Winner(SEMIFINAL, 0),
                Winner(SEMIFINAL, 1),
                places=(1, 2),
            ),
            SeriesSpec(
                THIRD_PLACE,
                0,
                Loser(SEMIFINAL, 0),
                Loser(SEMIFINAL, 1),
                places=(3, 4),
                placement=True,
            ),
        ],
    ],
    8: [
        [
            SeriesSpec(QUARTERFINAL, 0, Seed(1), Seed(8)),
            SeriesSpec(QUARTERFINAL, 1, Seed(4), Seed(5)),
            SeriesSpec(QUARTERFINAL, 2, Seed(2), Seed(7)),
            SeriesSpec(QUARTERFINAL, 3, Seed(3), Seed(6)),
        ],
        [
            SeriesSpec(
                SEMIFINAL,
                0,
                Winner(QUARTERFINAL, 0),
                Winner(QUARTERFINAL, 1),
            ),
            SeriesSpec(
                SEMIFINAL,
                1,
                Winner(QUARTERFINAL, 2),
                Winner(QUARTERFINAL, 3),
            ),
            # The consolation half: quarterfinal losers keep playing, which is
            # what makes 5th–8th bracket-decided and, in concurrent mode, keeps
            # every reserved player occupied in every window.
            SeriesSpec(
                CONSOLATION_SEMIFINAL,
                0,
                Loser(QUARTERFINAL, 0),
                Loser(QUARTERFINAL, 1),
                placement=True,
            ),
            SeriesSpec(
                CONSOLATION_SEMIFINAL,
                1,
                Loser(QUARTERFINAL, 2),
                Loser(QUARTERFINAL, 3),
                placement=True,
            ),
        ],
        [
            SeriesSpec(
                CHAMPIONSHIP,
                0,
                Winner(SEMIFINAL, 0),
                Winner(SEMIFINAL, 1),
                places=(1, 2),
            ),
            SeriesSpec(
                THIRD_PLACE,
                0,
                Loser(SEMIFINAL, 0),
                Loser(SEMIFINAL, 1),
                places=(3, 4),
                placement=True,
            ),
            SeriesSpec(
                FIFTH_PLACE,
                0,
                Winner(CONSOLATION_SEMIFINAL, 0),
                Winner(CONSOLATION_SEMIFINAL, 1),
                places=(5, 6),
                placement=True,
                requires=CONSOLATION_SEMIFINAL,
            ),
            SeriesSpec(
                SEVENTH_PLACE,
                0,
                Loser(CONSOLATION_SEMIFINAL, 0),
                Loser(CONSOLATION_SEMIFINAL, 1),
                places=(7, 8),
                placement=True,
                requires=CONSOLATION_SEMIFINAL,
            ),
        ],
    ],
}

QUALIFIER_COUNTS = tuple(sorted(BRACKETS))


def series_keys(qualifier_count: int) -> list[str]:
    """Every series key in a bracket's template, in window order."""
    keys = []
    for window in BRACKETS[qualifier_count]:
        for spec in window:
            if spec.key not in keys:
                keys.append(spec.key)
    return keys


def placement_keys(qualifier_count: int) -> list[str]:
    """The series keys that may be switched off (postscript only)."""
    return [
        spec.key
        for window in BRACKETS[qualifier_count]
        for spec in window
        if spec.placement
    ]


def series_label(key: str, position: int = 0, game_number: int | None = None) -> str:
    """Human label for a series (optionally one of its games): "Semifinal 2,
    game 3". Takes plain values so it works for a ``PlayoffSeries`` row as well
    as a derived ``Series``."""
    label = SERIES_LABELS.get(key, key)
    if key in (QUARTERFINAL, SEMIFINAL, CONSOLATION_SEMIFINAL):
        label = f"{label} {position + 1}"
    if game_number is not None:
        label = f"{label}, game {game_number}"
    return label


def default_stage_games(qualifier_count: int, games: int = 3) -> dict[str, int]:
    """Every series in the bracket at ``games`` apiece — the starting point the
    setup form offers, with every placement series enabled."""
    return {key: games for key in series_keys(qualifier_count)}


@dataclass(frozen=True)
class PlayoffConfig:
    """The recorded intent: what the director configured and confirmed."""

    qualification_round: int
    qualifier_count: int
    timing: str
    # series key -> maximum games. A placement key that is missing or 0 means
    # that series is not played.
    stage_games: dict[str, int]
    # Confirmed qualifiers, best seed first.
    seeds: tuple[str, ...]

    @classmethod
    def from_model(cls, playoff) -> "PlayoffConfig":
        """Build from a ``Playoff`` row (duck-typed, as ``PairingData`` is)."""
        return cls(
            qualification_round=playoff.qualification_round,
            qualifier_count=playoff.qualifier_count,
            timing=playoff.timing,
            stage_games=dict(playoff.stage_games or {}),
            seeds=tuple(s["player"] for s in playoff.seeds or []),
        )

    def seed_of(self, name: str) -> int | None:
        """1-based qualification seed, or None for a non-participant."""
        try:
            return self.seeds.index(name) + 1
        except ValueError:
            return None


def validate_config(config: PlayoffConfig) -> list[str]:
    """Human-readable problems with a configuration; empty means valid.

    Used by the setup form and the create command, so the same rules apply
    whether a playoff arrives from the UI or from a replayed event.
    """
    errors = []
    if config.qualifier_count not in BRACKETS:
        return [
            f"Qualifier count must be one of {', '.join(map(str, QUALIFIER_COUNTS))}."
        ]
    if config.qualification_round < 1:
        errors.append("The qualification round must be at least 1.")
    if config.timing not in tuple(Timing):
        errors.append(f"Unknown timing mode {config.timing!r}.")
    if len(config.seeds) != config.qualifier_count:
        errors.append(
            f"Expected {config.qualifier_count} qualifiers, got {len(config.seeds)}."
        )
    if len(set(config.seeds)) != len(config.seeds):
        errors.append("A player cannot be qualified twice.")
    enabled = enabled_keys(config)
    for key in series_keys(config.qualifier_count):
        games = int(config.stage_games.get(key) or 0)
        if key not in enabled:
            continue
        if games < 1:
            errors.append(f"{SERIES_LABELS[key]} needs at least one game.")
        elif games % 2 == 0:
            errors.append(
                f"{SERIES_LABELS[key]} must be an odd number of games, not {games}."
            )
    if config.timing == Timing.CONCURRENT:
        missing = [
            SERIES_LABELS[key]
            for key in placement_keys(config.qualifier_count)
            if key not in enabled
        ]
        if missing:
            errors.append(
                "A concurrent playoff must play every placement series, or the "
                "players it would have covered are the only ones in the room "
                f"with no game: {', '.join(missing)}."
            )
    return errors


def enabled_keys(config: PlayoffConfig) -> set[str]:
    """Series keys actually played under this configuration.

    Non-placement series are always played. A placement series is played when it
    has a positive game count and whatever it requires is itself played.
    """
    enabled = set()
    for window in BRACKETS[config.qualifier_count]:
        for spec in window:
            if not spec.placement:
                enabled.add(spec.key)
                continue
            if int(config.stage_games.get(spec.key) or 0) < 1:
                continue
            if spec.requires and spec.requires not in enabled:
                continue
            enabled.add(spec.key)
    return enabled


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


class GameStatus(StrEnum):
    PLAYED = "played"
    # Needs a pairing: the series is live and this game is certainly necessary.
    SCHEDULED = "scheduled"
    # An earlier game is unplayed, so whether this one is needed isn't known yet.
    PENDING = "pending"
    # The series is already decided; this game will never be played.
    NOT_NEEDED = "not_needed"


class SeriesStatus(StrEnum):
    # Participants not yet known (an upstream series is unresolved).
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    CLINCHED = "clinched"
    # All games played, no majority, series spread level. Needs a director.
    TIED = "tied"


@dataclass(frozen=True)
class Game:
    """One game of a series, played or not."""

    number: int
    round: int
    status: str
    high_score: int | None = None
    low_score: int | None = None
    winner: str | None = None

    @property
    def played(self) -> bool:
        return self.status == GameStatus.PLAYED

    @property
    def tied(self) -> bool:
        return self.played and self.high_score == self.low_score


@dataclass(frozen=True)
class Series:
    """A series' fully derived state. Nothing here is ever written to the DB."""

    key: str
    position: int
    max_games: int
    start_round: int
    window: int
    places: tuple[int, int] | None
    # Participant names, better qualification seed first. None until known.
    high: str | None
    low: str | None
    games: tuple[Game, ...]
    high_score: float
    low_score: float
    # Cumulative spread over this series' games only, from ``high``'s side.
    high_spread: int
    status: str
    winner: str | None
    loser: str | None
    # "majority" or "spread" once decided, else None.
    decided_by: str | None

    @property
    def wins_required(self) -> float:
        """The score a player must exceed to clinch. Exposed for display: a
        best-of-3 shows "first to 2", i.e. the first whole number above this."""
        return self.max_games / 2

    @property
    def label(self) -> str:
        return series_label(self.key, self.position)

    @property
    def decided(self) -> bool:
        return self.winner is not None

    @property
    def participants(self) -> tuple[str, ...]:
        return tuple(n for n in (self.high, self.low) if n)

    @property
    def scheduled_games(self) -> tuple[Game, ...]:
        return tuple(g for g in self.games if g.status == GameStatus.SCHEDULED)

    @property
    def played_games(self) -> tuple[Game, ...]:
        return tuple(g for g in self.games if g.played)

    def score_for(self, name: str) -> float:
        return self.high_score if name == self.high else self.low_score


@dataclass(frozen=True)
class Window:
    """A stage window: the contiguous block of rounds a set of series plays in."""

    index: int
    start_round: int
    length: int

    @property
    def rounds(self) -> range:
        return range(self.start_round, self.start_round + self.length)


@dataclass(frozen=True)
class Bracket:
    config: PlayoffConfig
    series: tuple[Series, ...]
    windows: tuple[Window, ...]

    def get(self, key: str, position: int = 0) -> Series | None:
        for s in self.series:
            if s.key == key and s.position == position:
                return s
        return None

    @property
    def rounds(self) -> range:
        """Every round this playoff reserves."""
        start = self.config.qualification_round + 1
        total = sum(w.length for w in self.windows)
        return range(start, start + total)

    @property
    def complete(self) -> bool:
        """Every played-out series is decided and no game remains to schedule."""
        return all(s.status == SeriesStatus.CLINCHED for s in self.series if s.places)

    def window_of(self, round_num: int) -> Window | None:
        for w in self.windows:
            if round_num in w.rounds:
                return w
        return None

    def scheduled_by_round(self) -> dict[int, list[tuple[Series, Game]]]:
        """``{round: [(series, game), …]}`` for every game that needs a pairing.

        This is the whole contract with the pairing generator: it creates
        exactly these games and nothing else.
        """
        out: dict[int, list[tuple[Series, Game]]] = {}
        for s in self.series:
            for g in s.scheduled_games:
                out.setdefault(g.round, []).append((s, g))
        return out

    def reserved_names_by_round(self) -> dict[int, list[str]]:
        """``{round: [names]}`` of players held out of ordinary pairing.

        A player pulled into the bracket never returns to the ordinary pairing
        pool (see the plan's decisions), so every qualifier is reserved for every
        playoff round, whether or not they have a game that round.
        """
        names = list(self.config.seeds)
        return {r: list(names) for r in self.rounds}


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _result_index(slips) -> dict:
    """``{(round, {name, name}): slip}``. A best-of-N meets the same pair more
    than once, but always in different rounds, so this stays unique."""
    index = {}
    for slip in slips:
        key = (slip.round, frozenset({slip.winner_name, slip.loser_name}))
        index[key] = slip
    return index


def _resolve(source: Source, config: PlayoffConfig, done: dict) -> str | None:
    """The player a participant source points at, or None if not yet known."""
    match source:
        case Seed(n=n):
            return config.seeds[n - 1] if n <= len(config.seeds) else None
        case Winner(key=key, position=position):
            series = done.get((key, position))
            return series.winner if series else None
        case Loser(key=key, position=position):
            series = done.get((key, position))
            return series.loser if series else None
    return None


def _order_by_seed(config: PlayoffConfig, a: str | None, b: str | None):
    """(high, low) by qualification seed. Either may be None."""
    if a is None or b is None:
        return (a, b) if a is not None else (b, a)
    seed_a = config.seed_of(a) or len(config.seeds) + 1
    seed_b = config.seed_of(b) or len(config.seeds) + 1
    return (a, b) if seed_a <= seed_b else (b, a)


def _game_scores(slip, high: str) -> tuple[int, int, str | None]:
    """(high_score, low_score, winner or None for a draw) for one played game."""
    if slip.winner_name == high:
        high_score, low_score = slip.winner_score, slip.loser_score
    else:
        high_score, low_score = slip.loser_score, slip.winner_score
    if high_score == low_score:
        return high_score, low_score, None
    return high_score, low_score, high if high_score > low_score else slip.winner_name


def _series_state(
    spec: SeriesSpec,
    config: PlayoffConfig,
    window: Window,
    results: dict,
    done: dict,
) -> Series:
    max_games = max(int(config.stage_games.get(spec.key) or 0), 1)
    high, low = _order_by_seed(
        config,
        _resolve(spec.a, config, done),
        _resolve(spec.b, config, done),
    )

    def make(**derived) -> Series:
        return Series(
            key=spec.key,
            position=spec.position,
            max_games=max_games,
            start_round=window.start_round,
            window=window.index,
            places=spec.places,
            high=high,
            low=low,
            **derived,
        )

    if high is None or low is None:
        return make(
            games=(),
            high_score=0.0,
            low_score=0.0,
            high_spread=0,
            status=SeriesStatus.PENDING,
            winner=None,
            loser=None,
            decided_by=None,
        )

    # Walk the games in order, accumulating the score, and decide each one's
    # status from what is known at that point.
    threshold = max_games / 2
    played: dict[int, tuple[int, int, str | None]] = {}
    for number in range(1, max_games + 1):
        slip = results.get((window.start_round + number - 1, frozenset({high, low})))
        if slip is not None:
            played[number] = _game_scores(slip, high)

    def scores_before(number):
        """(high, low) series score from the games before ``number`` that have
        been played, and how many of those are still unplayed."""
        high_score = low_score = 0.0
        unplayed = 0
        for i in range(1, number):
            if i not in played:
                unplayed += 1
                continue
            _, _, winner = played[i]
            if winner is None:
                high_score += 0.5
                low_score += 0.5
            elif winner == high:
                high_score += 1
            else:
                low_score += 1
        return high_score, low_score, unplayed

    games = []
    for number in range(1, max_games + 1):
        round_num = window.start_round + number - 1
        if number in played:
            high_score, low_score, winner = played[number]
            games.append(
                Game(
                    number=number,
                    round=round_num,
                    status=GameStatus.PLAYED,
                    high_score=high_score,
                    low_score=low_score,
                    winner=winner,
                )
            )
            continue
        before_high, before_low, unplayed = scores_before(number)
        if before_high > threshold or before_low > threshold:
            # Already decided on the games played before this one.
            status = GameStatus.NOT_NEEDED
        elif before_high + unplayed > threshold or before_low + unplayed > threshold:
            # An earlier game is unplayed and could still end the series, so
            # whether this game is needed isn't knowable yet.
            status = GameStatus.PENDING
        else:
            # Nobody can have clinched before this game however the earlier
            # games went: it is certainly necessary. (With nothing played, this
            # covers games 1…wins_required, so a best-of-3's first two games can
            # be printed up front.)
            status = GameStatus.SCHEDULED
        games.append(Game(number=number, round=round_num, status=status))

    high_score, low_score, _ = scores_before(max_games + 1)
    high_spread = sum(played[i][0] - played[i][1] for i in played)

    winner = loser = decided_by = None
    if high_score > threshold:
        winner, loser, decided_by = high, low, "majority"
    elif low_score > threshold:
        winner, loser, decided_by = low, high, "majority"
    elif len(played) == max_games:
        # Every game played and no majority (only reachable with a drawn game).
        # Decided on spread within this series only — never tournament-wide
        # spread, and never a game outside the series.
        if high_spread > 0:
            winner, loser, decided_by = high, low, "spread"
        elif high_spread < 0:
            winner, loser, decided_by = low, high, "spread"

    if winner is not None:
        status = SeriesStatus.CLINCHED
    elif len(played) == max_games:
        # Level on games and on series spread — a drawn one-game series always
        # lands here. Nothing advances until the director schedules a decider.
        status = SeriesStatus.TIED
    elif played:
        status = SeriesStatus.IN_PROGRESS
    else:
        status = SeriesStatus.SCHEDULED

    return make(
        games=tuple(games),
        high_score=high_score,
        low_score=low_score,
        high_spread=high_spread,
        status=status,
        winner=winner,
        loser=loser,
        decided_by=decided_by,
    )


def build_bracket(config: PlayoffConfig, slips) -> Bracket:
    """Derive the whole bracket from the configuration and the division's
    results. ``slips`` is any iterable of objects with ``round``, ``winner_name``,
    ``loser_name``, ``winner_score`` and ``loser_score`` — both ``ResultSlipData``
    and the ``ResultSlip`` model qualify."""
    template = BRACKETS[config.qualifier_count]
    enabled = enabled_keys(config)
    results = _result_index(slips)

    done: dict[tuple[str, int], Series] = {}
    ordered: list[Series] = []
    windows: list[Window] = []
    start_round = config.qualification_round + 1

    for index, specs in enumerate(template):
        specs = [spec for spec in specs if spec.key in enabled]
        length = max(
            (max(int(config.stage_games.get(s.key) or 0), 1) for s in specs),
            default=0,
        )
        window = Window(index=index, start_round=start_round, length=length)
        for spec in specs:
            series = _series_state(spec, config, window, results, done)
            done[(spec.key, spec.position)] = series
            ordered.append(series)
        windows.append(window)
        start_round += length

    return Bracket(config=config, series=tuple(ordered), windows=tuple(windows))


# ---------------------------------------------------------------------------
# Final placements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    place: int
    # None when the place is not yet decided.
    name: str | None
    # "series" (won on the bracket), "seed" (eliminated at the same stage as
    # others and separated by qualification order), "standings" (main field),
    # or "unresolved".
    source: str
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.name is not None


def final_placements(bracket: Bracket, standings, numbers) -> list[Placement]:
    """The division's finishing order: bracket first, then the main field.

    ``standings`` is the ordinary standings after the division's last round
    (``standings_after_round``); ``numbers`` maps player name -> entrant number.

    Ordinary standings sort on wins then spread and stop there, so an exact tie
    falls back to result-slip iteration order. The main-field tail therefore
    breaks such ties on entrant number, making this a total order. That tiebreak
    lives *here* and not in ``standings_after_round``, which feeds the state
    digest — reordering exact ties there would perturb digests already recorded
    for existing tournaments.
    """
    config = bracket.config
    placements: dict[int, Placement] = {}
    owned: set[int] = set()
    placed_names: set[str] = set()

    for series in bracket.series:
        if not series.places:
            continue
        win_place, lose_place = series.places
        owned.update(series.places)
        if series.decided:
            placements[win_place] = Placement(win_place, series.winner, "series")
            placements[lose_place] = Placement(lose_place, series.loser, "series")
            placed_names.update(series.participants)
        else:
            # Never seed-order an undecided series: that would present an
            # unfinished bracket as a finished one.
            note = (
                "tied — needs a decider"
                if series.status == SeriesStatus.TIED
                else "not yet decided"
            )
            for place in series.places:
                placements[place] = Placement(
                    place, None, "unresolved", f"{series.label}: {note}"
                )

    # Bracket players no placement series covers (a postscript playoff that
    # switched some off): eliminated later ranks higher, then qualification seed.
    last_window = {}
    for series in bracket.series:
        for name in series.participants:
            last_window[name] = max(last_window.get(name, -1), series.window)
    leftover = [
        name
        for name in config.seeds
        if name not in placed_names
        and not any(
            name in s.participants and s.places and not s.decided
            for s in bracket.series
        )
    ]
    leftover.sort(key=lambda n: (-last_window.get(n, -1), config.seed_of(n)))
    free_places = [p for p in range(1, config.qualifier_count + 1) if p not in owned]
    for place, name in zip(free_places, leftover):
        placements[place] = Placement(
            place, name, "seed", "eliminated; placed by qualification order"
        )
        placed_names.add(name)

    ordered = [
        placements.get(p, Placement(p, None, "unresolved"))
        for p in range(1, config.qualifier_count + 1)
    ]

    # The main field, by wins then spread then entrant number.
    seeds = set(config.seeds)
    tail = [p for p in standings if p.name not in seeds]
    tail.sort(key=lambda p: (-p.score, -p.spread, numbers.get(p.name, 0)))
    for offset, player in enumerate(tail, start=config.qualifier_count + 1):
        ordered.append(Placement(offset, player.name, "standings"))
    return ordered


# ---------------------------------------------------------------------------
# Lifecycle (the ORM-facing half)
# ---------------------------------------------------------------------------


def playoff_for(division):
    """The division's playoff, or None.

    Always read fresh rather than through ``division.playoff``: the reverse
    one-to-one descriptor caches a *negative* lookup on the instance, so a
    division touched before its playoff was created would keep reporting that it
    has none for the rest of the request (or the rest of a test).
    """
    from tournaments.models import Playoff

    return Playoff.objects.filter(division=division).first()


def qualification_seeds(division, round_num, count):
    """The top ``count`` entrants in ``division`` after ``round_num``, as the
    seed snapshot a playoff records.

    This is what the setup page previews and what the director may override
    before confirming. Withdrawn entrants are not offered: a dropped player
    cannot play a series, and a bracket must never depend on one.
    """
    from tournaments.pairing.base import PairingData, standings_after_round

    pd = PairingData.for_division(division)
    standings = standings_after_round(pd, round_num)
    return [
        {
            "seed": i + 1,
            "player": p.name,
            "wins": p.score,
            "spread": p.spread,
        }
        for i, p in enumerate(standings[:count])
    ]


def selectable_qualification_rounds(division) -> list[int]:
    """Rounds a playoff may qualify on: every round the division has scheduled,
    plus any round that has results beyond it.

    The configured schedule is the point — a postscript playoff has to qualify on
    the *last* configured round, which is typically not played yet when the
    director sets the playoff up. Played rounds are unioned in so an imported
    division (results but no schedule) still offers something.
    """
    configured = set(division.configured_round_numbers(default=[]))
    return sorted(configured | set(range(1, division.max_round() + 1)))


def finished_rounds(division) -> set[int]:
    """Rounds whose pairings are all played."""
    from tournaments.models import RoundPairings

    return set(
        division.round_pairings_set.filter(status=RoundPairings.FINISHED).values_list(
            "round", flat=True
        )
    )


def schedule_conflicts(division, config: PlayoffConfig) -> list[str]:
    """Problems between a playoff configuration and the division's schedule.

    ``validate_config`` checks the playoff on its own terms; this adds the
    checks that need the division: that the qualification round exists and is
    complete, and that the reserved rounds line up with the configured schedule.
    """
    from tournaments.pairing.round_pairing import RP

    configured = division.configured_round_numbers(default=[])
    last_configured = max(configured, default=0)
    bracket = build_bracket(config, [])
    playoff_rounds = list(bracket.rounds)
    errors = []
    if last_configured and config.qualification_round > last_configured:
        errors.append(
            f"Round {config.qualification_round} is past the end of the "
            f"schedule (round {last_configured})."
        )
    elif config.qualification_round not in finished_rounds(division):
        # The seed snapshot taken here is frozen for the life of the bracket, so
        # it has to come from a round that is actually over. The round can still
        # be *chosen* before it is played — that is how a director plans ahead —
        # but confirming has to wait for the results.
        errors.append(
            f"Round {config.qualification_round} isn't finished yet. The "
            "playoffs can only be created once the qualifying round is done."
        )
    if config.timing == Timing.POSTSCRIPT:
        if last_configured > config.qualification_round:
            errors.append(
                "A postscript playoff has to start where the main tournament "
                f"ends: the schedule runs to round {last_configured}, so the "
                f"qualification round must be {last_configured}, not "
                f"{config.qualification_round}."
            )
    elif playoff_rounds and last_configured < playoff_rounds[-1]:
        errors.append(
            "A concurrent playoff needs the main schedule to cover its rounds: "
            f"the bracket runs to round {playoff_rounds[-1]} but the schedule "
            f"ends at round {last_configured}."
        )
    if config.timing == Timing.CONCURRENT and playoff_rounds:
        try:
            by_round = {
                rp["round"]: rp["pairing"] for rp in division.settings.round_pairings
            }
        except AttributeError, KeyError, TypeError:
            by_round = {}
        clashing = sorted(
            {
                by_round[r]
                for r in playoff_rounds
                if RP.is_round_robin(by_round.get(r, ""))
            }
        )
        if clashing:
            # A round-robin block is a fixed template over a fixed field; holding
            # players out of some of its rounds cannot be satisfied.
            errors.append(
                f"A concurrent playoff cannot overlap a {', '.join(clashing)} "
                "block — those rounds pair the whole field from a fixed template."
            )
    return errors


def sync_series(playoff, bracket):
    """Bring ``PlayoffSeries`` rows into line with the derived bracket.

    Structure only — participants, length and window start. Rows are upserted
    (never recreated) so a ``Pairing.series`` reference survives regeneration;
    rows for a series the configuration no longer plays are dropped, which can
    only happen while the playoff is still editable (i.e. before any result).

    Returns ``{(key, position): PlayoffSeries}`` for the pairing generator.
    """
    from tournaments.models import PlayoffSeries

    entrants = {
        e.player.name: e for e in playoff.division.entrants.select_related("player")
    }
    rows = {}
    for series in bracket.series:
        row, _ = PlayoffSeries.objects.update_or_create(
            playoff=playoff,
            key=series.key,
            position=series.position,
            defaults={
                "high": entrants.get(series.high) if series.high else None,
                "low": entrants.get(series.low) if series.low else None,
                "max_games": series.max_games,
                "start_round": series.start_round,
            },
        )
        rows[(series.key, series.position)] = row
    live = {(s.key, s.position) for s in bracket.series}
    for row in playoff.series.all():
        if (row.key, row.position) not in live:
            row.delete()
    return rows


def _series_game_status(bracket, key, position, game_number):
    """The derived status of one game, or None if the bracket has no such game."""
    series = bracket.get(key, position)
    if series is None:
        return None
    for game in series.games:
        if game.number == game_number:
            return game.status
    return None


def prune_unnecessary_pairings(division, bracket):
    """Delete playoff pairings the bracket no longer schedules.

    Generation never *creates* an unnecessary game, but a director may have
    published a whole window before an earlier game clinched the series. Such a
    pairing is removed outright — no result, no bye, no zero score — which is
    the one place this design deliberately parts company with tsh's
    zeroed-out partials. A game that has already been played is never touched.

    A playoff round can legitimately end up with no games at all (both semis
    clinched 2–0, so the window's last round has nothing left). That round is
    marked finished here rather than in ``RoundPairings.update_status``, whose
    ``total > 0`` guard exists to stop an *unpaired* round reading as finished
    and must keep doing so.
    """
    from tournaments.models import RoundPairings

    stale = []
    for pairing in division.pairings.filter(
        series__isnull=False, result__isnull=True
    ).select_related("series"):
        status = _series_game_status(
            bracket,
            pairing.series.key,
            pairing.series.position,
            pairing.game_number,
        )
        if status != GameStatus.SCHEDULED:
            stale.append(pairing)
    for pairing in stale:
        pairing.delete()

    # A round can only be closed once nothing can appear in it any more: a game
    # still ``pending`` might become necessary (a series that goes 1–1 needs its
    # decider), and closing the round early would strand it.
    open_rounds = {
        game.round
        for series in bracket.series
        for game in series.games
        if game.status in (GameStatus.SCHEDULED, GameStatus.PENDING)
    }
    for rp in division.round_pairings_set.filter(
        round__in=set(bracket.rounds) - open_rounds,
        status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS],
    ):
        if not rp.pairings.exists():
            rp.status = RoundPairings.FINISHED
            rp.save(update_fields=["status"])
    return stale


def refresh_after_results(division):
    """Drop playoff games the latest results have made unnecessary.

    Called wherever a result is written, alongside ``RoundPairings.update_status``.
    A no-op for a division without a playoff.
    """
    playoff = playoff_for(division)
    if playoff is None:
        return []
    return prune_unnecessary_pairings(division, playoff.bracket())


def conflicts_for_results(config: PlayoffConfig, slips) -> list[str]:
    """Results that the bracket implied by ``slips`` does not actually play.

    The guard against a destructive correction. Recompute the bracket from a
    *prospective* set of results: if some already-recorded game is not one the
    new bracket plays — because changing an earlier score sent someone else to
    the final — the edit would silently rewrite history, so it is refused.
    Unplayed downstream state needs no such protection; it is regenerated.

    Pure: ``slips`` is the complete prospective result set, duck-typed as
    elsewhere in this module. Empty means the edit is safe.
    """
    bracket = build_bracket(config, slips)
    rounds = set(bracket.rounds)
    expected = {
        (game.round, frozenset(series.participants))
        for series in bracket.series
        for game in series.games
        if game.played
    }
    conflicts = []
    for slip in slips:
        if slip.round not in rounds:
            continue
        key = (slip.round, frozenset({slip.winner_name, slip.loser_name}))
        if key not in expected:
            conflicts.append(
                f"Round {slip.round}: {slip.winner_name} vs {slip.loser_name} "
                f"is not a game this bracket plays. Changing an earlier result "
                f"would rewrite the bracket under games that have already been "
                f"played — delete those results first if that is intended."
            )
    return conflicts


def conflicts_for_single_result(
    division, pairing, winner_name, winner_score, loser_score
):
    """``conflicts_for_results`` for one about-to-be-saved result.

    Builds the prospective result set: every existing slip except this pairing's
    own (an edit replaces it), plus the new one.
    """
    from tournaments.pairing.base import ResultSlipData

    playoff = playoff_for(division)
    if playoff is None:
        return []
    names = {pairing.first.player.name, pairing.second.player.name}
    slips = [
        ResultSlipData.from_db(r)
        for r in division.result_slips.select_related("winner__player", "loser__player")
        if not (
            r.round == pairing.round
            and {r.winner.player.name, r.loser.player.name} == names
        )
    ]
    loser_name = next(iter(names - {winner_name}), winner_name)
    slips.append(
        ResultSlipData(
            round=pairing.round,
            winner_name=winner_name,
            loser_name=loser_name,
            winner_score=winner_score,
            loser_score=loser_score,
            winner_started=True,
        )
    )
    return conflicts_for_results(playoff.config(), slips)
