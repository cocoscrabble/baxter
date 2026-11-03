"""Show each tournament's pages in that tournament's time zone."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


class TournamentTimezoneMiddleware:
    """Activate the tournament's zone for any view under a tournament URL.

    Everything is stored in UTC; this only changes how times render (the
    ``date`` filter, ``localtime``). Pages outside a tournament (the admin
    area, the tournament list) stay in UTC.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()

    def process_view(self, request, view_func, view_args, view_kwargs):
        from tournaments.models import Tournament

        slug = view_kwargs.get("tournament_slug")
        name = (
            Tournament.objects.filter(slug=slug)
            .values_list("time_zone", flat=True)
            .first()
            if slug
            else None
        )
        try:
            timezone.activate(ZoneInfo(name)) if name else timezone.deactivate()
        except ZoneInfoNotFoundError, ValueError:
            timezone.deactivate()
        return None
