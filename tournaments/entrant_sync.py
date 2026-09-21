"""Keeping entrant ratings in step with the player table.

An entrant pins their whole rating seed on the entrant row (PLAN_ENTRANTS
decision 3), but the pin only *freezes* once the division has started — its
first round published (``Division.under_way``), the same moment the seeding
freezes. Until then the seed is live: whatever moves a player's rating (a
roster or WESPA pull, a player import, a WESPA link, a guest merge) re-pins
every entrant of every division that has not started (``refresh_upcoming``), and
publishing the first round catches up anything still behind first
(``refresh_before_start``). An entry taken months ahead of the event is seeded
off the ratings current when play begins, not when the form came in.

Once a division is under way, the freeze is what lets the roster pull run on a
six-hourly cron without reshuffling a tournament in progress, and nothing here
weakens it. What remains after that is the deliberate gesture: a director
looking at a division whose seeds have gone stale and choosing, entrant by
entrant, to take the new ones.

Two properties pull in different directions, and the split below is how they are
kept apart:

- **Drift is a live comparison.** ``rating_drift`` reads the player table as it
  stands now, so the page shows what a refresh would do at the moment it is
  looked at.

- **The refresh records values, never the intent.** The command is handed the
  seeds to write, not an instruction to go and sync, because a replay runs
  against a player table that has moved on since — often by months. Entrant
  ratings are part of the division digest (``events.division_digest``), so this
  is not a fine point: an event meaning "take whatever the roster says" would
  replay to a different digest every time, and the fuzzer's invariant would
  fail. The automatic refreshes are the same logged commands as the manual one,
  for the same reason.

**Manual ratings are never offered**, before the start or after. A director who
typed a rating was saying what this player is worth; a sync is not entitled to
overrule it (decision 3 again). To move one, edit it by hand — which re-pins it
as manual.
"""

from dataclasses import dataclass

from .models import Division, Entrant, RoundPairings

# What a refresh writes. Exactly the fields Entrant.enter freezes, because half
# a seed is worse than a stale one: the live projection damps by career games
# and grows deviation from last_played, so a rating moved without them describes
# a player who never existed.
SEED_FIELDS = ("rating", "rating_source", "deviation", "career_games", "last_played")


def current_seed(player):
    """The seed this player would be entered with today.

    Goes through ``Player.effective_rating`` rather than reading ``rating``, so
    the CoCo-then-WESPA cascade has one definition (decision 2).
    """
    rating, source = player.effective_rating
    return {
        "rating": rating,
        "rating_source": source,
        "deviation": player.deviation or 0.0,
        "career_games": player.career_games,
        "last_played": player.last_played,
    }


@dataclass(frozen=True)
class Drift:
    """One entrant whose pinned seed no longer matches the player table."""

    entrant: object
    seed: dict

    @property
    def key(self):
        """The player number — the identity, and what the form posts back."""
        return self.entrant.player.player_number

    @property
    def name(self):
        return self.entrant.display_name

    @property
    def old_rating(self):
        return self.entrant.rating

    @property
    def new_rating(self):
        return self.seed["rating"]

    @property
    def rating_changed(self):
        """Whether the *visible* number moves.

        A seed can drift without it: someone who played elsewhere gains career
        games and a last_played date while their rating stays put. Worth
        refreshing, but not worth drawing an arrow for.
        """
        return self.old_rating != self.new_rating


def rating_drift(division):
    """Every entrant in ``division`` whose seed differs from the player table.

    In display order, so the page and the payload agree. Manual entrants are
    excluded, not merely unticked — they are not on offer.
    """
    drifted = []
    for entrant in division.entrants.select_related("player").order_by(
        "-rating", "number"
    ):
        if entrant.rating_source == Entrant.MANUAL:
            continue
        seed = current_seed(entrant.player)
        if all(getattr(entrant, f) == seed[f] for f in SEED_FIELDS):
            continue
        drifted.append(Drift(entrant=entrant, seed=seed))
    return drifted


def payload_for(division, drifted):
    """The ``entrant_ratings_refreshed`` payload for these drift rows.

    Natural-key based and fully explicit, so it replays into a fresh database
    against a player table nobody has to reason about. ``last_played`` is
    isoformatted here because the log is JSON and a date is not.
    """
    return {
        "division": division.name,
        "entrants": [
            {
                "player": d.key,
                **{
                    f: (
                        d.seed[f].isoformat()
                        if f == "last_played" and d.seed[f] is not None
                        else d.seed[f]
                    )
                    for f in SEED_FIELDS
                },
            }
            for d in drifted
        ],
    }


def refresh_before_start(division, actor=None):
    """Re-pin a division that has not started to the player table, and reseed.

    A no-op once the division is under way — from then on only a director
    re-pins, entrant by entrant. Before that the seed is meant to be live, so
    every non-manual drift is taken. Both steps go through the logged commands
    and record nothing when nothing moved, so this is safe to call freely.

    Returns whether anything changed (a rating seed or an entrant number), which
    is what tells the publish gate its draft pairings are stale.
    """
    from .commands import refresh_entrant_ratings, reseed_entrants

    if division.under_way():
        return False
    drifted = rating_drift(division)
    if drifted:
        refresh_entrant_ratings(
            division.tournament, actor, payload_for(division, drifted)
        )
    renumbered = reseed_entrants(
        division.tournament, actor, {"division": division.name}
    )
    return bool(drifted or renumbered)


def upcoming_divisions(players=None):
    """Divisions with entrants and no round out of draft, optionally narrowed to
    those entering one of ``players``."""
    divisions = Division.objects.filter(entrants__isnull=False).exclude(
        round_pairings_set__status__in=[
            s for s, _ in RoundPairings.STATUS_CHOICES if s != RoundPairings.DRAFT
        ]
    )
    if players is not None:
        divisions = divisions.filter(entrants__player__in=players)
    return divisions.select_related("tournament").distinct()


def refresh_upcoming(players=None, actor=None):
    """``refresh_before_start`` every division that has not started.

    Called after anything that moves player ratings. ``actor`` is None for the
    scheduled pulls, which have nobody to attribute the change to. Returns the
    divisions that changed.
    """
    return [
        division
        for division in upcoming_divisions(players)
        if refresh_before_start(division, actor)
    ]
