"""Tests for standings_after_round (tournaments/pairing/base.py) — the standings
derivation the app keeps in Python (pairing computation is the Rust engine)."""

from unittest import TestCase

from tournaments.pairing.base import (
    EntrantData,
    PairingData,
    PlayerData,
    Repeats,
    ResultSlipData,
    standings_after_round,
)


class DroppedAndLateEntrantStandingsTests(TestCase):
    """standings_after_round handling of withdrawals and late entrants."""

    def _pd(self, entrants, slips):
        return PairingData(
            result_slips=slips,
            entrants=entrants,
            repeats=Repeats(),
            round_pairings=[],
        )

    def test_dropped_excluded_from_pairing_but_kept_for_display(self):
        entrants = [
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("B", 1500)),
            EntrantData(PlayerData("C", 1400), dropped=True),
            EntrantData(PlayerData("D", 1300)),
        ]
        slips = [
            ResultSlipData(1, "A", "C", 400, 300, True),
            ResultSlipData(1, "B", "D", 400, 300, True),
        ]
        pd = self._pd(entrants, slips)
        pairing = [p.name for p in standings_after_round(pd, 1)]
        self.assertNotIn("C", pairing)  # unpairable once withdrawn
        display = [
            p.name for p in standings_after_round(pd, 1, include_dropped=True)
        ]
        self.assertIn("C", display)  # still shown in standings

    def test_dropped_result_still_counts_for_opponent(self):
        # C withdrew, but the game C lost to A still gives A its win/spread.
        entrants = [
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("C", 1400), dropped=True),
        ]
        slips = [ResultSlipData(1, "A", "C", 450, 300, True)]
        pd = self._pd(entrants, slips)
        standings = standings_after_round(pd, 1)
        a = next(p for p in standings if p.name == "A")
        self.assertEqual(a.wins, 1)
        self.assertEqual(a.spread, 150)

    def test_late_entrant_appended_at_bottom(self):
        entrants = [
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("B", 1500)),
            EntrantData(PlayerData("C", 1400)),
            EntrantData(PlayerData("D", 1300)),
            EntrantData(PlayerData("E", 1200)),  # added after round 1, no results
        ]
        slips = [
            ResultSlipData(1, "A", "C", 400, 300, True),
            ResultSlipData(1, "B", "D", 400, 300, True),
        ]
        pd = self._pd(entrants, slips)
        names = [p.name for p in standings_after_round(pd, 1)]
        self.assertIn("E", names)
        self.assertEqual(names[-1], "E")  # zero record sits at the bottom

    def test_two_late_entrants_in_seed_order(self):
        entrants = [
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("B", 1500)),
            EntrantData(PlayerData("E", 1200)),  # late, lower rated
            EntrantData(PlayerData("F", 1250)),  # late, higher rated
        ]
        slips = [ResultSlipData(1, "A", "B", 400, 300, True)]
        pd = self._pd(entrants, slips)
        names = [p.name for p in standings_after_round(pd, 1)]
        # Newcomers appended in rating order among themselves: F (1250) then E.
        self.assertEqual(names[-2:], ["F", "E"])
