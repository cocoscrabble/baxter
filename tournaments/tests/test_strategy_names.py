"""The pairing strategy enum: what is an identifier and what is a label.

``RP``'s value is a wire identifier. It crosses into the Rust engine as
``{"pairing": "SwissNoRepeats"}``, it is what a division's saved schedule holds,
and it is what every ``round_pairings_saved`` payload in the event log names.
Renaming one breaks pairing for existing divisions and stops their logs
replaying — silently, because nothing about a rename looks dangerous.

``label`` is the half that is free to change, and these tests are the line
between them.
"""

import json
from unittest import TestCase

import scrabble_pairing_py

from tournaments.pairing.round_pairing import ABBREV, RP, STRATEGY_TYPES


class IdentifierTests(TestCase):
    def test_every_value_is_the_member_name(self):
        # The rule that makes a rename obvious: if the value ever stops being the
        # member's own name, someone has put a label in it.
        for strategy in RP:
            with self.subTest(strategy=strategy.name):
                self.assertEqual(strategy.value, strategy.name)

    def test_every_strategy_has_a_label(self):
        for strategy in RP:
            with self.subTest(strategy=strategy.name):
                self.assertTrue(strategy.label.strip())

    def test_labels_are_distinct(self):
        labels = [s.label for s in RP]
        self.assertEqual(len(labels), len(set(labels)))

    def test_a_member_still_compares_equal_to_its_wire_value(self):
        # StrEnum: stored schedules and payloads hold plain strings, and the
        # code compares them against members without converting.
        self.assertEqual(RP.SwissNoRepeats, "SwissNoRepeats")
        self.assertEqual(RP("SwissNoRepeats"), RP.SwissNoRepeats)

    def test_the_shorthand_covers_every_strategy(self):
        # make_pairings("KH:3 SW:5") looks codes up here, so a strategy with no
        # code cannot be written in a schedule spec at all.
        self.assertEqual(set(ABBREV.values()), set(RP))

    def test_the_codes_are_distinct(self):
        self.assertEqual(len(ABBREV), len(set(ABBREV)))


class DropdownOrderTests(TestCase):
    def test_the_offered_order_is_the_declaration_order(self):
        # Derived, not listed twice: the two used to be separate lists in
        # different orders, so reordering the enum moved nothing.
        self.assertEqual(STRATEGY_TYPES, list(RP))


class EngineAcceptanceTests(TestCase):
    """Every offered strategy must be a name the engine knows.

    The engine is the only implementation, so a strategy it does not recognise
    is one a director can pick and then not pair with. Some strategies need more
    than this fixture gives them (COP wants its config, quads want a field that
    divides), so the assertion is only that the *name* resolves — an "unknown
    pairing strategy" is the failure being guarded against.
    """

    PLAYERS = [
        {"name": chr(ord("A") + i), "rating": 1600 - i * 25} for i in range(12)
    ]

    def _error_for(self, strategy):
        out = json.loads(
            scrabble_pairing_py.pair_json(
                json.dumps(
                    {
                        "players": self.PLAYERS,
                        "round_pairings": [
                            {"round": 1, "start_round": 0, "pairing": str(strategy)}
                        ],
                    }
                )
            )
        )
        return out[0]["error"]

    def test_no_offered_strategy_is_unknown_to_the_engine(self):
        for strategy in STRATEGY_TYPES:
            with self.subTest(strategy=strategy.name):
                error = self._error_for(strategy)
                self.assertNotIn(
                    "unknown pairing strategy", (error or "").lower(),
                    f"the engine does not know {str(strategy)!r} — the value is "
                    f"the wire identifier, not the label",
                )
