"""Bracket derivation tests.

Pure: no database. Everything here exercises ``tournaments/playoff.py`` directly,
which is the point of keeping the derivation a function of (config, results).
"""

from django.test import SimpleTestCase

from tournaments.pairing.base import Player, ResultSlipData
from tournaments.playoff import (
    CHAMPIONSHIP,
    CONSOLATION_SEMIFINAL,
    FIFTH_PLACE,
    QUARTERFINAL,
    SEMIFINAL,
    SEVENTH_PLACE,
    THIRD_PLACE,
    GameStatus,
    PlayoffConfig,
    SeriesStatus,
    Timing,
    build_bracket,
    default_stage_games,
    final_placements,
    validate_config,
)

QUAL_ROUND = 10


def config(count=4, qual=QUAL_ROUND, timing=Timing.POSTSCRIPT, games=3, **overrides):
    """A playoff config over players named P1…Pn, seeded in that order."""
    stage_games = default_stage_games(count, games)
    stage_games.update(overrides)
    return PlayoffConfig(
        qualification_round=qual,
        qualifier_count=count,
        timing=str(timing),
        stage_games=stage_games,
        seeds=tuple(f"P{i}" for i in range(1, count + 1)),
    )


def slip(round, winner, loser, winner_score=420, loser_score=380):
    return ResultSlipData(
        round=round,
        winner_key=winner,
        loser_key=loser,
        winner_score=winner_score,
        loser_score=loser_score,
        winner_started=True,
    )


def draw(round, a, b, score=400):
    """A drawn game. Which name lands in ``winner`` is arbitrary for a tie."""
    return slip(round, a, b, score, score)


def statuses(series):
    return [g.status for g in series.games]


class WindowTests(SimpleTestCase):
    def test_two_qualifiers_play_an_immediate_championship(self):
        bracket = build_bracket(config(count=2), [])
        final = bracket.get(CHAMPIONSHIP)
        self.assertEqual((final.high, final.low), ("P1", "P2"))
        self.assertEqual(final.start_round, QUAL_ROUND + 1)
        self.assertEqual(list(bracket.rounds), [11, 12, 13])

    def test_four_qualifiers_seed_one_four_and_two_three(self):
        bracket = build_bracket(config(count=4), [])
        self.assertEqual(
            (bracket.get(SEMIFINAL, 0).high, bracket.get(SEMIFINAL, 0).low),
            ("P1", "P4"),
        )
        self.assertEqual(
            (bracket.get(SEMIFINAL, 1).high, bracket.get(SEMIFINAL, 1).low),
            ("P2", "P3"),
        )
        # Semifinal window 11–13, final window 14–16.
        self.assertEqual(bracket.get(SEMIFINAL, 0).start_round, 11)
        self.assertEqual(bracket.get(CHAMPIONSHIP).start_round, 14)
        self.assertEqual(bracket.get(THIRD_PLACE).start_round, 14)
        self.assertEqual(list(bracket.rounds), list(range(11, 17)))

    def test_eight_qualifiers_use_standard_bracket_paths(self):
        bracket = build_bracket(config(count=8), [])
        pairs = [
            (bracket.get(QUARTERFINAL, i).high, bracket.get(QUARTERFINAL, i).low)
            for i in range(4)
        ]
        self.assertEqual(
            pairs, [("P1", "P8"), ("P4", "P5"), ("P2", "P7"), ("P3", "P6")]
        )
        self.assertEqual(bracket.get(CONSOLATION_SEMIFINAL, 0).start_round, 14)
        self.assertEqual(bracket.get(FIFTH_PLACE).start_round, 17)
        self.assertEqual(list(bracket.rounds), list(range(11, 20)))

    def test_window_length_is_the_longest_series_in_it(self):
        # Best-of-1 semifinals, best-of-5 final: windows are 1 then 5 rounds.
        bracket = build_bracket(config(count=4, semifinal=1, championship=5), [])
        self.assertEqual(bracket.get(SEMIFINAL, 0).start_round, 11)
        self.assertEqual(bracket.get(CHAMPIONSHIP).start_round, 12)
        self.assertEqual(list(bracket.rounds), list(range(11, 17)))

    def test_stage_lengths_are_independent(self):
        bracket = build_bracket(
            config(count=8, quarterfinal=1, semifinal=3, championship=5,
                   third_place=3, consolation_semifinal=3, fifth_place=3,
                   seventh_place=3),
            [],
        )
        self.assertEqual(bracket.get(QUARTERFINAL, 0).max_games, 1)
        self.assertEqual(bracket.get(SEMIFINAL, 0).max_games, 3)
        self.assertEqual(bracket.get(CHAMPIONSHIP).max_games, 5)
        # QF window 1 round, SF window 3, final window 5 (the championship).
        self.assertEqual(bracket.get(SEMIFINAL, 0).start_round, 12)
        self.assertEqual(bracket.get(CHAMPIONSHIP).start_round, 15)


class SchedulingTests(SimpleTestCase):
    def test_certainly_necessary_games_are_scheduled_up_front(self):
        # Best of 3: nobody can clinch in one game, so games 1 and 2 can be
        # printed before either is played. Game 3 waits.
        semi = build_bracket(config(count=4), []).get(SEMIFINAL, 0)
        self.assertEqual(
            statuses(semi),
            [GameStatus.SCHEDULED, GameStatus.SCHEDULED, GameStatus.PENDING],
        )

    def test_best_of_five_schedules_its_first_three_games(self):
        semi = build_bracket(config(count=4, semifinal=5), []).get(SEMIFINAL, 0)
        self.assertEqual(
            statuses(semi),
            [GameStatus.SCHEDULED] * 3 + [GameStatus.PENDING] * 2,
        )

    def test_single_game_series_schedules_its_only_game(self):
        semi = build_bracket(config(count=4, semifinal=1), []).get(SEMIFINAL, 0)
        self.assertEqual(statuses(semi), [GameStatus.SCHEDULED])

    def test_third_game_is_scheduled_once_the_series_is_level(self):
        results = [slip(11, "P1", "P4"), slip(12, "P4", "P1")]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual(semi.status, SeriesStatus.IN_PROGRESS)
        self.assertEqual(statuses(semi)[2], GameStatus.SCHEDULED)

    def test_early_clinch_makes_the_last_game_unnecessary(self):
        results = [slip(11, "P1", "P4"), slip(12, "P1", "P4")]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual(semi.status, SeriesStatus.CLINCHED)
        self.assertEqual(semi.winner, "P1")
        self.assertEqual(semi.decided_by, "majority")
        self.assertEqual(statuses(semi)[2], GameStatus.NOT_NEEDED)
        self.assertEqual(semi.scheduled_games, ())

    def test_full_length_series_clinches_on_the_last_game(self):
        results = [
            slip(11, "P1", "P4"), slip(12, "P4", "P1"), slip(13, "P4", "P1"),
        ]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual(semi.status, SeriesStatus.CLINCHED)
        self.assertEqual(semi.winner, "P4")
        self.assertEqual((semi.high_score, semi.low_score), (1.0, 2.0))

    def test_a_forfeit_result_clinches_like_any_other(self):
        # A withdrawal mid-bracket is recorded as a result, not as a dropped
        # entrant — which is what keeps the series from stalling forever.
        results = [
            slip(11, "P1", "P4", 100, 0), slip(12, "P1", "P4", 100, 0),
        ]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual(semi.status, SeriesStatus.CLINCHED)
        self.assertEqual(semi.winner, "P1")

    def test_scheduled_by_round_groups_live_games(self):
        bracket = build_bracket(config(count=4), [])
        by_round = bracket.scheduled_by_round()
        self.assertEqual(sorted(by_round), [11, 12])
        self.assertEqual(len(by_round[11]), 2)  # both semifinals, game 1
        self.assertEqual({g.number for _, g in by_round[12]}, {2})

    def test_a_pending_series_schedules_nothing(self):
        bracket = build_bracket(config(count=4), [])
        final = bracket.get(CHAMPIONSHIP)
        self.assertEqual(final.status, SeriesStatus.PENDING)
        self.assertEqual(final.games, ())


class AdvancementTests(SimpleTestCase):
    def semis_done(self):
        """P1 beats P4 2–0; P3 beats P2 2–1."""
        return [
            slip(11, "P1", "P4"), slip(12, "P1", "P4"),
            slip(11, "P2", "P3"), slip(12, "P3", "P2"), slip(13, "P3", "P2"),
        ]

    def test_winners_advance_and_losers_go_to_third_place(self):
        bracket = build_bracket(config(count=4), self.semis_done())
        final = bracket.get(CHAMPIONSHIP)
        third = bracket.get(THIRD_PLACE)
        self.assertEqual((final.high, final.low), ("P1", "P3"))
        self.assertEqual((third.high, third.low), ("P2", "P4"))
        self.assertEqual(final.status, SeriesStatus.SCHEDULED)

    def test_participants_are_ordered_by_qualification_seed(self):
        # P3 won its semifinal but is seeded below P1, so P1 is the high seed.
        bracket = build_bracket(config(count=4), self.semis_done())
        self.assertEqual(bracket.get(CHAMPIONSHIP).high, "P1")

    def test_eight_player_consolation_half_keeps_everyone_playing(self):
        # Every quarterfinal decided 2–0 by the better seed.
        results = []
        for round_num in (11, 12):
            for high, low in [("P1", "P8"), ("P4", "P5"), ("P2", "P7"), ("P3", "P6")]:
                results.append(slip(round_num, high, low))
        bracket = build_bracket(config(count=8), results)
        self.assertEqual(
            (bracket.get(SEMIFINAL, 0).high, bracket.get(SEMIFINAL, 0).low),
            ("P1", "P4"),
        )
        self.assertEqual(
            (bracket.get(CONSOLATION_SEMIFINAL, 0).high,
             bracket.get(CONSOLATION_SEMIFINAL, 0).low),
            ("P5", "P8"),
        )
        # The semifinal window schedules games for all eight qualifiers.
        window_rounds = set(range(14, 17))
        playing = {
            name
            for round_num, games in bracket.scheduled_by_round().items()
            if round_num in window_rounds
            for series, _ in games
            for name in series.participants
        }
        self.assertEqual(playing, {f"P{i}" for i in range(1, 9)})


class TieTests(SimpleTestCase):
    def test_a_drawn_game_counts_half_to_each(self):
        results = [draw(11, "P1", "P4"), slip(12, "P1", "P4")]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual((semi.high_score, semi.low_score), (1.5, 0.5))
        # 1.5 is not *more* than half of 3, so the series is still live.
        self.assertEqual(semi.status, SeriesStatus.IN_PROGRESS)
        self.assertEqual(statuses(semi)[2], GameStatus.SCHEDULED)

    def test_win_plus_two_draws_is_a_majority(self):
        results = [slip(11, "P1", "P4"), draw(12, "P1", "P4"), draw(13, "P1", "P4")]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual((semi.high_score, semi.low_score), (2.0, 1.0))
        self.assertEqual(semi.status, SeriesStatus.CLINCHED)
        self.assertEqual(semi.decided_by, "majority")

    def test_level_series_is_decided_on_spread_within_the_series(self):
        results = [
            slip(11, "P1", "P4", 500, 400),   # P1 +100
            slip(12, "P4", "P1", 450, 400),   # P4 +50
            draw(13, "P1", "P4"),
        ]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual((semi.high_score, semi.low_score), (1.5, 1.5))
        self.assertEqual(semi.winner, "P1")
        self.assertEqual(semi.decided_by, "spread")
        self.assertEqual(semi.high_spread, 50)

    def test_spread_outside_the_series_never_decides_it(self):
        # P4 has a huge qualification-round spread, and a huge win in the *other*
        # semifinal's rounds; neither may touch this series.
        results = [
            slip(1, "P4", "P2", 900, 100),
            slip(11, "P1", "P4", 500, 400),
            slip(12, "P4", "P1", 450, 400),
            draw(13, "P1", "P4"),
        ]
        semi = build_bracket(config(count=4), results).get(SEMIFINAL, 0)
        self.assertEqual(semi.winner, "P1")
        self.assertEqual(semi.high_spread, 50)

    def test_drawn_single_game_series_is_unresolved(self):
        # The one case series spread cannot break: a drawn best-of-1 is 0–0.
        results = [draw(11, "P1", "P2")]
        bracket = build_bracket(config(count=2, championship=1), results)
        final = bracket.get(CHAMPIONSHIP)
        self.assertEqual(final.status, SeriesStatus.TIED)
        self.assertIsNone(final.winner)
        self.assertEqual(final.scheduled_games, ())


class PlacementTests(SimpleTestCase):
    def standings_for(self, names, extra=()):
        """Standings-shaped players: bracket players first, then a main field."""
        players = []
        for i, name in enumerate(names):
            p = Player(name)
            p.score = 10 - i
            p.spread = 1000 - 100 * i
            players.append(p)
        players.extend(extra)
        return players

    def numbers_for(self, players):
        return {p.name: i + 1 for i, p in enumerate(players)}

    def placed(self, bracket, standings=None, numbers=None):
        standings = standings if standings is not None else self.standings_for(
            bracket.config.seeds
        )
        numbers = numbers if numbers is not None else self.numbers_for(standings)
        return final_placements(bracket, standings, numbers)

    def four_player_finished(self):
        """Semis: P1 d. P4 2–1, P2 d. P3 2–1. Final: P2 d. P1 2–1.
        Third place: P3 d. P4 2–0.

        So the runner-up P1 finishes the playoff 3–3 while third-placed P3
        finishes 3–2 — the 2023 NACC Division Two shape."""
        return [
            slip(11, "P1", "P4"), slip(12, "P4", "P1"), slip(13, "P1", "P4"),
            slip(11, "P2", "P3"), slip(12, "P3", "P2"), slip(13, "P2", "P3"),
            slip(14, "P2", "P1"), slip(15, "P1", "P2"), slip(16, "P2", "P1"),
            slip(14, "P3", "P4"), slip(15, "P3", "P4"),
        ]

    def test_bracket_beats_aggregate_record(self):
        results = self.four_player_finished()
        bracket = build_bracket(config(count=4), results)
        self.assertTrue(bracket.complete)
        places = {p.place: p.name for p in self.placed(bracket)}
        self.assertEqual(places[1], "P2")
        self.assertEqual(places[2], "P1")
        self.assertEqual(places[3], "P3")
        self.assertEqual(places[4], "P4")

        # The regression this guards: on playoff games alone the runner-up is
        # 3–3 and third place is 3–2, so any record-based sort inverts them.
        wins = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
        losses = dict(wins)
        for r in results:
            wins[r.winner_key] += 1
            losses[r.loser_key] += 1
        self.assertEqual((wins["P1"], losses["P1"]), (3, 3))
        self.assertEqual((wins["P3"], losses["P3"]), (3, 2))

    def test_eight_player_bracket_places_all_eight(self):
        results = []
        # Quarterfinals: better seed wins 2–0 in every one.
        for round_num in (11, 12):
            for high, low in [("P1", "P8"), ("P4", "P5"), ("P2", "P7"), ("P3", "P6")]:
                results.append(slip(round_num, high, low))
        # Semifinals (14–16) and consolation semifinals, better seed wins 2–0.
        for round_num in (14, 15):
            for high, low in [("P1", "P4"), ("P2", "P3"), ("P5", "P8"), ("P6", "P7")]:
                results.append(slip(round_num, high, low))
        # Final window (17–19).
        for round_num in (17, 18):
            for high, low in [("P1", "P2"), ("P4", "P3"), ("P5", "P6"), ("P8", "P7")]:
                results.append(slip(round_num, high, low))
        bracket = build_bracket(config(count=8), results)
        places = {p.place: p.name for p in self.placed(bracket)}
        self.assertEqual(
            places,
            {1: "P1", 2: "P2", 3: "P4", 4: "P3", 5: "P5", 6: "P6", 7: "P8", 8: "P7"},
        )
        self.assertTrue(all(p.source == "series" for p in self.placed(bracket)[:8]))

    def test_disabled_placement_series_fall_back_to_seed_order(self):
        # Postscript, no consolation half and no third-place series: the
        # semifinal losers take 3–4 and the quarterfinal losers 5–8, by seed.
        cfg = config(
            count=8, third_place=0, consolation_semifinal=0,
            fifth_place=0, seventh_place=0,
        )
        results = []
        for round_num in (11, 12):
            for high, low in [("P1", "P8"), ("P4", "P5"), ("P2", "P7"), ("P3", "P6")]:
                results.append(slip(round_num, high, low))
        for round_num in (14, 15):
            for high, low in [("P1", "P4"), ("P2", "P3")]:
                results.append(slip(round_num, high, low))
        for round_num in (17, 18):
            results.append(slip(round_num, "P2", "P1"))
        bracket = build_bracket(cfg, results)
        placements = self.placed(bracket)
        places = {p.place: p.name for p in placements}
        self.assertEqual(places[1], "P2")
        self.assertEqual(places[2], "P1")
        # Semifinal losers, later elimination, ordered by seed.
        self.assertEqual([places[3], places[4]], ["P3", "P4"])
        # Quarterfinal losers, by seed.
        self.assertEqual([places[5], places[6], places[7], places[8]],
                         ["P5", "P6", "P7", "P8"])
        self.assertEqual(placements[4].source, "seed")

    def test_an_undecided_series_leaves_its_places_unresolved(self):
        results = [slip(11, "P1", "P2")]  # championship 1–0 in a best of 3
        bracket = build_bracket(config(count=2), results)
        placements = self.placed(bracket)
        self.assertEqual([p.name for p in placements[:2]], [None, None])
        self.assertEqual(placements[0].source, "unresolved")
        self.assertIn("not yet decided", placements[0].note)

    def test_tied_series_says_it_needs_a_decider(self):
        bracket = build_bracket(config(count=2, championship=1), [draw(11, "P1", "P2")])
        placements = self.placed(bracket)
        self.assertIn("needs a decider", placements[0].note)

    def test_main_field_follows_the_bracket_and_breaks_ties_on_number(self):
        bracket = build_bracket(config(count=2), [
            slip(11, "P1", "P2"), slip(12, "P1", "P2"),
        ])
        # Two main-field players dead level on wins *and* spread: the tiebreak
        # is entrant number, so the order is deterministic either way round.
        alice, bob = Player("Alice"), Player("Bob")
        for p in (alice, bob):
            p.score, p.spread = 5, 120
        standings = self.standings_for(("P1", "P2"), extra=[bob, alice])
        numbers = {"P1": 1, "P2": 2, "Alice": 3, "Bob": 4}
        placements = final_placements(bracket, standings, numbers)
        self.assertEqual([p.name for p in placements], ["P1", "P2", "Alice", "Bob"])
        self.assertEqual([p.place for p in placements], [1, 2, 3, 4])
        # Same standings in the other order produce the same placement.
        standings = self.standings_for(("P1", "P2"), extra=[alice, bob])
        placements = final_placements(bracket, standings, numbers)
        self.assertEqual([p.name for p in placements], ["P1", "P2", "Alice", "Bob"])


class ConfigTests(SimpleTestCase):
    def test_valid_configs_pass(self):
        self.assertEqual(validate_config(config(count=4)), [])
        self.assertEqual(validate_config(config(count=8, quarterfinal=1)), [])
        self.assertEqual(
            validate_config(config(count=8, timing=Timing.CONCURRENT)), []
        )

    def test_even_series_length_is_rejected(self):
        errors = validate_config(config(count=4, semifinal=2))
        self.assertTrue(any("odd number of games" in e for e in errors))

    def test_unknown_qualifier_count_is_rejected(self):
        cfg = config(count=4)
        cfg = PlayoffConfig(
            qualification_round=cfg.qualification_round,
            qualifier_count=6,
            timing=cfg.timing,
            stage_games=cfg.stage_games,
            seeds=cfg.seeds,
        )
        self.assertTrue(validate_config(cfg))

    def test_concurrent_requires_every_placement_series(self):
        errors = validate_config(
            config(count=4, timing=Timing.CONCURRENT, third_place=0)
        )
        self.assertTrue(any("no game" in e for e in errors))

    def test_postscript_may_disable_placement_series(self):
        self.assertEqual(validate_config(config(count=4, third_place=0)), [])

    def test_disabling_the_consolation_half_disables_what_needs_it(self):
        cfg = config(count=8, consolation_semifinal=0)
        bracket = build_bracket(cfg, [])
        self.assertIsNone(bracket.get(FIFTH_PLACE))
        self.assertIsNone(bracket.get(SEVENTH_PLACE))
        self.assertIsNone(bracket.get(CONSOLATION_SEMIFINAL, 0))
        # …and the semifinal window shrinks to what is actually played.
        self.assertEqual(validate_config(cfg), [])

    def test_wrong_number_of_seeds_is_rejected(self):
        cfg = PlayoffConfig(
            qualification_round=10,
            qualifier_count=4,
            timing=str(Timing.POSTSCRIPT),
            stage_games=default_stage_games(4),
            seeds=("P1", "P2"),
        )
        self.assertTrue(any("Expected 4 qualifiers" in e for e in validate_config(cfg)))

    def test_duplicate_qualifier_is_rejected(self):
        cfg = PlayoffConfig(
            qualification_round=10,
            qualifier_count=4,
            timing=str(Timing.POSTSCRIPT),
            stage_games=default_stage_games(4),
            seeds=("P1", "P1", "P2", "P3"),
        )
        self.assertTrue(any("qualified twice" in e for e in validate_config(cfg)))


class ReservationTests(SimpleTestCase):
    def test_every_qualifier_is_reserved_for_every_playoff_round(self):
        bracket = build_bracket(config(count=8), [])
        reserved = bracket.reserved_keys_by_round()
        self.assertEqual(sorted(reserved), list(range(11, 20)))
        for names in reserved.values():
            self.assertEqual(set(names), {f"P{i}" for i in range(1, 9)})
