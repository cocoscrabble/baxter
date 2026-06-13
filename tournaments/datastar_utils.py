from django.template.loader import render_to_string
from datastar_py.django import DatastarResponse, ServerSentEventGenerator as SSE


def is_datastar(request):
    """Check if request came from Datastar."""
    return "Datastar-Request" in request.headers


def fragment_response(template_name, context, request=None, signals=None, **kwargs):
    """Render template to HTML, return as Datastar SSE patch_elements response.

    Pass ``signals`` to also emit a patch_signals event (e.g. to reset inline-edit
    state after a swap, independent of how the morph re-applies data-signals).
    """
    html = render_to_string(template_name, context, request=request)
    events = [SSE.patch_elements(html, **kwargs)]
    if signals:
        events.append(SSE.patch_signals(signals))
    return DatastarResponse(events)


def redirect_response(url):
    """Return a Datastar SSE redirect response."""
    return DatastarResponse(SSE.redirect(url))
