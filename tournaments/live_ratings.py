"""Project each entrant's rating from the games played so far.

Non-binding, and the reason is worth stating up front: ratings are official only
when the whole history is replayed in chronological order, so this projection is
stale the moment another tournament rates ahead of this one. It is a *preview* of
what this tournament will do to a player's rating, useful while the event is
running and never a substitute for the real run.

Pure: no storage, no network, no writes. It reads the division and returns
numbers. Baxter must be able to do this with no connection to the central
database, which is why every input is already frozen on the entrant
(``plans/PLAN_COCO_PROGRAM.md``).

The math is the *same code* the official run uses — ``coco_ratings.core``, which
Baxter depends on precisely so this cannot drift into a second implementation of
the Norwegian rating system. What this module does is assemble the inputs the
way the official replay assembles them, which is the part that is easy to get
subtly wrong:

- seed each player from the entrant's frozen snapshot;
- age their deviation to the tournament's start date, exactly as
  ``ratingsdb.adjust_tournament`` does — deviation grows with inactivity, and
  skipping it makes the projection systematically wrong for returning players;
- rate unrated players through ``calc_initial_ratings``, the same convergence
  loop, before rating anybody else.
"""

from dataclasses import dataclass
from datetime import date

from coco_ratings.core import MAX_DEVIATION, GameResult, RatingsCalculator, Section
from coco_ratings.core import Player as CorePlayer

# Far enough back that a player with no recorded last-played date has their
# deviation aged to the maximum, which is the honest answer for someone the
# roster has never seen play.
_NEVER = date(1900, 1, 1)


@dataclass(frozen=True)
class Projection:
    """One entrant's projected standing. Derived; never stored."""

    player_number: str
    name: str
    # The rating they started the tournament on.
    #
    # For an entrant who arrived **unrated** this equals ``new_rating``, so
    # ``delta`` is 0 — because there was no previous rating to move. That is not
    # a quirk of this projection: it is exactly what the official run records
    # for a player's first tournament (``ratings.TournamentResult`` stores
    # old == new there), and the projection's whole job is to predict what the
    # official run will say. Render ``was_unrated`` rather than a delta for
    # those entrants.
    old_rating: int
    new_rating: int
    new_deviation: float
    games: int
    wins: float
    losses: float
    spread: int
    # True when the projection *established* a rating rather than adjusting one.
    # Such an entrant has ``delta == 0`` by construction; this is the flag that
    # says why, and the one a display should key off.
    was_unrated: bool

    @property
    def delta(self) -> int:
        return self.new_rating - self.old_rating


def project_ratings(division, as_of=None) -> dict[str, Projection]:
    """``{player_number: Projection}`` for every entrant with a rating to project.

    ``as_of`` is the date the deviation is aged to; it defaults to the
    tournament's start date, which is what the official run will use when this
    tournament is eventually rated.

    Returns an empty dict for a division with no entrants or no completed games
    — there is nothing to project, and saying so is better than returning
    everyone's unchanged rating as though it meant something.
    """
    entrants = [
        e for e in division.entrants.select_related("player") if not e.player.is_bye
    ]
    if not entrants:
        return {}

    as_of = as_of or division.tournament.start_date
    players = {e.key: _seed(e, as_of) for e in entrants}
    if not _add_games(division, players):
        return {}

    section = Section(division.name)
    section.players = list(players.values())
    for player in section.players:
        player.tally_results()

    calculator = RatingsCalculator()
    # Unrated players first, and through the convergence loop rather than the
    # single-pass rating: their rating has to be *established* from their
    # opponents before anyone can be rated against them.
    calculator.calc_initial_ratings(section)
    for player in section.get_rated_players():
        calculator.calc_new_rating_for_player(player)

    return {
        entrant.key: _projection(entrant, players[entrant.key])
        for entrant in entrants
    }


def _seed(entrant, as_of):
    """A ``core.Player`` seeded from the entrant's frozen snapshot.

    Mirrors ``ratingsdb.adjust_tournament``: set the pre-tournament numbers,
    then age the deviation to the tournament date. Only rated players are aged,
    matching the official flow — an unrated player has no deviation to grow.
    """
    player = CorePlayer(
        entrant.player.name,
        init_rating=entrant.rating,
        # A snapshot taken before the roster pull existed has no deviation; the
        # maximum is what an unknown one means.
        init_rating_deviation=entrant.deviation or MAX_DEVIATION,
        career_games=entrant.career_games,
        last_played=entrant.last_played or _NEVER,
    )
    # set_init_rating decides is_unrated from the rating itself (< 100), so an
    # entrant pinned at 0 arrives here already marked unrated.
    if not player.is_unrated:
        player.adjust_initial_deviation(as_of)
    player.last_played = as_of

    # set_init_rating clamps a sub-100 rating up to the 1500 seed but leaves
    # new_rating on the raw value — 0 for an unrated entrant. That only shows if
    # the player is never actually rated, which happens when every one of their
    # games is skipped: all byes, or all forfeits. Reporting a projected rating
    # of 0 for such a player would be nonsense, so start them where the engine
    # starts an unrated player it creates itself — at the seed.
    if player.is_unrated:
        player.new_rating = player.init_rating
        player.new_rating_deviation = player.init_rating_deviation
    return player


def _add_games(division, players) -> bool:
    """Attach a ``GameResult`` per completed game. True if there were any.

    Byes never reach the calculator. Two things stop them and only the second is
    load-bearing: the explicit check below, and the fact that ``players`` is
    built from real entrants only, so a bye has nobody to be looked up as. The
    check stays because it says *why* a bye is not a game — the calculator
    would ignore it anyway (it skips a zero score), but it would still count
    toward career games, and career games feed the rating multiplier. The
    official run never sees byes at all, since the results export omits them.
    """
    slips = division.result_slips.select_related(
        "winner__player", "loser__player"
    ).order_by("round")
    played = False
    for slip in slips:
        if slip.winner.player.is_bye or slip.loser.player.is_bye:
            continue
        winner = players.get(slip.winner_key)
        loser = players.get(slip.loser_key)
        if winner is None or loser is None:
            # A result against someone no longer entered. It counts for the
            # opponent in the standings, but there is nobody to rate it against.
            continue
        winner.games.append(
            GameResult(slip.round, loser, slip.winner_score, slip.loser_score)
        )
        loser.games.append(
            GameResult(slip.round, winner, slip.loser_score, slip.winner_score)
        )
        played = True
    return played


def _projection(entrant, player):
    return Projection(
        player_number=entrant.key,
        name=entrant.player.name,
        # init_rating *after* rating, which for an unrated player is the value
        # calc_initial_ratings converged on — see the note on Projection.
        old_rating=round(player.init_rating),
        new_rating=round(player.new_rating),
        new_deviation=round(player.new_rating_deviation, 2),
        games=len(player.games),
        wins=player.wins,
        losses=player.losses,
        spread=player.spread,
        was_unrated=player.is_unrated,
    )
