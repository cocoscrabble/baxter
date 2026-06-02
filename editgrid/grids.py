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
    """A wipe-and-recreate editable grid over an FK-scoped model collection.

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

    def prepare(self, parent, validated):
        """Build the (unsaved) instances to create, or return ``(_, errors)``.

        Runs before the save transaction so failures don't bump the version.
        Default builds one instance per validated row; grids needing extra
        lookups or cross-row checks override.
        """
        fk = {self.parent_field: parent}
        return [self.model(**fk, **dto.to_db_kwargs()) for dto in validated], []

    def persist(self, parent, prepared):
        """Write the prepared rows inside the save transaction.

        Default replaces the whole FK-scoped collection (delete + bulk-create);
        grids backed by something other than a model collection override.
        """
        self.queryset(parent).delete()
        self.model.objects.bulk_create(prepared)

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
