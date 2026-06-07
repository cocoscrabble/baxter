from datetime import date

from django.test import TestCase

from tournaments.forms import (
    ResultSlipForm,
    RoundPairingForm,
    RoundPairingFormSet,
    TournamentForm,
    clean_multiline_text,
)
from tournaments.models import Division, Entrant, Pairing, Player, RoundPairings, Tournament
from users.models import User


class CleanMultilineTextTests(TestCase):
    def test_empty_string(self):
        self.assertEqual(clean_multiline_text(""), [])

    def test_whitespace_only(self):
        self.assertEqual(clean_multiline_text("   \n   \n   "), [])

    def test_single_line(self):
        self.assertEqual(clean_multiline_text("hello"), ["hello"])

    def test_multiple_lines(self):
        self.assertEqual(
            clean_multiline_text("one\ntwo\nthree"), ["one", "two", "three"]
        )

    def test_strips_whitespace(self):
        self.assertEqual(clean_multiline_text("  one  \n  two  "), ["one", "two"])

    def test_removes_empty_lines(self):
        self.assertEqual(
            clean_multiline_text("one\n\ntwo\n\n\nthree"), ["one", "two", "three"]
        )

    def test_removes_duplicates_preserving_order(self):
        self.assertEqual(
            clean_multiline_text("apple\nbanana\napple\ncherry\nbanana"),
            ["apple", "banana", "cherry"],
        )


def setUpTournament(target):
    """Common test setup: owner, tournament, division, 2 players + entrants."""
    target.owner = User.objects.create_user(username="owner", password="testpass123")
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


class TournamentFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.editor1 = User.objects.create_user(
            username="editor1", password="testpass123"
        )
        cls.editor2 = User.objects.create_user(
            username="editor2", password="testpass123"
        )

    def test_valid_form(self):
        form = TournamentForm(
            data={
                "name": "Test Tournament",
                "location": "Test Location",
                "start_date": "2026-03-15",
                "editor_usernames": "",
            }
        )
        self.assertTrue(form.is_valid())

    def test_valid_form_with_editors(self):
        form = TournamentForm(
            data={
                "name": "Test Tournament",
                "location": "Test Location",
                "start_date": "2026-03-15",
                "editor_usernames": "editor1\neditor2",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(len(form.cleaned_data["editor_usernames"]), 2)

    def test_invalid_editor_username(self):
        form = TournamentForm(
            data={
                "name": "Test Tournament",
                "location": "Test Location",
                "start_date": "2026-03-15",
                "editor_usernames": "editor1\nnonexistent",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("editor_usernames", form.errors)
        self.assertIn("nonexistent", form.errors["editor_usernames"][0])

    def test_multiple_invalid_editor_usernames(self):
        form = TournamentForm(
            data={
                "name": "Test Tournament",
                "location": "Test Location",
                "start_date": "2026-03-15",
                "editor_usernames": "fake1\nfake2",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("fake1", form.errors["editor_usernames"][0])
        self.assertIn("fake2", form.errors["editor_usernames"][0])

    def test_save_adds_owner_as_editor(self):
        form = TournamentForm(
            data={
                "name": "Test Tournament",
                "location": "Test Location",
                "start_date": "2026-03-15",
                "editor_usernames": "editor1",
            }
        )
        self.assertTrue(form.is_valid())
        tournament = form.save(commit=False)
        tournament.owner = self.owner
        tournament.save()
        form.save()
        self.assertIn(self.owner, tournament.editors.all())
        self.assertIn(self.editor1, tournament.editors.all())

    def test_edit_form_populates_editors(self):
        tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        tournament.editors.add(self.owner, self.editor1)

        form = TournamentForm(instance=tournament)
        # Owner should be excluded from the list
        self.assertIn("editor1", form.fields["editor_usernames"].initial)
        self.assertNotIn("owner", form.fields["editor_usernames"].initial)


class ResultSlipFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        # Create a RoundPairings + Pairing so the form has data to work with.
        cls.rp = RoundPairings.objects.create(
            division=cls.division, round=1, status=RoundPairings.PUBLISHED,
        )
        cls.pairing = Pairing.objects.create(
            division=cls.division, round=1, round_pairings=cls.rp,
            first=cls.entrant1, second=cls.entrant2,
            table=1,
        )

    def _pbr(self):
        return {
            1: [(self.pairing.pk, self.entrant1.pk, "Alice", self.entrant2.pk, "Bob")],
        }

    def test_loser_score_field_label(self):
        form = ResultSlipForm(division=self.division, pairings_by_round=self._pbr())
        self.assertEqual(form.fields["loser_score"].label, "Opponent score")

    def test_valid_form(self):
        form = ResultSlipForm(
            data={
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
            division=self.division,
            pairings_by_round=self._pbr(),
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_accepts_negative_scores(self):
        # Scrabble scores can legitimately go negative (e.g. end-of-game
        # penalties), so the score fields must accept them.
        form = ResultSlipForm(
            data={
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant1.pk,
                "winner_score": 5,
                "loser_score": -20,
                "winner_started": True,
            },
            division=self.division,
            pairings_by_round=self._pbr(),
        )
        self.assertTrue(form.is_valid(), form.errors)
        rs = form.save()
        self.assertEqual(rs.winner_score, 5)
        self.assertEqual(rs.loser_score, -20)

    def test_winner_must_be_in_pairing(self):
        other_player = Player.objects.create(name="Charlie", player_number="003", rating=1400)
        other_entrant = Entrant.objects.create(division=self.division, player=other_player, number=3)
        form = ResultSlipForm(
            data={
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": other_entrant.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
            division=self.division,
            pairings_by_round=self._pbr(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Winner must be one of the players in the pairing", str(form.errors))

    def test_invalid_pairing_pk(self):
        form = ResultSlipForm(
            data={
                "round": 1,
                "pairing": 99999,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
            division=self.division,
            pairings_by_round=self._pbr(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pairing", form.errors)

    def test_save_creates_result_slip(self):
        form = ResultSlipForm(
            data={
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
            division=self.division,
            pairings_by_round=self._pbr(),
        )
        self.assertTrue(form.is_valid(), form.errors)
        rs = form.save()
        self.assertEqual(rs.division, self.division)
        self.assertEqual(rs.round, 1)
        self.assertEqual(rs.pairing, self.pairing)
        self.assertEqual(rs.winner, self.entrant1)
        self.assertEqual(rs.loser, self.entrant2)
        self.assertEqual(rs.winner_score, 450)
        self.assertEqual(rs.loser_score, 380)


class RoundPairingFormTests(TestCase):
    def test_valid(self):
        form = RoundPairingForm(data={
            "round": 3,
            "pairing_type": "Swiss",
            "start_round": 2,
        })
        self.assertTrue(form.is_valid())

    def test_start_round_must_be_less_than_round(self):
        form = RoundPairingForm(data={
            "round": 3,
            "pairing_type": "Swiss",
            "start_round": 3,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("Based on round must be less than 3", str(form.errors))

    def test_start_round_cannot_exceed_round(self):
        form = RoundPairingForm(data={
            "round": 2,
            "pairing_type": "Swiss",
            "start_round": 5,
        })
        self.assertFalse(form.is_valid())

    def test_pairing_type_choices_include_all_strategies(self):
        form = RoundPairingForm()
        choices = [c[0] for c in form.fields["pairing_type"].choices]
        for name in ["KotH", "QotH", "Swiss", "RoundRobin"]:
            self.assertIn(name, choices)


class RoundPairingFormSetTests(TestCase):
    def _management_data(self, total):
        return {
            "form-TOTAL_FORMS": str(total),
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }

    def test_invalid_form_in_formset(self):
        data = self._management_data(2)
        data.update({
            "form-0-round": "1",
            "form-0-pairing_type": "Swiss",
            "form-0-start_round": "0",
            "form-1-round": "2",
            "form-1-pairing_type": "KotH",
            "form-1-start_round": "2",
        })
        formset = RoundPairingFormSet(data)
        self.assertFalse(formset.is_valid())
