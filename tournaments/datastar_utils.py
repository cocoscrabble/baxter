from django.template.loader import render_to_string
from datastar_py.django import DatastarResponse, ServerSentEventGenerator as SSE


def is_datastar(request):
    """Check if request came from Datastar."""
    return "Datastar-Request" in request.headers


def fragment_response(template_name, context, request=None, **kwargs):
    """Render template to HTML, return as Datastar SSE patch_elements response."""
    html = render_to_string(template_name, context, request=request)
    return DatastarResponse(SSE.patch_elements(html, **kwargs))


def redirect_response(url):
    """Return a Datastar SSE redirect response."""
    return DatastarResponse(SSE.redirect(url))
