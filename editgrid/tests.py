from dataclasses import dataclass

from django.test import TestCase
from django.utils import timezone

from editgrid.grids import EditGrid, GridContext, parse_rows
from editgrid.models import PRESENCE_WINDOW, EditPresence, EditVersion
from users.models import User

KEY = "thing:1:rows"


@dataclass
class _RowDTO:
    """Minimal DTO for exercising parse_rows."""

    n: int

    @classmethod
    def from_json(cls, row):
        return cls(n=row["n"]) if "n" in row else None

    def validate(self, ceiling):
        return [] if self.n <= ceiling else ["too big"]


class ParseRowsTests(TestCase):
    def test_collects_validated_and_errors_with_row_numbers(self):
        validated, errors = parse_rows(_RowDTO, [{"n": 1}, {}, {"n": 9}], 5)
        self.assertEqual([d.n for d in validated], [1])
        self.assertEqual(errors, ["Row 2: all fields are required.", "Row 3: too big"])


class _KeyedGrid(EditGrid):
    """A grid whose portable rows are identified by ``id``."""

    def portable_key(self, row):
        return row.get("id")


class GridDeltaTests(TestCase):
    grid = _KeyedGrid()

    def test_it_reports_additions_removals_and_changes(self):
        before = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]
        after = [{"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 4, "v": "d"}]
        self.assertEqual(
            self.grid.delta(before, after),
            {
                "added": [{"id": 4, "v": "d"}],
                "removed": [{"id": 3, "v": "c"}],
                "changed": [{"from": {"id": 2, "v": "b"}, "to": {"id": 2, "v": "B"}}],
            },
        )

    def test_an_unchanged_row_is_not_in_the_delta(self):
        rows = [{"id": 1, "v": "a"}]
        self.assertEqual(
            self.grid.delta(rows, rows),
            {"added": [], "removed": [], "changed": []},
        )

    def test_rows_with_no_identity_have_no_delta(self):
        # The base grid keys nothing, which is how a grid opts out.
        self.assertIsNone(EditGrid().delta([{"v": "a"}], [{"v": "b"}]))

    def test_a_duplicate_key_is_treated_as_unkeyable(self):
        dupes = [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}]
        self.assertIsNone(self.grid.delta(dupes, dupes))

    def test_applying_a_delta_rebuilds_the_whole_collection(self):
        before = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]
        after = [{"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 4, "v": "d"}]
        rebuilt = self.grid.apply_delta(before, self.grid.delta(before, after))
        self.assertEqual(sorted(rebuilt, key=lambda r: r["id"]), sorted(after, key=lambda r: r["id"]))

    def test_removing_a_row_that_is_not_there_raises(self):
        delta = {"added": [], "changed": [], "removed": [{"id": 9, "v": "x"}]}
        with self.assertRaisesMessage(ValueError, "not there to remove"):
            self.grid.apply_delta([{"id": 1, "v": "a"}], delta)

    def test_changing_a_row_that_moved_underneath_raises(self):
        delta = {
            "added": [], "removed": [],
            "changed": [{"from": {"id": 1, "v": "a"}, "to": {"id": 1, "v": "z"}}],
        }
        with self.assertRaisesMessage(ValueError, "not what the change was recorded over"):
            self.grid.apply_delta([{"id": 1, "v": "drifted"}], delta)

    def test_adding_a_row_that_is_already_there_raises(self):
        delta = {"added": [{"id": 1, "v": "a"}], "changed": [], "removed": []}
        with self.assertRaisesMessage(ValueError, "already there"):
            self.grid.apply_delta([{"id": 1, "v": "a"}], delta)


class GridContextTests(TestCase):
    def test_json_properties_serialize_rows_and_lookups(self):
        ctx = GridContext(
            dom_id="t", rows=[{"a": 1}], lookups={"x": [2]},
            version=3, key=KEY, presence_url="/p/", js_module="m.js",
        )
        self.assertEqual(ctx.rows_json, '[{"a": 1}]')
        self.assertEqual(ctx.lookups_json, '{"x": [2]}')


class EditVersionTests(TestCase):
    def test_version_for_defaults_to_zero(self):
        self.assertEqual(EditVersion.version_for("absent:0:x"), 0)

    def test_version_for_returns_saved_value(self):
        EditVersion.objects.create(key=KEY, version=5)
        self.assertEqual(EditVersion.version_for(KEY), 5)

    def test_lock_creates_then_returns_same_row(self):
        first = EditVersion.lock(KEY)
        second = EditVersion.lock(KEY)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EditVersion.objects.filter(key=KEY).count(), 1)


class EditPresenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = User.objects.create_user(username="a", password="x")
        cls.b = User.objects.create_user(username="b", password="x")

    def _stale(self, presence):
        old = timezone.now() - PRESENCE_WINDOW - timezone.timedelta(seconds=1)
        EditPresence.objects.filter(pk=presence.pk).update(last_seen=old)

    def test_heartbeat_upserts_one_row_per_user(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.heartbeat(KEY, self.a)
        self.assertEqual(EditPresence.objects.filter(key=KEY, user=self.a).count(), 1)

    def test_others_excludes_self_and_lists_others(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.heartbeat(KEY, self.b)
        self.assertEqual(EditPresence.others(KEY, self.a), ["b"])
        self.assertEqual(EditPresence.others(KEY, self.b), ["a"])

    def test_others_ignores_stale_and_other_keys(self):
        stale = EditPresence.objects.create(key=KEY, user=self.b)
        self._stale(stale)
        EditPresence.heartbeat("thing:1:other", self.b)
        self.assertEqual(EditPresence.others(KEY, self.a), [])

    def test_heartbeat_prunes_stale_rows(self):
        stale = EditPresence.objects.create(key=KEY, user=self.b)
        self._stale(stale)
        EditPresence.heartbeat(KEY, self.a)
        self.assertFalse(EditPresence.objects.filter(pk=stale.pk).exists())

    def test_release_drops_the_users_row(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.release(KEY, self.a)
        self.assertFalse(EditPresence.objects.filter(key=KEY, user=self.a).exists())
