"""Merging a guest into the CoCo player of the same name.

For the duplicates the roster pull leaves when a name belongs to two guests: it
creates the real player as a third row, and renumbering is then impossible.
"""

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.commands import create_tournament
from tournaments.models import Entrant, Player
from tournaments.player_merge import merge_candidates, merge_guest
from tournaments.tests import test_replay
from users.models import User


def tournament(owner, name):
    return create_tournament(
        None, owner,
        {
            "name": name, "location": "X", "start_date": "2026-05-01",
            "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
        },
    )


class MergeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="td-merge", password="pw")
        self.first = tournament(self.owner, "Champs")
        self.second = tournament(self.owner, "Open")
        self.guest1 = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0, is_provisional=True
        )
        self.guest2 = Player.objects.create(
            name="joe thorngren", player_number="T-9", rating=0, is_provisional=True,
            wespa_id=1234, wespa_rating=1500,
        )
        self.coco = Player.objects.create(
            name="Joe Thorngren", player_number="0301", rating=1400
        )
        self.entrant1 = Entrant.enter(self.first.divisions.get(), self.guest1, 1)
        self.entrant2 = Entrant.enter(self.second.divisions.get(), self.guest2, 1)

    def test_every_guest_sharing_the_name_is_offered(self):
        Player.objects.create(name="Nobody Else", player_number="T-5", rating=0,
                              is_provisional=True)
        offered = {c.guest.player_number: c for c in merge_candidates()}
        self.assertEqual(set(offered), {"T-4", "T-9"})
        self.assertEqual(offered["T-4"].targets, [self.coco])
        self.assertEqual(offered["T-4"].tournaments, [self.first])

    def test_both_guests_fold_into_one_player(self):
        merge_guest(self.guest1, self.coco, actor=self.owner)
        merge_guest(self.guest2, self.coco, actor=self.owner)

        self.assertEqual(
            list(Player.objects.filter(name__iexact="Joe Thorngren")), [self.coco]
        )
        for entrant in (self.entrant1, self.entrant2):
            entrant.refresh_from_db()
            self.assertEqual(entrant.player_id, self.coco.pk)
        self.assertEqual(merge_candidates(), [])

    def test_entrants_take_the_real_players_rating_before_the_start(self):
        # Entered as an unrated guest; neither division has published a round.
        merge_guest(self.guest1, self.coco, actor=self.owner)
        self.entrant1.refresh_from_db()
        self.assertEqual(
            (self.entrant1.rating, self.entrant1.rating_source), (1400, Entrant.COCO)
        )

    def test_the_wespa_link_moves_with_the_person(self):
        merge_guest(self.guest2, self.coco, actor=self.owner)
        self.coco.refresh_from_db()
        self.assertEqual((self.coco.wespa_id, self.coco.wespa_rating), (1234, 1500))

    def test_it_is_logged_in_every_tournament_the_guest_played_in(self):
        Entrant.enter(self.second.divisions.get(), self.guest1, 2)
        merge_guest(self.guest1, self.coco, actor=self.owner)
        for t in (self.first, self.second):
            with self.subTest(tournament=t.name):
                event = t.events.get(event_type="player_merged")
                self.assertEqual(event.payload, {"guest": "T-4", "into": "0301"})

    def test_one_person_entered_twice_in_a_division_is_refused(self):
        Entrant.enter(self.first.divisions.get(), self.coco, 2)
        with self.assertRaisesRegex(ValueError, "both entered in Champs / Open"):
            merge_guest(self.guest1, self.coco, actor=self.owner)
        self.assertTrue(Player.objects.filter(pk=self.guest1.pk).exists())
        self.assertFalse(self.first.events.filter(event_type="player_merged").exists())

    def test_a_refusal_in_a_later_tournament_logs_nothing_anywhere(self):
        # The clash is in the second tournament; the first must not keep an event.
        Entrant.enter(self.second.divisions.get(), self.guest1, 2)
        Entrant.enter(self.second.divisions.get(), self.coco, 3)
        with self.assertRaises(ValueError):
            merge_guest(self.guest1, self.coco, actor=self.owner)
        self.assertFalse(self.first.events.filter(event_type="player_merged").exists())
        self.entrant1.refresh_from_db()
        self.assertEqual(self.entrant1.player_id, self.guest1.pk)

    def test_only_a_guest_can_be_merged(self):
        other = Player.objects.create(name="Joe Thorngren", player_number="0302", rating=0)
        with self.assertRaisesRegex(ValueError, "not a guest"):
            merge_guest(other, self.coco, actor=self.owner)


class MergeViewTests(TestCase):
    def setUp(self):
        self.url = reverse("player_merge")
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0, is_provisional=True
        )
        self.coco = Player.objects.create(
            name="Joe Thorngren", player_number="0301", rating=1400
        )

    def login(self, role):
        self.client.force_login(
            User.objects.create_user(username=f"u-{role}", password="pw", role=role)
        )

    def test_a_director_is_refused(self):
        self.login("director")
        response = self.client.post(self.url, {"guest": "T-4", "into": "0301"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Player.objects.filter(pk=self.guest.pk).exists())

    def test_an_admin_merges(self):
        self.login("admin")
        self.assertContains(self.client.get(self.url), "Merge into #0301")
        response = self.client.post(self.url, {"guest": "T-4", "into": "0301"})
        self.assertRedirects(response, self.url)
        self.assertFalse(Player.objects.filter(pk=self.guest.pk).exists())

    def test_different_names_are_refused(self):
        other = Player.objects.create(name="Someone Else", player_number="0400", rating=0)
        self.login("admin")
        self.client.post(self.url, {"guest": "T-4", "into": other.player_number})
        self.assertTrue(Player.objects.filter(pk=self.guest.pk).exists())

    def test_the_admin_index_flags_waiting_merges(self):
        self.login("admin")
        self.assertContains(
            self.client.get(reverse("admin_index")), "a name with a CoCo player</strong>"
        )


@tag("slow")
class MergeReplayTests(test_replay.LoggedTournamentMixin, TestCase):
    """A log containing a merge replays to the same digest in a fresh database,
    where the unlogged roster pull that created the CoCo player never happened."""

    def test_replay_renumbers_where_the_coco_player_never_existed(self):
        from tournaments.events import division_digest, export_jsonl
        from tournaments.replay import parse_jsonl, replay

        guest = Player.objects.create(
            name="Guest Gwen", player_number="T-7", rating=1450, is_provisional=True
        )
        t, division = self._build_logged_tournament(players=[*self.players[:3], guest])
        coco = Player.objects.create(name="Guest Gwen", player_number="0412", rating=1450)

        merge_guest(guest, coco, actor=self.owner)
        recorded = division_digest(division)
        exported = export_jsonl(t)

        t.delete()
        Player.objects.all().delete()
        _header, events = parse_jsonl(exported)
        ctx = replay(events, verify=True)
        self.assertEqual(division_digest(ctx.tournament.divisions.get()), recorded)
