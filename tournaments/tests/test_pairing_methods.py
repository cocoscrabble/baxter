from unittest import TestCase

from tournaments.pairing.methods import (
    PairingMethod,
    pairing_method_schedule,
    swiss_contenders_schedule,
)


class SwissContendersScheduleTests(TestCase):
    def test_equal_thirds(self):
        schedule = swiss_contenders_schedule(entrants=18, total_rounds=24)

        self.assertEqual(schedule.method, PairingMethod.SWISS_CONTENDERS)
        self.assertEqual(
            schedule.blocks,
            [
                {"pairing": "SwissNoRepeats", "rounds": 8, "pair_from": 1},
                {"pairing": "Swiss", "rounds": 8, "pair_from": 1},
                {"pairing": "COP", "rounds": 8, "pair_from": 1},
            ],
        )

    def test_first_extra_round_goes_to_minimal_repeat_swiss(self):
        schedule = swiss_contenders_schedule(entrants=18, total_rounds=25)

        self.assertEqual([block["rounds"] for block in schedule.blocks], [8, 9, 8])

    def test_second_extra_round_goes_to_no_repeat_swiss(self):
        schedule = swiss_contenders_schedule(entrants=18, total_rounds=26)

        self.assertEqual([block["rounds"] for block in schedule.blocks], [9, 9, 8])

    def test_rejects_short_event(self):
        with self.assertRaisesRegex(ValueError, "at least 14 rounds"):
            swiss_contenders_schedule(entrants=18, total_rounds=13)

    def test_minimum_length_event_uses_the_remainder_rule(self):
        schedule = swiss_contenders_schedule(entrants=6, total_rounds=14)

        self.assertEqual([block["rounds"] for block in schedule.blocks], [5, 5, 4])

    def test_even_field_rejects_too_many_no_repeat_rounds(self):
        with self.assertRaisesRegex(
            ValueError,
            "needs 10 no-repeat rounds, but 10 entrants can support at most 9",
        ):
            swiss_contenders_schedule(entrants=10, total_rounds=29)

    def test_odd_field_includes_one_distinct_bye_in_no_repeat_capacity(self):
        schedule = swiss_contenders_schedule(entrants=9, total_rounds=26)

        self.assertEqual(schedule.blocks[0]["rounds"], 9)

    def test_odd_field_rejects_a_second_bye(self):
        with self.assertRaisesRegex(
            ValueError,
            "needs 10 no-repeat rounds, but 9 entrants can support at most 9",
        ):
            swiss_contenders_schedule(entrants=9, total_rounds=29)

    def test_first_class_dispatch(self):
        schedule = pairing_method_schedule(
            PairingMethod.SWISS_CONTENDERS,
            entrants=18,
            total_rounds=24,
        )

        self.assertEqual(schedule.method, PairingMethod.SWISS_CONTENDERS)
