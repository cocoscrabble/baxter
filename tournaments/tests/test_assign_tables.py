from django.test import SimpleTestCase

from tournaments.assign_tables import assign_tables, parse_board_table_map


class ParseBoardTableMapTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(parse_board_table_map([]), {})
        self.assertEqual(parse_board_table_map(None), {})

    def test_list_form(self):
        raw = [{"board": 1, "table": 1}, {"board": 2, "table": 1}, {"board": 3, "table": 2}]
        self.assertEqual(parse_board_table_map(raw), {1: 1, 2: 1, 3: 2})

    def test_dict_form(self):
        self.assertEqual(parse_board_table_map({"1": "1", "2": "2"}), {1: 1, 2: 2})


class AssignTablesTests(SimpleTestCase):
    def test_empty_map_identity_numbering(self):
        result = assign_tables(["a", "b", "c", "d"], {}, {})
        self.assertEqual(result, {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_one_per_table_no_fixed(self):
        m = {1: 1, 2: 2, 3: 3, 4: 4}
        result = assign_tables(["a", "b", "c", "d"], {}, m)
        self.assertEqual(result, {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_two_per_table_no_fixed(self):
        m = {1: 1, 2: 1, 3: 2, 4: 2}
        result = assign_tables(["a", "b", "c", "d"], {}, m)
        self.assertEqual(result, {"a": 1, "b": 1, "c": 2, "d": 2})

    def test_mixed_first_single_then_double(self):
        # 2 single tables, then 2 doubles
        m = {1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4}
        result = assign_tables(["a", "b", "c", "d", "e", "f"], {}, m)
        self.assertEqual(result, {"a": 1, "b": 2, "c": 3, "d": 3, "e": 4, "f": 4})

    def test_fixed_claims_slot_others_fill_in_order(self):
        m = {1: 1, 2: 2, 3: 3, 4: 4}
        # B is fixed to table 3
        result = assign_tables(["a", "b", "c", "d"], {"b": 3}, m)
        self.assertEqual(result, {"a": 1, "b": 3, "c": 2, "d": 4})

    def test_fixed_in_two_per_table(self):
        m = {1: 1, 2: 1, 3: 2, 4: 2}
        result = assign_tables(["a", "b", "c", "d"], {"b": 1}, m)
        # B takes the first table-1 board; A takes the second; C,D take table 2
        self.assertEqual(result, {"a": 1, "b": 1, "c": 2, "d": 2})

    def test_two_fixed_to_same_double_table(self):
        m = {1: 1, 2: 1, 3: 2, 4: 2}
        result = assign_tables(["a", "b", "c", "d"], {"a": 2, "c": 2}, m)
        # Both fit at table 2's two boards; B,D fill table 1
        self.assertEqual(result, {"a": 2, "b": 1, "c": 2, "d": 1})

    def test_more_pairings_than_boards_raises(self):
        m = {1: 1, 2: 2}
        with self.assertRaises(ValueError):
            assign_tables(["a", "b", "c"], {}, m)

    def test_fixed_to_nonexistent_table_raises(self):
        m = {1: 1, 2: 2}
        with self.assertRaises(ValueError):
            assign_tables(["a", "b"], {"a": 99}, m)

    def test_extra_boards_in_map_are_ignored(self):
        # Map covers 6 boards but only 3 games.
        m = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        result = assign_tables(["a", "b", "c"], {}, m)
        self.assertEqual(result, {"a": 1, "b": 2, "c": 3})
