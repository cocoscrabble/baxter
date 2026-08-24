from datetime import date

from coco_ratings.identity import canonical_player_number
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from tournaments.models import (
    BYE_PLAYER_NUMBER,
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    Tournament,
    is_reserved_player_number,
    next_temp_player_number,
)
from users.models import User


def setUpTournament(target):
    """Common test setup: owner, other user, tournament, division, 2 players + entrants."""
    target.owner = User.objects.create_user(username="owner", password="testpass123")
    target.other = User.objects.create_user(username="other", password="testpass123")
    target.tournament = Tournament.objects.create(
        name="Test Tournament",
        location="Test Location",
        start_date=date(2026, 3, 15),
        owner=target.owner,
    )
    target.tournament.editors.add(target.owner)
    target.division = Division.objects.create(name="Open", tournament=target.tournament)
    target.player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
    target.player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
    target.entrant1 = Entrant.objects.create(
        division=target.division, player=target.player1, number=1
    )
    target.entrant2 = Entrant.objects.create(
        division=target.division, player=target.player2, number=2
    )


class TournamentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.editor = User.objects.create_user(username="editor", password="testpass123")

    def test_str_returns_name(self):
        self.assertEqual(str(self.tournament), "Test Tournament")

    def test_get_absolute_url(self):
        url = self.tournament.get_absolute_url()
        self.assertEqual(url, f"/tournaments/{self.tournament.slug}/")

    def test_owner_can_edit(self):
        self.assertTrue(self.tournament.can_edit(self.owner))

    def test_editor_can_edit(self):
        self.tournament.editors.add(self.editor)
        self.assertTrue(self.tournament.can_edit(self.editor))

    def test_other_user_cannot_edit(self):
        self.assertFalse(self.tournament.can_edit(self.other))


class DivisionModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_str_returns_name(self):
        self.assertEqual(str(self.division), "Open")

    def test_max_round_with_no_results(self):
        division = Division.objects.create(name="Empty", tournament=self.tournament)
        self.assertEqual(division.max_round(), 0)

    def test_max_round_with_results(self):
        division = Division.objects.create(name="WithResults", tournament=self.tournament)
        entrant1 = Entrant.objects.create(division=division, player=self.player1, number=1)
        entrant2 = Entrant.objects.create(division=division, player=self.player2, number=2)
        for r in [1, 3, 2]:
            ResultSlip.objects.create(
                division=division,
                round=r,
                winner=entrant1,
                winner_score=400,
                loser=entrant2,
                loser_score=350,
                winner_started=True,
            )
        self.assertEqual(division.max_round(), 3)


class EntrantModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.division.entrants.all().delete()
        cls.player = Player.objects.create(
            name="John Doe",
            player_number="12345",
            rating=1500,
        )

    def test_str_returns_number_and_player_name(self):
        entrant = Entrant.objects.create(
            division=self.division,
            player=self.player,
            number=1,
        )
        self.assertEqual(str(entrant), "1: John Doe")

    def test_duplicate_player_in_division_is_rejected(self):
        Entrant.objects.create(division=self.division, player=self.player, number=1)
        with self.assertRaises(IntegrityError):
            Entrant.objects.create(division=self.division, player=self.player, number=2)


class ResultSlipModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_str_returns_formatted_result(self):
        slip = ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        self.assertEqual(str(slip), "R1: Alice 450-380 Bob")

    def test_winner_and_loser_name(self):
        slip = ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        self.assertEqual(slip.winner_name, "Alice")
        self.assertEqual(slip.loser_name, "Bob")


class DivisionSettingsModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_str(self):
        settings = DivisionSettings.objects.create(division=self.division)
        self.assertEqual(str(settings), "Settings for Open")


class NextTempPlayerNumberTests(TestCase):
    def test_no_players(self):
        self.assertEqual(next_temp_player_number(), "T-1")

    def test_sequences_over_existing_temp_numbers(self):
        Player.objects.create(name="A", player_number="T-1", rating=1500, is_provisional=True)
        Player.objects.create(name="B", player_number="T-2", rating=1500, is_provisional=True)
        self.assertEqual(next_temp_player_number(), "T-3")

    def test_ignores_registry_players(self):
        # Canonical registry numbers must not influence the temp sequence.
        Player.objects.create(name="A", player_number="A100", rating=1500)
        Player.objects.create(name="B", player_number="101", rating=1500)
        self.assertEqual(next_temp_player_number(), "T-1")

    def test_fills_gap_above_current_max(self):
        Player.objects.create(name="A", player_number="T-5", rating=1500, is_provisional=True)
        self.assertEqual(next_temp_player_number(), "T-6")


class SlugTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="o", password="p")

    def _tournament(self, name):
        return Tournament.objects.create(
            name=name, location="x", start_date=date(2026, 1, 1), owner=self.owner
        )

    def test_tournament_slug_from_name(self):
        t = self._tournament("Spring Open 2026")
        self.assertEqual(t.slug, "spring-open-2026")

    def test_tournament_slug_deduped_globally(self):
        a = self._tournament("Open")
        b = self._tournament("Open")
        self.assertEqual(a.slug, "open")
        self.assertEqual(b.slug, "open-2")

    def test_reserved_slug_avoided(self):
        t = self._tournament("Create")
        self.assertEqual(t.slug, "create-2")

    def test_empty_slugify_falls_back(self):
        t = self._tournament("!!!")
        self.assertEqual(t.slug, "tournament")

    def test_division_slug_deduped_within_tournament_only(self):
        t1 = self._tournament("T1")
        t2 = self._tournament("T2")
        d1 = Division.objects.create(name="A", tournament=t1)
        d1b = Division.objects.create(name="A B", tournament=t1)  # also slugifies near "a"
        d2 = Division.objects.create(name="A", tournament=t2)
        self.assertEqual(d1.slug, "a")
        self.assertEqual(d2.slug, "a")  # different tournament, no clash
        self.assertEqual(d1b.slug, "a-b")

    def test_rename_resyncs_slug_and_records_alias(self):
        t = self._tournament("Old Name")
        d = Division.objects.create(name="Open", tournament=t)
        old_t_slug, old_d_slug = t.slug, d.slug

        t.name = "New Name"
        t.save()
        d.name = "Champs"
        d.save()

        self.assertEqual(t.slug, "new-name")
        self.assertEqual(d.slug, "champs")
        self.assertTrue(t.slug_aliases.filter(slug=old_t_slug).exists())
        self.assertTrue(d.slug_aliases.filter(slug=old_d_slug).exists())

    def test_rename_persists_slug_with_update_fields(self):
        d = Division.objects.create(name="Open", tournament=self._tournament("T"))
        d.name = "Closed"
        d.save(update_fields=["name"])
        d.refresh_from_db()
        self.assertEqual(d.slug, "closed")


class PlayerNumberIdentityTests(TestCase):
    """player_number is the identity, and it has exactly one spelling.

    See plans/PLAN_PLAYER_IDENTITY.md phase 1. The canonical form is shared with
    the central player database via coco_ratings.identity, so the two systems
    cannot disagree about whether 233 and 0233 are one person.
    """

    def test_save_canonicalizes_the_number(self):
        player = Player.objects.create(name="Bare", player_number="233", rating=1500)
        player.refresh_from_db()
        self.assertEqual(player.player_number, "0233")

    def test_save_collapses_over_padding(self):
        player = Player.objects.create(name="Padded", player_number="00233", rating=1500)
        player.refresh_from_db()
        self.assertEqual(player.player_number, "0233")

    def test_local_and_reserved_numbers_survive_canonicalization(self):
        # T- placeholders and the bye's number are not CoCo numbers; rewriting
        # them would break the bye and the export gate.
        temp = Player.objects.create(name="Temp", player_number="T-7", rating=0)
        self.assertEqual(temp.player_number, "T-7")
        self.assertEqual(Player.get_bye().player_number, BYE_PLAYER_NUMBER)

    def test_duplicate_number_is_rejected(self):
        Player.objects.create(name="First", player_number="0500", rating=1500)
        with self.assertRaises(IntegrityError):
            Player.objects.create(name="Second", player_number="0500", rating=1500)

    def test_case_variant_is_rejected(self):
        Player.objects.create(name="First", player_number="T-9", rating=1500)
        with self.assertRaises(IntegrityError):
            Player.objects.create(name="Second", player_number="t-9", rating=1500)

    def test_bare_and_padded_are_one_identity(self):
        # The point of the whole exercise: these are the same person, and the
        # database must refuse to hold them as two.
        Player.objects.create(name="Padded", player_number="0233", rating=1500)
        with self.assertRaises(IntegrityError):
            Player.objects.create(name="Bare", player_number="233", rating=1500)

    def test_reserved_number_rejected_for_a_real_player(self):
        player = Player(name="Impostor", player_number=BYE_PLAYER_NUMBER, rating=1500)
        with self.assertRaises(ValidationError):
            player.full_clean()

    def test_reserved_number_rejected_in_any_casing(self):
        player = Player(name="Impostor", player_number="bye", rating=1500)
        with self.assertRaises(ValidationError):
            player.full_clean()

    def test_the_bye_may_hold_the_reserved_number(self):
        bye = Player.get_bye()
        bye.full_clean()  # must not raise

    def test_reserved_helper_matches_the_rust_bye_check(self):
        # scrabble-pairing/src/standings.rs compares the engine key to "Bye"
        # with eq_ignore_ascii_case, so the reservation must be case-insensitive
        # in exactly the same way.
        for value in ("BYE", "bye", "Bye", " bye "):
            self.assertTrue(is_reserved_player_number(value), value)
        for value in ("0233", "T-7", "", None, "BYES"):
            self.assertFalse(is_reserved_player_number(value), value)


class CanonicalNumberConformanceTests(TestCase):
    """The shared contract with the central player database.

    This table is duplicated in ../ratings/tests/test_identity.py on purpose: if
    the two projects ever disagree about the canonical form, one person becomes
    two on both sides at once. A divergence should fail here as well as there.
    """

    CASES = [
        ("233", "0233"),
        ("1", "0001"),
        ("0233", "0233"),
        ("00233", "0233"),
        (233, "0233"),
        ("  233 ", "0233"),
        ("12345", "12345"),   # wider than four digits is never truncated
        ("T-7", "T-7"),
        ("T-123", "T-123"),
        ("BYE", "BYE"),
        ("", ""),
    ]

    def test_canonical_form(self):
        for value, expected in self.CASES:
            with self.subTest(value=value):
                self.assertEqual(canonical_player_number(value), expected)

    def test_is_idempotent(self):
        for value, _ in self.CASES:
            with self.subTest(value=value):
                once = canonical_player_number(value)
                self.assertEqual(canonical_player_number(once), once)


class SharedNameTests(TestCase):
    """Names are not identities, so two players may hold the same one."""

    def test_two_players_may_share_a_name(self):
        first, error = Player.create("John Smith", rating=1600)
        self.assertIsNone(error)
        second, error = Player.create("John Smith", rating=1400)
        self.assertIsNone(error)
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.player_number, second.player_number)
        self.assertEqual(Player.objects.filter(name="John Smith").count(), 2)

    def test_create_still_requires_a_name(self):
        player, error = Player.create("   ")
        self.assertIsNone(player)
        self.assertEqual(error, "Name is required.")

    def test_same_named_finds_the_existing_players_in_number_order(self):
        Player.objects.create(name="Ann", player_number="0500", rating=1500)
        Player.objects.create(name="ANN", player_number="0100", rating=1400)
        Player.objects.create(name="Bea", player_number="0200", rating=1300)
        self.assertEqual(
            [p.player_number for p in Player.same_named("ann")], ["0100", "0500"]
        )

    def test_same_named_is_empty_for_a_blank_name(self):
        Player.objects.create(name="Ann", player_number="0500", rating=1500)
        self.assertFalse(Player.same_named("").exists())
        self.assertFalse(Player.same_named(None).exists())

    def test_a_non_numeric_rating_falls_back_to_zero(self):
        player, error = Player.create("Ann", rating="not a number")
        self.assertIsNone(error)
        self.assertEqual(player.rating, 0)


class EffectiveRatingTests(TestCase):
    """The rating cascade: CoCo, else WESPA, else nothing (decision 2).

    ``Player.rating == 0`` is the sole test for "no CoCo rating", which is why
    a WESPA rating of 0 is still distinguishable from having none at all.
    """

    def _player(self, rating, wespa):
        return Player(name="P", player_number="0001", rating=rating,
                      wespa_rating=wespa)

    def test_coco_wins_when_both_are_present(self):
        self.assertEqual(
            self._player(1600, 1400).effective_rating, (1600, Entrant.COCO)
        )

    def test_coco_alone(self):
        self.assertEqual(
            self._player(1600, None).effective_rating, (1600, Entrant.COCO)
        )

    def test_wespa_alone(self):
        self.assertEqual(
            self._player(0, 1400).effective_rating, (1400, Entrant.WESPA)
        )

    def test_neither(self):
        self.assertEqual(
            self._player(0, None).effective_rating, (0, Entrant.NONE)
        )

    def test_a_wespa_rating_of_zero_is_still_a_rating(self):
        """Distinct from having none: NULL means unknown, 0 means zero."""
        self.assertEqual(
            self._player(0, 0).effective_rating, (0, Entrant.WESPA)
        )


class EntrantEnterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.division.entrants.all().delete()

    def _player(self, rating=0, wespa=None, number="0500"):
        return Player.objects.create(
            name="Snap", player_number=number, rating=rating, wespa_rating=wespa
        )

    def test_snapshots_the_coco_rating(self):
        entrant = Entrant.enter(self.division, self._player(rating=1600), 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (1600, "coco"))

    def test_snapshots_the_wespa_rating_when_there_is_no_coco_one(self):
        entrant = Entrant.enter(self.division, self._player(wespa=1400), 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (1400, "wespa"))

    def test_snapshots_nothing_when_the_player_has_neither(self):
        entrant = Entrant.enter(self.division, self._player(), 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

    def test_an_explicit_rating_is_manual(self):
        entrant = Entrant.enter(
            self.division, self._player(rating=1600), 1, rating=1234
        )
        self.assertEqual((entrant.rating, entrant.rating_source), (1234, "manual"))

    def test_an_explicit_rating_of_zero_is_still_manual(self):
        """0 is a rating a director may deliberately assign, not a missing one."""
        entrant = Entrant.enter(
            self.division, self._player(rating=1600), 1, rating=0
        )
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "manual"))

    def test_registration_flags_pass_through(self):
        entrant = Entrant.enter(
            self.division, self._player(rating=1500), 1,
            tentative=True, paid=True, playing_up=True, payment_note="cash",
        )
        self.assertTrue(entrant.tentative)
        self.assertTrue(entrant.paid)
        self.assertTrue(entrant.playing_up)
        self.assertEqual(entrant.payment_note, "cash")

    def test_the_snapshot_does_not_follow_the_player(self):
        """The whole point of pinning: a rating change under a running
        tournament must not reshuffle anyone."""
        player = self._player(rating=1600)
        entrant = Entrant.enter(self.division, player, 1)
        player.rating = 1900
        player.save(update_fields=["rating"])
        entrant.refresh_from_db()
        self.assertEqual(entrant.rating, 1600)

    def test_the_bye_entrant_is_still_creatable(self):
        bye = self.division.bye_entrant()
        self.assertTrue(bye.player.is_bye)
        self.assertEqual((bye.rating, bye.rating_source), (0, "none"))
