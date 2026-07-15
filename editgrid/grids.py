"""Declarative definition of an editable grid: enough config for the generic
view to serve GET (serialize rows) and POST (validate, wipe, recreate), with
hooks for the grid-specific bits. Domain-agnostic — concrete grids live in the
host app and supply the model, validation DTO, serialization, and lookups.
"""

import json
from dataclasses import dataclass, field

# Default client module: the generic bootstrap that builds + wires a grid from
# its column spec. Grids with custom controls override ``js_module`` with a thin
# module that calls ``initGrid`` itself.
GENERIC_JS = "editgrid/js/grid.js"


@dataclass
class Column:
    """Declarative Tabulator column. Drives the grid's columns, the label
    formatting for choice columns, and row serialization (via ``value_type``).

    kind:
      - ``display`` — read-only text
      - ``number``  — integer editor (``min`` floor)
      - ``text``    — free-text string editor (use ``value_type="str"``)
      - ``choice``  — list editor over a value->label map: either ``lookup``
        (a key into the grid's ``lookups``) or a static ``values`` map;
        ``autocomplete`` for large sets.
    """

    field: str
    title: str = ""
    kind: str = "number"
    width: int | None = None
    min_width: int | None = None
    align: str = ""
    min: int | None = None
    lookup: str = ""
    values: dict | None = None
    autocomplete: bool = False
    value_type: str = "int"  # int | bool | str — how the client serializes it
    auto_increment: bool = False  # new rows get max(field) + 1
    new_row: object = None  # default value for this field in a new row
    hidden: bool = False  # kept in row data + serialized, but not shown


def parse_rows(dto_cls, rows, *validate_args):
    """Parse and validate a list of row dicts via a DTO class.

    The DTO must provide a ``from_json(row)`` classmethod returning None for
    missing/invalid input, and a ``validate(*args)`` method returning a list
    of error strings. Returns ``(validated, errors)`` with errors prefixed by
    row number.
    """
    errors = []
    validated = []
    for i, row in enumerate(rows):
        dto = dto_cls.from_json(row)
        if dto is None:
            errors.append(f"Row {i + 1}: all fields are required.")
            continue
        row_errors = dto.validate(*validate_args)
        if row_errors:
            errors.extend(f"Row {i + 1}: {e}" for e in row_errors)
        else:
            validated.append(dto)
    return validated, errors


@dataclass
class GridContext:
    """Everything the template tags need to render one grid instance."""

    dom_id: str
    rows: list
    lookups: dict
    version: int
    key: str
    presence_url: str
    js_module: str
    save_url: str = ""
    columns: list = field(default_factory=list)
    auto_init: bool = False
    focus_field: str = ""

    @property
    def rows_json(self):
        return json.dumps(self.rows)

    @property
    def lookups_json(self):
        return json.dumps(self.lookups)

    @property
    def columns_json(self):
        return json.dumps(self.columns)


class EditGrid:
    """An editable grid over an FK-scoped model collection.

    By default it wipes and recreates the whole collection on save. A grid that
    sets ``key_fields`` instead reconciles the payload against the existing rows
    (match / update / create / delete keyed on that natural key), so untouched
    rows keep their pk, their ``auto_now_add`` timestamps, and any dependent
    rows that would otherwise cascade away.

    Concrete subclasses set the class attributes and override the serialization
    / lookup / validation hooks. The grid-specific bits that don't fit the
    default (extra FK lookups, cross-row checks, post-save side effects) go in
    ``prepare`` and ``after_save``.
    """

    model: type
    parent_field: str = ""     # FK kwarg on ``model`` pointing at the parent
    related_name: str = ""     # reverse accessor for the collection on the parent
    scope: str = ""            # editgrid key scope
    dto_class: type
    data_key: str = "rows"     # JSON key the client wraps its rows in
    dom_id: str = ""
    js_module: str = GENERIC_JS  # generic bootstrap; override for custom controls
    template_name: str = ""
    columns: list = []         # list[Column] driving the client's table
    focus_field: str = ""      # field to focus when a new row is added
    # Host grids that should log a save event set this to the event type; the
    # host view's on_saved hook records it. Empty = don't log.
    event_type: str = ""

    def to_portable(self, rows, parent):
        """Convert the client's (pk-based) rows into a pk-free, replay-safe
        payload for the event log. Default passes them through — override on
        grids whose rows carry pks (entrant/player references)."""
        return rows

    def from_portable(self, rows, parent):
        """Inverse of ``to_portable``: turn a logged (name-based) payload back
        into client-shaped (pk-based) rows so a replay can drive the same save.
        Default passes them through."""
        return rows

    # Reconciling-save configuration. Empty ``key_fields`` keeps the legacy
    # wipe-and-recreate behaviour, so grids that don't opt in are unaffected.
    key_fields: tuple[str, ...] = ()        # model attrs forming row identity
    update_fields: tuple[str, ...] = ()     # model attrs the grid may change
    unique_within_parent: tuple[str, ...] = ()  # unique attrs needing a swap dance

    def queryset(self, parent):
        return getattr(parent, self.related_name).all()

    def rows_for(self, parent):
        return [self.serialize_row(obj) for obj in self.queryset(parent)]

    def serialize_row(self, obj):
        raise NotImplementedError

    def lookups(self, parent):
        """Extra data (e.g. autocomplete options) for the page's pageData."""
        return {}

    def validate_args(self, parent):
        return ()

    def validate(self, rows, parent):
        return parse_rows(self.dto_class, rows, *self.validate_args(parent))

    def can_delete(self, instance):
        """Return an error message if ``instance`` must not be removed, else None.

        Called (pre-transaction) for every existing row whose key is absent from
        the payload. Only consulted for reconciling grids (``key_fields`` set).
        """
        return None

    def _row_key(self, instance):
        return tuple(getattr(instance, f) for f in self.key_fields)

    def reconcile_errors(self, parent, prepared):
        """Pre-transaction checks for reconciling grids: duplicate keys in the
        payload and deletion guards on rows that would be removed. Returns a
        list of error strings (empty when there's nothing to reject).
        """
        if not self.key_fields:
            return []
        errors = []
        seen = set()
        payload_keys = set()
        for inst in prepared:
            key = self._row_key(inst)
            if key in seen:
                errors.append(f"Duplicate row for {self.key_fields} = {key}.")
            seen.add(key)
            payload_keys.add(key)
        for obj in self.queryset(parent):
            if self._row_key(obj) not in payload_keys:
                msg = self.can_delete(obj)
                if msg:
                    errors.append(msg)
        return errors

    def prepare(self, parent, validated):
        """Build the (unsaved) instances to write, or return ``(_, errors)``.

        Runs before the save transaction so failures don't bump the version.
        Default builds one instance per validated row and runs the reconcile
        guards; grids needing extra lookups or cross-row checks override (and
        should call ``reconcile_errors`` themselves).
        """
        fk = {self.parent_field: parent}
        prepared = [self.model(**fk, **dto.to_db_kwargs()) for dto in validated]
        return prepared, self.reconcile_errors(parent, prepared)

    def persist(self, parent, prepared):
        """Write the prepared rows inside the save transaction.

        With ``key_fields`` set, reconcile against the existing rows so untouched
        rows are left alone. Otherwise replace the whole FK-scoped collection
        (delete + bulk-create). Grids backed by something other than a model
        collection override.
        """
        if not self.key_fields:
            self.queryset(parent).delete()
            self.model.objects.bulk_create(prepared)
            return
        self._reconcile(parent, prepared)

    def _reconcile(self, parent, prepared):
        existing = {self._row_key(obj): obj for obj in self.queryset(parent)}
        payload_keys = set()
        to_create, to_update = [], []
        for inst in prepared:
            key = self._row_key(inst)
            payload_keys.add(key)
            match = existing.get(key)
            if match is None:
                to_create.append(inst)
            elif self._copy_changes(inst, match):
                to_update.append(match)
        to_delete = [obj for key, obj in existing.items() if key not in payload_keys]
        # Delete first so a freed unique value (e.g. an entrant number) is
        # available to a create or update in the same save.
        for obj in to_delete:
            obj.delete()
        self._save_updates(to_update)
        if to_create:
            self.model.objects.bulk_create(to_create)

    def _copy_changes(self, src, dst):
        """Copy managed fields from ``src`` onto ``dst``; return True if any
        value actually changed."""
        changed = False
        for f in self.update_fields:
            val = getattr(src, f)
            if getattr(dst, f) != val:
                setattr(dst, f, val)
                changed = True
        return changed

    def _save_updates(self, to_update):
        if not to_update:
            return
        if self.unique_within_parent:
            # Park each row's unique field(s) at a distinct temporary out of the
            # valid range, then restore the real values — so an in-place
            # permutation (two entrants swapping numbers) doesn't transiently
            # violate the non-deferrable unique constraint on SQLite.
            finals = [
                {f: getattr(obj, f) for f in self.unique_within_parent}
                for obj in to_update
            ]
            for i, obj in enumerate(to_update):
                for f in self.unique_within_parent:
                    setattr(obj, f, -(i + 1))
                obj.save(update_fields=self.unique_within_parent)
            for obj, final in zip(to_update, finals):
                for f, val in final.items():
                    setattr(obj, f, val)
        for obj in to_update:
            obj.save(update_fields=self.update_fields)

    def after_save(self, parent):
        """Hook run inside the save transaction after the rows are written."""


class JsonBlobGrid(EditGrid):
    """An editable grid backed by a JSON list field on a settings-style model,
    rather than an FK-scoped model collection.

    Concrete grids set ``blob_model`` / ``blob_fk`` / ``blob_field`` and provide
    their own ``validate`` (no DTO); the validated rows are stored verbatim.
    """

    blob_model: type
    blob_fk: str = ""
    blob_field: str = ""

    def _blob_object(self, parent):
        obj, _ = self.blob_model.objects.get_or_create(**{self.blob_fk: parent})
        return obj

    def rows_for(self, parent):
        return getattr(self._blob_object(parent), self.blob_field) or []

    def prepare(self, parent, validated):
        return validated, []

    def persist(self, parent, prepared):
        obj = self._blob_object(parent)
        setattr(obj, self.blob_field, prepared)
        obj.save(update_fields=[self.blob_field])
