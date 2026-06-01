import json

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views import View

from .concurrency import check_conflict
from .grids import GridContext
from .models import EditPresence, EditVersion


class BaseEditGridView(View):
    """Config-driven GET/POST for one editable grid.

    Set ``grid`` (an :class:`~editgrid.grids.EditGrid`) via ``as_view(grid=...)``.
    Subclasses supply the host-domain bits: ``get_parent()``, ``grid_key(parent)``
    (the opaque editgrid key) and ``presence_url(parent)``.
    """

    grid = None

    def get_parent(self):
        raise NotImplementedError

    def grid_key(self, parent):
        raise NotImplementedError

    def presence_url(self, parent):
        return ""

    def get_grid_context(self, parent):
        key = self.grid_key(parent)
        return GridContext(
            dom_id=self.grid.dom_id,
            rows=self.grid.rows_for(parent),
            lookups=self.grid.lookups(parent),
            version=EditVersion.version_for(key),
            key=key,
            presence_url=self.presence_url(parent),
            js_module=self.grid.js_module,
        )

    def get_context_data(self, parent):
        return {"grid": self.get_grid_context(parent)}

    def get(self, request, *args, **kwargs):
        parent = self.get_parent()
        return render(request, self.grid.template_name, self.get_context_data(parent))

    def post(self, request, *args, **kwargs):
        parent = self.get_parent()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get(self.grid.data_key, [])
        validated, errors = self.grid.validate(rows, parent)
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        # Build (and any extra validation) before the transaction so a failure
        # doesn't bump the version.
        prepared, prep_errors = self.grid.prepare(parent, validated)
        if prep_errors:
            return JsonResponse({"errors": prep_errors}, status=400)

        with check_conflict(self.grid_key(parent), data.get("_version")) as guard:
            if guard.conflict:
                return guard.conflict
            self.grid.persist(parent, prepared)
            self.grid.after_save(parent)
        return guard.response


class EditPresenceBaseView(View):
    """Generic editing-presence endpoint, keyed by an opaque string.

    Subclasses supply ``get_key()`` (returning None for a 404) and any
    permission mixins. A heartbeat POST records the caller as present and
    returns the other editors active on the same key; a ``release`` POST drops
    the caller (sent via ``navigator.sendBeacon`` on tab close, with the CSRF
    token in the form body so the standard CSRF check still applies). If the
    heartbeat carries a ``known_version`` query param, the response also reports
    whether the version has moved on, so the client can warn before a conflict.
    """

    def get_key(self):
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        key = self.get_key()
        if key is None:
            raise Http404("Unknown edit grid.")
        if request.POST.get("release"):
            EditPresence.release(key, request.user)
            return HttpResponse(status=204)
        EditPresence.heartbeat(key, request.user)
        payload = {"editors": EditPresence.others(key, request.user)}
        try:
            known_version = int(request.GET["known_version"])
        except (KeyError, ValueError):
            known_version = None
        if known_version is not None:
            current_version = EditVersion.version_for(key)
            payload["stale"] = current_version != known_version
            payload["current_version"] = current_version
        return JsonResponse(payload)
