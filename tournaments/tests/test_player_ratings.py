"""Every write of a player's rating goes through ``player_ratings``.

That module is where everything downstream of a rating change lives (entrants
of divisions that have not started re-pin and reseed), so a writer that goes
round it silently leaves those entrants stale. The completeness check below is
what stops one being added; the rest pins that the gate fires, and only when a
rating actually moved.
"""

import re
from pathlib import Path

from django.test import TestCase

from tournaments.commands import add_entrant, create_tournament
from tournaments.models import Entrant, Player
from tournaments.player_ratings import save_players
from users.models import User

APP = Path(__file__).resolve().parent.parent


class CompletenessTests(TestCase):
    def test_players_are_bulk_updated_only_through_the_gate(self):
        offenders = [
            str(path.relative_to(APP))
            for path in APP.rglob("*.py")
            if "migrations" not in path.parts
            and "tests" not in path.parts
            and path.name != "player_ratings.py"
            and re.search(r"\bPlayer\.objects\.bulk_update\(", path.read_text())
        ]
        self.assertEqual(
            offenders, [],
            "Write player ratings with player_ratings.save_players, so the "
            "entrants that follow them are refreshed.",
        )


class GateTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username="td-gate", password="pw")
        tournament = create_tournament(
            None, owner,
            {
                "name": "Soon", "location": "X", "start_date": "2026-12-01",
                "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
            },
        )
        self.player = Player.objects.create(
            name="Alec", player_number="0233", rating=1500
        )
        self.entrant = add_entrant(
            tournament, owner,
            {"division": "Open", "player": "0233",
             "rating": 1500, "rating_source": Entrant.COCO},
        )

    def test_a_rating_write_reaches_the_entrant(self):
        self.player.rating = 1700
        save_players([self.player], ["rating"])
        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1700)

    def test_a_write_that_touches_no_rating_does_not_refresh(self):
        # The row already disagrees with the entrant; a name change is no
        # reason to act on that.
        Player.objects.filter(pk=self.player.pk).update(rating=1700)
        self.player.refresh_from_db()
        self.player.name = "Alec B"
        save_players([self.player], ["name"])
        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1500)
