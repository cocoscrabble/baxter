"""The WESPA integration: reading the list.

See ``plans/PLAN_WESPA.md``. What these pin down, in the order the plan argues
them, is mostly *restraint* — the pull holds far more back than it writes.
"""

import json

from django.test import TestCase

from tournaments.wespa_api import WespaParseError, parse_wespa


def document(*players):
    """A WESPA document as the endpoint serves one."""
    return json.dumps({"players": list(players)})


def row(wespa_id, name, rating=1500, country="NZL"):
    return {
        "playerid": wespa_id,
        "name": name,
        "country": country,
        "cswrating": rating,
    }


class ParseTests(TestCase):
    def test_the_endpoints_shape_is_read(self):
        rows = parse_wespa(document(row(5, "Adam Logan", 2070, "CAN")))
        self.assertEqual(
            rows,
            [{"wespa_id": 5, "name": "Adam Logan", "country": "CAN", "rating": 2070}],
        )

    def test_a_bare_list_is_accepted(self):
        rows = parse_wespa(json.dumps([row(5, "Adam Logan")]))
        self.assertEqual(len(rows), 1)

    def test_bytes_and_text_are_one_code_path(self):
        """The fetched bytes and an uploaded file must not diverge."""
        text = document(row(5, "Adam Logan"))
        self.assertEqual(parse_wespa(text), parse_wespa(text.encode()))

    def test_a_null_rating_survives_as_none(self):
        rows = parse_wespa(document({"playerid": 5, "name": "A", "cswrating": None}))
        self.assertIsNone(rows[0]["rating"])
        self.assertEqual(rows[0]["country"], "")

    def test_an_unreadable_row_is_fatal_not_skipped(self):
        """Dropping rows quietly would mean a rating silently failing to update."""
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), {"name": "no id"}))
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), row(6, "")))

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), row(5, "B")))

    def test_junk_is_a_message_not_a_traceback(self):
        for bad in ["not json", "{}", '{"players": []}', "42"]:
            with self.assertRaises(WespaParseError):
                parse_wespa(bad)
