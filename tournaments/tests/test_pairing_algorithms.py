from unittest import TestCase

from tournaments.pairing.base import (
    EntrantData,
    PairingData,
    PairingError,
    PlayerData,
    Repeats,
    ResultSlipData,
    standings_after_round,
)
from tournaments.pairing.round_pairing import RP, RoundPairing
from tournaments.pairing.basic import (
    pair_koth,
    pair_qoth,
    pair_round_robin,
    pair_double_round_robin,
    pair_charlottesville,
)
from tournaments.pairing.quads import (
    pair_clustered_quads,
    pair_distributed_quads,
    pair_equalized_quads,
    pair_sixes,
)
from tournaments.pairing.swiss import pair_swiss


def make_pd(standings_str, round_pairings=None):
    """Create PairingData where seedings (round 0) match the letter order.

    Each letter becomes a player whose rating ensures the seeding order
    matches the string order: first letter = highest rated, etc.
    """
    n = len(standings_str)
    entrants = [
        EntrantData(PlayerData(name=ch, rating=(n - i) * 100))
        for i, ch in enumerate(standings_str)
    ]
    return PairingData(
        result_slips=[],
        entrants=entrants,
        repeats=Repeats(),
        round_pairings=round_pairings or [],
    )


def pairings_str(pairings):
    """Flatten pairings into a string: 'AB' means first pair is A vs B, etc."""
    return "".join(p.first.name + p.second.name for p in pairings)


class PairingTestCase(TestCase):
    """Base class providing assert_pairings helper.

    assert_pairings(pair_fn, standings, expected, ...) creates a field of
    players whose seeding order matches `standings` (one letter per player),
    runs `pair_fn`, and checks that the resulting pairing sequence matches
    `expected` (read in consecutive pairs: "ABCD" means A-B, C-D).
    """

    def assert_pairings(
        self,
        pair_fn,
        standings,
        expected,
        round=1,
        start_round=0,
        pairing=RP.KotH,
        round_pairings=None,
    ):
        standings = standings.replace(" ", "")
        expected = expected.replace(" ", "")
        pd = make_pd(standings, round_pairings=round_pairings)
        rp = RoundPairing(round=round, start_round=start_round, pairing=pairing)
        pairings = pair_fn(pd, rp)
        self.assertEqual(pairings_str(pairings), expected)


# ── KotH ────────────────────────────────────────────────


class KotHTests(PairingTestCase):
    """KotH pairs consecutively: 1v2, 3v4, ..."""

    def test_4_players(self):
        self.assert_pairings(pair_koth, "ABCD", "ABCD")

    def test_6_players(self):
        self.assert_pairings(pair_koth, "ABCDEF", "ABCDEF")

    def test_8_players(self):
        self.assert_pairings(pair_koth, "ABCDEFGH", "ABCDEFGH")


# ── QotH ────────────────────────────────────────────────


class QotHTests(PairingTestCase):
    """QotH cross-pairs in groups of 4: 1v3, 2v4.
    When field % 4 == 2 the last 6 are paired 1-4, 2-5, 3-6.
    """

    def test_8_players(self):
        # Two groups of 4: (A,C)(B,D) | (E,G)(F,H)
        self.assert_pairings(pair_qoth, "ABCD EFGH", "ACBD EGFH")

    def test_12_players(self):
        # Three groups of 4
        self.assert_pairings(pair_qoth, "ABCD EFGH IJKL", "ACBD EGFH IKJL")

    def test_6_players(self):
        # 6 % 4 == 2: last 6 paired as 1-4, 2-5, 3-6
        self.assert_pairings(pair_qoth, "ABCDEF", "ADBECF")

    def test_10_players(self):
        # 10 % 4 == 2: first group of 4 cross-paired, last 6 as 1-4, 2-5, 3-6
        self.assert_pairings(pair_qoth, "ABCD EFGHIJ", "ACBD EHFIGJ")


# ── Round Robin ─────────────────────────────────────────


class RoundRobinTests(PairingTestCase):
    """Round robin for N players over N-1 rounds.

    Uses the circle method: player 0 stays fixed, others rotate.
    start_round=1 means standings come from seedings (round 0).
    """

    def _rr(self, standings, expected, round):
        self.assert_pairings(
            pair_round_robin,
            standings,
            expected,
            round=round,
            start_round=1,
            pairing=RP.RoundRobin,
        )

    def test_4_players(self):
        self._rr("ABCD", "ADBC", round=1)
        self._rr("ABCD", "ACDB", round=2)
        self._rr("ABCD", "ABCD", round=3)

    def test_6_players(self):
        self._rr("ABCDEF", "AFBECD", round=1)
        self._rr("ABCDEF", "AEFDBC", round=2)
        self._rr("ABCDEF", "ADECFB", round=3)
        self._rr("ABCDEF", "ACDBEF", round=4)
        self._rr("ABCDEF", "ABCFDE", round=5)


# ── Double Round Robin ──────────────────────────────────


class DoubleRoundRobinTests(PairingTestCase):
    """Double round robin: consecutive pairs of rounds share the same pairing."""

    def _drr(self, standings, expected, round):
        self.assert_pairings(
            pair_double_round_robin,
            standings,
            expected,
            round=round,
            start_round=1,
            pairing=RP.DoubleRoundRobin,
        )

    def test_4_players(self):
        # repeat each RR pairing twice
        self._drr("ABCD", "ADBC", round=1)
        self._drr("ABCD", "ADBC", round=2)
        self._drr("ABCD", "ACDB", round=3)
        self._drr("ABCD", "ACDB", round=4)
        self._drr("ABCD", "ABCD", round=5)
        self._drr("ABCD", "ABCD", round=6)


# ── Charlottesville ─────────────────────────────────────


class CharlottesvilleTests(PairingTestCase):
    """Charlottesville: snake split into 2 groups, cross-group round robin.

    For 8 players ABCDEFGH:
      Group 1 (indices 1,3,5,7): B, D, F, H
      Group 2 (indices 0,2,4,6): A, C, E, G  →  reversed: G, E, C, A
    Group 2 is rotated each round and paired against group 1.
    """

    def _cv(self, standings, expected, round):
        self.assert_pairings(
            pair_charlottesville,
            standings,
            expected,
            round=round,
            start_round=1,
            pairing=RP.Charlottesville,
        )

    def test_8_players(self):
        self._cv("ABCDEFGH", "BGDEFCHA", round=1)
        self._cv("ABCDEFGH", "BEDCFAHG", round=2)
        self._cv("ABCDEFGH", "BCDAFGHE", round=3)
        self._cv("ABCDEFGH", "BADGFEHC", round=4)


# ── Clustered Quads ─────────────────────────────────────


class ClusteredQuadsTests(PairingTestCase):
    """Clustered quads: groups of 4 from consecutive standings.

    For 8 players: quads [A,B,C,D] and [E,F,G,H].
    """

    def _cq(self, standings, expected, pos):
        rps = [RoundPairing(i, 0, RP.Quads_Clustered) for i in range(1, 4)]
        self.assert_pairings(
            pair_clustered_quads,
            standings,
            expected,
            round=pos,
            start_round=0,
            pairing=RP.Quads_Clustered,
            round_pairings=rps,
        )

    def test_8_players(self):
        self._cq("ABCD EFGH", "ADBC EHFG", pos=1)
        self._cq("ABCD EFGH", "ACBD EGFH", pos=2)
        self._cq("ABCD EFGH", "ABCD EFGH", pos=3)

    def test_10_players_with_hex(self):
        # 10 % 4 == 2: quad [A,B,C,D], hex [E,F,G,H,I,J]
        self._cq("ABCD EFGHIJ", "ADBC EFGHIJ", pos=1)
        self._cq("ABCD EFGHIJ", "ACBD EGHIFJ", pos=2)
        self._cq("ABCD EFGHIJ", "ABCD EHFIGJ", pos=3)


# ── Distributed Quads ───────────────────────────────────


class DistributedQuadsTests(PairingTestCase):
    """Distributed quads: interleave standings into groups.

    For 8 players with stride=2: quads [A,C,E,G] and [B,D,F,H].
    """

    def _dq(self, standings, expected, pos):
        rps = [RoundPairing(i, 0, RP.Quads_Distributed) for i in range(1, 4)]
        self.assert_pairings(
            pair_distributed_quads,
            standings,
            expected,
            round=pos,
            start_round=0,
            pairing=RP.Quads_Distributed,
            round_pairings=rps,
        )

    def test_8_players(self):
        self._dq("ABCDEFGH", "AGCEBHDF", pos=1)
        self._dq("ABCDEFGH", "AECGBFDH", pos=2)
        self._dq("ABCDEFGH", "ACEGBDFH", pos=3)


# ── Equalized Quads ─────────────────────────────────────────


class EqualizedQuadsTests(PairingTestCase):
    """Equalized quads: snake distribution to equalize opponent strength.

    For 8 players with stride=2:
      Snake: [A,B] [D,C] [E,F] [H,G]
      Quads: [A,D,E,H] and [B,C,F,G]
    """

    def _eq(self, standings, expected, pos):
        rps = [RoundPairing(i, 0, RP.Quads_Equalized) for i in range(1, 4)]
        self.assert_pairings(
            pair_equalized_quads,
            standings,
            expected,
            round=pos,
            start_round=0,
            pairing=RP.Quads_Equalized,
            round_pairings=rps,
        )

    def test_8_players(self):
        self._eq("ABCDEFGH", "AHDEBGCF", pos=1)
        self._eq("ABCDEFGH", "AEDHBFCG", pos=2)
        self._eq("ABCDEFGH", "ADEHBCFG", pos=3)


# ── Sixes ───────────────────────────────────────────────


class SixesTests(PairingTestCase):
    """Sixes: Equalized-quads-style snake distribution into groups of 6.

    For 12 players with stride=2:
      Snake: [A,B] [D,C] [E,F] [H,G] [I,J] [L,K]
      Hexes: [A,D,E,H,I,L] and [B,C,F,G,J,K]
    """

    def _sx(self, standings, expected, pos):
        rps = [RoundPairing(i, 0, RP.Sixes) for i in range(1, 4)]
        self.assert_pairings(
            pair_sixes,
            standings,
            expected,
            round=pos,
            start_round=0,
            pairing=RP.Sixes,
            round_pairings=rps,
        )

    def test_12_players(self):
        self._sx("ABCDEF GHIJKL", "ADEHILBCFGJK", pos=1)
        self._sx("ABCDEFGHIJKL", "AEHIDLBFGJCK", pos=2)
        self._sx("ABCDEFGHIJKL", "AHDIELBGCJFK", pos=3)

    def test_10_players_with_quad(self):
        # 10 % 6 == 4: 1 hex [A,B,C,D,E,F], 1 quad [G,H,I,J]
        self._sx("ABCDEFGHIJ", "ABCDEFGJHI", pos=1)
        self._sx("ABCDEFGHIJ", "ACDEBFGIHJ", pos=2)
        self._sx("ABCDEFGHIJ", "ADBECFGHIJ", pos=3)


# ── Swiss Initial ───────────────────────────────────────


class SwissInitialTests(PairingTestCase):
    """Swiss initial pairing: top half vs bottom half."""

    def _sw(self, standings, expected):
        self.assert_pairings(
            pair_swiss,
            standings,
            expected,
            round=1,
            start_round=0,
            pairing=RP.Swiss,
        )

    def test_4_players(self):
        # A-C, B-D
        self._sw("ABCD", "ACBD")

    def test_6_players(self):
        # A-D, B-E, C-F
        self._sw("ABCDEF", "ADBECF")

    def test_8_players(self):
        # A-E, B-F, C-G, D-H
        self._sw("ABCDEFGH", "AEBFCGDH")


# ── Swiss small fields (regression: must not hang) ──────


class SwissSmallFieldTests(PairingTestCase):
    """Swiss round 2+ on tiny fields must terminate.

    The repro was 4 players in two win-groups after round 1: merging the bottom
    group collapses everything into a single sub-6 group, and the old merge loop
    (`while len(groups.bottom) < 6: merge_bottom()`) spun forever because
    merge_bottom is a no-op once one group remains.
    """

    def _round2(self, standings_str, round1_results):
        """Pair round 2 given round-1 (winner, loser) results.

        Seeding order matches `standings_str`; each result is a full-round game
        so standings_after_round(1) yields the expected win-groups.
        """
        n = len(standings_str)
        entrants = [
            EntrantData(PlayerData(name=ch, rating=(n - i) * 100))
            for i, ch in enumerate(standings_str)
        ]
        slips = [
            ResultSlipData(
                round=1,
                winner_name=w,
                loser_name=loser,
                winner_score=400,
                loser_score=300,
                winner_started=True,
            )
            for (w, loser) in round1_results
        ]
        pd = PairingData(
            result_slips=slips,
            entrants=entrants,
            repeats=Repeats(),
            round_pairings=[],
        )
        rp = RoundPairing(round=2, start_round=1, pairing=RP.Swiss)
        return pair_swiss(pd, rp)

    def _assert_valid(self, pairings, standings_str):
        """Every paired player is distinct and drawn from the field."""
        paired = [p.first.name for p in pairings] + [
            p.second.name for p in pairings
        ]
        self.assertEqual(len(paired), len(set(paired)), "a player was paired twice")
        self.assertTrue(set(paired) <= set(standings_str))

    def test_2_players(self):
        pairings = self._round2("AB", [("A", "B")])
        self.assertEqual(len(pairings), 1)
        self._assert_valid(pairings, "AB")

    def test_4_players_two_win_groups(self):
        # A beat C, B beat D → two win-groups of two; the original hang repro.
        pairings = self._round2("ABCD", [("A", "C"), ("B", "D")])
        self.assertEqual(len(pairings), 2)
        self._assert_valid(pairings, "ABCD")

    def test_3_players(self):
        # Odd field: one game played, the unpaired seed carries a bye upstream.
        pairings = self._round2("ABC", [("A", "B")])
        self.assertEqual(len(pairings), 1)
        self._assert_valid(pairings, "ABC")

    def test_5_players(self):
        pairings = self._round2("ABCDE", [("A", "C"), ("B", "D")])
        self.assertEqual(len(pairings), 2)
        self._assert_valid(pairings, "ABCDE")


# ── Dropped entrants & late adds ────────────────────────


class DroppedAndLateEntrantTests(TestCase):
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

    def test_dropping_out_of_played_round_robin_raises(self):
        entrants = [
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("B", 1500)),
            EntrantData(PlayerData("C", 1400), dropped=True),  # withdraws mid-RR
            EntrantData(PlayerData("D", 1300)),
        ]
        rps = [RoundPairing(r, 0, RP.RoundRobin) for r in (1, 2, 3)]
        slips = [
            ResultSlipData(1, "A", "C", 400, 300, True),
            ResultSlipData(1, "B", "D", 400, 300, True),
        ]
        pd = PairingData(
            result_slips=slips, entrants=entrants, repeats=Repeats(),
            round_pairings=rps,
        )
        with self.assertRaises(PairingError) as cm:
            pair_round_robin(pd, RoundPairing(2, 0, RP.RoundRobin))
        self.assertIn("withdrew", str(cm.exception))
