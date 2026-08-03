from unittest import TestCase

from tournaments.pairing.methods import (
    PairingMethod,
    ThreePhaseOpening,
    pairing_method_schedule,
    three_phase_schedule,
)


class ThreePhaseScheduleTests(TestCase):
    def test_nacc_division_two_shape(self):
        schedule = three_phase_schedule(entrants=18, total_rounds=24)

        self.assertEqual(schedule.method, PairingMethod.THREE_PHASE)
        self.assertEqual(schedule.opening, ThreePhaseOpening.FONTES)
        self.assertEqual(
            schedule.blocks,
            [
                {"pairing": "Quads_Equalized", "rounds": 3, "pair_from": 1},
                {"pairing": "SwissNoRepeats", "rounds": 9, "pair_from": 1},
                {"pairing": "COP", "rounds": 12, "pair_from": 1},
            ],
        )

    def test_nacc_division_one_uses_the_fontes_opening_for_4n_plus_2_field(self):
        schedule = three_phase_schedule(entrants=22, total_rounds=24)

        self.assertEqual(schedule.opening, ThreePhaseOpening.FONTES)
        self.assertEqual(
            schedule.blocks[0],
            {
                "pairing": "Quads_Equalized",
                "rounds": 3,
                "pair_from": 1,
            },
        )

    def test_odd_total_puts_extra_round_in_cop_half(self):
        schedule = three_phase_schedule(
            entrants=28,
            total_rounds=31,
            opening=ThreePhaseOpening.FONTES,
        )

        self.assertEqual([block["rounds"] for block in schedule.blocks], [3, 12, 16])

    def test_compact_field_automatically_uses_full_round_robin(self):
        schedule = three_phase_schedule(entrants=14, total_rounds=20)

        self.assertEqual(schedule.opening, ThreePhaseOpening.ROUND_ROBIN)
        self.assertEqual(
            schedule.blocks,
            [
                {"pairing": "RoundRobin", "rounds": 13, "pair_from": 1},
                {"pairing": "COP", "rounds": 7, "pair_from": 1},
            ],
        )

    def test_director_can_force_fontes_for_compact_field(self):
        schedule = three_phase_schedule(
            entrants=10,
            total_rounds=20,
            opening=ThreePhaseOpening.FONTES,
        )

        self.assertEqual(schedule.opening, ThreePhaseOpening.FONTES)
        self.assertEqual([block["rounds"] for block in schedule.blocks], [3, 7, 10])

    def test_odd_field_uses_same_fontes_phase_shape(self):
        schedule = three_phase_schedule(
            entrants=23,
            total_rounds=24,
            opening=ThreePhaseOpening.FONTES,
        )

        self.assertEqual([block["rounds"] for block in schedule.blocks], [3, 9, 12])

    def test_rejects_short_event(self):
        with self.assertRaisesRegex(ValueError, "at least 14 rounds"):
            three_phase_schedule(entrants=18, total_rounds=13)

    def test_round_robin_must_leave_room_for_cop(self):
        with self.assertRaisesRegex(ValueError, "at least one round"):
            three_phase_schedule(
                entrants=15,
                total_rounds=14,
                opening=ThreePhaseOpening.ROUND_ROBIN,
            )

    def test_first_class_dispatch(self):
        schedule = pairing_method_schedule(
            PairingMethod.THREE_PHASE,
            entrants=18,
            total_rounds=24,
        )

        self.assertEqual(schedule.method, PairingMethod.THREE_PHASE)
