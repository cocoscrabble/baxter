"""Where player records come from.

One interface with a local implementation, so the registration page's search and
its guest creation do not care whether the roster lives in Baxter's own table or
in the central player database (plans/PLAN_ENTRANTS.md decision 11).

``LocalPlayerSource`` is the configured default and the thing tests run against.
The registry-backed implementation belongs with the roster pull specified in
PLAN_COCO_PROGRAM.md; nothing here does any network I/O.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerRecord:
    """A player as a source describes them — never a Baxter primary key."""

    player_number: str
    name: str
    rating: int = 0
    wespa_rating: int | None = None

    @property
    def effective_rating(self):
        """``(rating, source)``, matching ``Player.effective_rating``."""
        from tournaments.models import Entrant

        if self.rating:
            return self.rating, Entrant.COCO
        if self.wespa_rating is not None:
            return self.wespa_rating, Entrant.WESPA
        return 0, Entrant.NONE


class PlayerSource:
    """The seam. Implementations answer three questions and nothing else."""

    def search(self, query) -> list[PlayerRecord]:
        raise NotImplementedError

    def fetch(self, player_number) -> PlayerRecord | None:
        raise NotImplementedError

    def mint_number(self, name) -> str | None:
        """A number for a player this source has never seen, or None to keep
        whatever local placeholder the caller would otherwise mint."""
        raise NotImplementedError


class LocalPlayerSource(PlayerSource):
    """Baxter's own ``Player`` table, minting ``T-`` numbers as it always has."""

    LIMIT = 20

    def _record(self, player):
        return PlayerRecord(
            player_number=player.player_number,
            name=player.name,
            rating=player.rating,
            wespa_rating=player.wespa_rating,
        )

    def search(self, query):
        """Players whose name contains ``query``, best-guess order.

        The bye is never a candidate. An empty query returns nothing rather than
        the whole roster: this feeds an autocomplete, and a thousand-row reply
        is not an answer.
        """
        from tournaments.models import Player

        query = (query or "").strip()
        if not query:
            return []
        matches = (
            Player.objects.filter(is_bye=False, name__icontains=query)
            .order_by("name", "player_number")[: self.LIMIT]
        )
        return [self._record(p) for p in matches]

    def fetch(self, player_number):
        from tournaments.models import Player, canonical_player_number

        player = Player.objects.filter(
            player_number=canonical_player_number(player_number), is_bye=False
        ).first()
        return self._record(player) if player else None

    def mint_number(self, name):
        from tournaments.models import next_temp_player_number

        return next_temp_player_number()


def get_player_source() -> PlayerSource:
    """The configured source. One place to swap in a registry-backed one."""
    return LocalPlayerSource()
