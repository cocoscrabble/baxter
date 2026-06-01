"""Declarative definition of an editable grid: enough config for the generic
view to serve GET (serialize rows) and POST (validate, wipe, recreate), with
hooks for the grid-specific bits. Domain-agnostic — concrete grids live in the
host app and supply the model, validation DTO, serialization, and lookups.
"""

import json
from dataclasses import dataclass


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

    @property
    def rows_json(self):
        return json.dumps(self.rows)

    @property
    def lookups_json(self):
        return json.dumps(self.lookups)


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
    js_module: str = ""        # static path to the per-grid JS module
    template_name: str = ""

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
