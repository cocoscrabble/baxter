"""Static completeness guard: every mutating POST view in the tournaments app
must route through a logged command, or be explicitly exempted.

Walks the URLconf and asserts each tournament view that accepts POST is either
known to be command-backed or on the exempt list. Adding a new mutating view
without wiring it to the event log fails here.
"""

from django.test import SimpleTestCase
from django.urls import get_resolver


def _post_view_classes():
    names = set()

    def walk(resolver):
        for pattern in resolver.url_patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern)
                continue
            view_class = getattr(pattern.callback, "view_class", None)
            if view_class is None:
                continue
            if not view_class.__module__.startswith("tournaments"):
                continue
            if getattr(view_class, "post", None) is not None:
                names.add(view_class.__name__)

    walk(get_resolver())
    return names


# Views whose POST routes through a @records_event command (directly or via the
# grid save hook / the delete command_context).
COMMAND_BACKED = {
    "TournamentCreateView",
    "TournamentUpdateView",
    "TournamentDeleteView",
    "DivisionCreateView",
    "DivisionRenameView",
    "DivisionDeleteView",
    "DivisionRestoreView",
    "DivisionRoundPairingsEditView",
    "DivisionSettingsEditView",
    "DivisionEntrantsEditView",
    "DivisionRegisterView",
    "CreatePlayerView",
    "DivisionEditResultsView",
    "DivisionFixedPairingsEditView",
    "DivisionFixedTablesEditView",
    "DivisionBoardTableMapEditView",
    "BulkImportEntrantsView",
    "WhatIfImportView",
    "ResultSlipCreateView",
    "AddFixedPairingView",
    "RemoveFixedPairingView",
    "RemoveFixedPairingsView",
    "PublishPairingsView",
    "PublishRoundView",
    "UnpublishRoundView",
    "SimulateMatchView",
    "DivisionRefreshRatingsView",
    "SimulateRoundView",
    "PlayoffSetupView",
}

# POST views that don't mutate logged tournament state.
EXEMPT = {
    "DivisionPairingMethodPreviewView",  # method compilation only, no write
    "DivisionRoundPairingsPreviewView",  # preview only, no write
    "EditPresenceView",                  # editing-presence heartbeat
    "PlayerImportView",                  # global registry import (out of scope)
    # Global rating refresh. Unlogged on purpose: entrants pin their rating when
    # they enter, so this mutates no replayable tournament state.
    "WespaImportView",
    # Roster pull. Unlogged for the same reason: entrants freeze their seed at
    # registration, so a pull cannot move a tournament already under way.
    "RosterImportView",
    # Dev tool that builds a whole fake tournament directly; rebuilt on commands
    # in event-log Phase 5.
    "FakeTournamentCreateView",
}


class EventLogCompletenessTests(SimpleTestCase):
    def test_every_post_view_is_command_backed_or_exempt(self):
        discovered = _post_view_classes()
        uncategorized = discovered - COMMAND_BACKED - EXEMPT
        self.assertEqual(
            uncategorized,
            set(),
            f"These POST views are neither command-backed nor exempt — wire them "
            f"to the event log (a @records_event command) or add them to EXEMPT: "
            f"{sorted(uncategorized)}",
        )

    def test_no_stale_entries(self):
        # Keep the lists honest: every named view must still exist and accept POST.
        discovered = _post_view_classes()
        stale = (COMMAND_BACKED | EXEMPT) - discovered
        self.assertEqual(stale, set(), f"stale entries in the guard lists: {sorted(stale)}")
