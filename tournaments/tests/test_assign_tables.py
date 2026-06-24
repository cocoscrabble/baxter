from django.test import SimpleTestCase

from tournaments.assign_tables import assign_tables, parse_board_table_map


def labelled(rows):
    """Build a board map dict from (board, table, label) tuples."""
    return {b: {"table": t, "label": l} for b, t, l in rows}


class ParseBoardTableMapTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(parse_board_table_map([]), {})
        self.assertEqual(parse_board_table_map(None), {})

    def test_list_form_with_labels(self):
        raw = [
            {"board": 1, "table": 1, "label": "S1"},
            {"board": 2, "table": 2, "label": "1"},
            {"board": 3, "table": 3, "label": "2"},
        ]
        self.assertEqual(
            parse_board_table_map(raw),
            {
                1: {"table": 1, "label": "S1"},
                2: {"table": 2, "label": "1"},
                3: {"table": 3, "label": "2"},
            },
        )

    def test_legacy_list_form_defaults_label_to_table(self):
        # Maps stored before streamed tables existed have no "label" key.
        raw = [{"board": 1, "table": 1}, {"board": 2, "table": 1}, {"board": 3, "table": 2}]
        self.assertEqual(
            parse_board_table_map(raw),
            {
                1: {"table": 1, "label": "1"},
                2: {"table": 1, "label": "1"},
                3: {"table": 2, "label": "2"},
            },
        )

    def test_dict_form_defaults_label(self):
        self.assertEqual(
            parse_board_table_map({"1": "1", "2": "2"}),
            {1: {"table": 1, "label": "1"}, 2: {"table": 2, "label": "2"}},
        )


class AssignTablesTests(SimpleTestCase):
    def test_empty_map_identity_numbering(self):
        result = assign_tables(["a", "b", "c", "d"], {}, {})
        self.assertEqual(
            result,
            {"a": (1, "1"), "b": (2, "2"), "c": (3, "3"), "d": (4, "4")},
        )

    def test_one_per_table_no_fixed(self):
        m = labelled([(1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4")])
        result = assign_tables(["a", "b", "c", "d"], {}, m)
        self.assertEqual(
            result,
            {"a": (1, "1"), "b": (2, "2"), "c": (3, "3"), "d": (4, "4")},
        )

    def test_two_per_table_no_fixed(self):
        m = labelled([(1, 1, "1"), (2, 1, "1"), (3, 2, "2"), (4, 2, "2")])
        result = assign_tables(["a", "b", "c", "d"], {}, m)
        self.assertEqual(
            result,
            {"a": (1, "1"), "b": (1, "1"), "c": (2, "2"), "d": (2, "2")},
        )

    def test_streamed_separate_numbering(self):
        # 2 streamed (S1, S2), 1 single (1), then a double (2).
        m = labelled([
            (1, 1, "S1"), (2, 2, "S2"), (3, 3, "1"), (4, 4, "2"), (5, 4, "2"),
        ])
        result = assign_tables(["a", "b", "c", "d", "e"], {}, m)
        self.assertEqual(
            result,
            {"a": (1, "S1"), "b": (2, "S2"), "c": (3, "1"), "d": (4, "2"), "e": (4, "2")},
        )

    def test_fixed_by_label_claims_slot(self):
        m = labelled([(1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4")])
        result = assign_tables(["a", "b", "c", "d"], {"b": "3"}, m)
        self.assertEqual(
            result,
            {"a": (1, "1"), "b": (3, "3"), "c": (2, "2"), "d": (4, "4")},
        )

    def test_fixed_to_streamed_label(self):
        m = labelled([(1, 1, "S1"), (2, 2, "S2"), (3, 3, "1"), (4, 4, "2")])
        # Pin "c" to the streamed table S2.
        result = assign_tables(["a", "b", "c", "d"], {"c": "S2"}, m)
        self.assertEqual(
            result,
            {"c": (2, "S2"), "a": (1, "S1"), "b": (3, "1"), "d": (4, "2")},
        )

    def test_fixed_in_two_per_table(self):
        m = labelled([(1, 1, "1"), (2, 1, "1"), (3, 2, "2"), (4, 2, "2")])
        result = assign_tables(["a", "b", "c", "d"], {"b": "1"}, m)
        self.assertEqual(
            result,
            {"a": (1, "1"), "b": (1, "1"), "c": (2, "2"), "d": (2, "2")},
        )

    def test_more_pairings_than_boards_raises(self):
        m = labelled([(1, 1, "1"), (2, 2, "2")])
        with self.assertRaises(ValueError):
            assign_tables(["a", "b", "c"], {}, m)

    def test_fixed_to_nonexistent_label_raises(self):
        m = labelled([(1, 1, "1"), (2, 2, "2")])
        with self.assertRaises(ValueError):
            assign_tables(["a", "b"], {"a": "99"}, m)

    def test_extra_boards_in_map_are_ignored(self):
        m = labelled([(i, i, str(i)) for i in range(1, 7)])
        result = assign_tables(["a", "b", "c"], {}, m)
        self.assertEqual(result, {"a": (1, "1"), "b": (2, "2"), "c": (3, "3")})
