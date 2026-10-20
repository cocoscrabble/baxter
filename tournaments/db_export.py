"""Export the whole database as a fixture ``manage.py loaddata`` can restore.

The point is reconciliation: the results files in ``../ratings`` are the
ratings' source of truth, and checking them against what Baxter actually
recorded means running Baxter's own code (exporters, standings, replay) over
production's data on a laptop. To do that:

    uv run python manage.py migrate        # a fresh, empty database
    uv run python manage.py loaddata baxter-db-....json.gz

on a checkout at the revision in the file name, since the fixture is shaped by
that revision's models.

Two things differ from ``dumpdata``:

- **Every row, not every visible row.** Rows are read through each model's
  base manager. The default managers hide rows on purpose (``Entrant.objects``
  hides the bye entrant, ``Division.objects`` hides some divisions), and a
  restore missing those would not be the same tournament.
- **No password hashes.** The file is a download, and a hash is a credential
  that can be attacked offline. Users are kept, because tournaments and events
  point at them, but every password is made unusable. Sessions, login
  throttling records and Django's admin history are left out, since none of
  them describe a tournament.
"""

import gzip
import json

from django.apps import apps
from django.contrib.auth.hashers import make_password
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder

# What a restore needs: the accounts tournaments belong to, and everything
# Baxter itself stores. auth.Group is included because users refer to groups
# by name; permissions are recreated by ``migrate``, and the fixture refers to
# them by natural key.
EXPORTED_APPS = ("auth", "users", "tournaments", "editgrid")
EXCLUDED_MODELS = {"auth.permission"}


def exported_models():
    """The models in the fixture, ordered so that a row's references load first."""
    app_list = []
    for label in EXPORTED_APPS:
        config = apps.get_app_config(label)
        models = [
            m for m in config.get_models()
            if m._meta.label_lower not in EXCLUDED_MODELS
        ]
        app_list.append((config, models))
    return serializers.sort_dependencies(app_list, allow_cycles=True)


def database_rows():
    """Every exported row, as the ``python`` serializer's dicts."""
    unusable = make_password(None)
    rows = []
    for model in exported_models():
        queryset = model._base_manager.order_by(model._meta.pk.name)
        batch = serializers.serialize(
            "python", queryset.iterator(), use_natural_foreign_keys=True
        )
        if model._meta.label_lower == "users.user":
            for row in batch:
                row["fields"]["password"] = unusable
        rows.extend(batch)
    return rows


def export_database():
    """``(gzipped JSON fixture, row count)``."""
    rows = database_rows()
    data = json.dumps(rows, cls=DjangoJSONEncoder, indent=1).encode()
    return gzip.compress(data), len(rows)
