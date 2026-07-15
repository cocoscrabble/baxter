from django.apps import AppConfig


class TournamentsConfig(AppConfig):
    name = "tournaments"

    def ready(self):
        # Install the event-log development write guard (dev/tests only).
        from tournaments.events import connect_write_guard

        connect_write_guard()
