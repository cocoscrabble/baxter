"""The one gate for writing a player's rating, and everything downstream of it.

A player's rating seed arrives from several places — the roster pull, a
confirmed roster resolution, the WESPA pull, a WESPA link, a player import —
and each of them used to write ``Player`` rows directly. Whatever has to follow
a rating change (today: entrants of divisions that have not started re-pin and
reseed, ``entrant_sync.refresh_upcoming``) then had to be remembered at every
one of those call sites, and some were missed.

So there are exactly two ways in:

- ``save_players`` is how an existing player's rating seed is written. It
  writes, then runs the downstream step if a rating field was among those
  written. ``test_player_ratings`` fails if ``Player.objects.bulk_update``
  appears anywhere else.
- ``players_rerated`` is the downstream step on its own, for the cases where
  the rating an entrant follows changes without a rating write: a guest merged
  into a real player, or a form (Django admin) that saved the row itself.

Creating a player needs neither: a new player has no entrants to follow them.

**Never call either from inside a command.** The downstream step records its
own events, carrying the values it wrote; nested inside a command, replaying
that command would re-run it against the replay database's player table.
"""

from .models import Player

# What an entrant's seed is derived from (``entrant_sync.current_seed``).
RATING_FIELDS = frozenset(
    {"rating", "wespa_rating", "deviation", "career_games", "last_played"}
)


def save_players(players, fields, *, actor=None):
    """Write ``fields`` on existing ``players``, then follow any rating change.

    ``bulk_update`` bypasses ``Player.save``, so a caller writing
    ``player_number`` must hand over a canonical one. ``actor`` is who the
    downstream events are attributed to; None for the scheduled pulls.
    """
    players = list(players)
    if not players:
        return
    Player.objects.bulk_update(players, list(fields), batch_size=500)
    if RATING_FIELDS & set(fields):
        players_rerated(players, actor=actor)


def players_rerated(players, *, actor=None):
    """Everything that follows a change in how ``players`` are rated."""
    from .entrant_sync import refresh_upcoming

    refresh_upcoming(players=players, actor=actor)
