from django.http import Http404, HttpResponse, JsonResponse
from django.views import View

from .models import EditPresence, EditVersion


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
