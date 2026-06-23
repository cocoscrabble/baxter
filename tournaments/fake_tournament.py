"""Generate a fully-populated fake tournament for testing.

Creates a division filled with random players, sets every round to King of the
Hill, and simulates all but the final round so the last round is left pairable.
"""

import random

from django.db import transaction
from django.utils import timezone

from .generate_pairings import publish_rounds, regenerate_pairings
from .match_simulation import simulate_round
from .models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    Tournament,
)
from .pairing.round_pairing import RP, blocks_to_round_pairings


def default_fake_tournament_name():
    """The pre-filled name suggested for a new fake tournament."""
    return f"Test Tournament {timezone.now():%Y-%m-%d %H:%M:%S}"


@transaction.atomic
def create_fake_tournament(user, num_players, num_rounds, name=None):
    """Create a tournament with results simulated through round num_rounds-1.

    Picks ``num_players`` random players for the entrants, sets every round to
    King of the Hill, then pairs/publishes/simulates each round in turn so the
    standings build up realistically. The final round is left unpaired ("able to
    be paired"). Returns the created Division.

    King of the Hill is used rather than Swiss because it always pairs the whole
    field (1v2, 3v4, ...); the Swiss implementation can return a partial round,
    which never finishes and would stall the round-at-a-time loop.

    The caller is responsible for validating that at least ``num_players``
    players exist. Odd fields are fine — the engine adds a bye each round.
    """
    now = timezone.now()
    tournament = Tournament.objects.create(
        name=name or default_fake_tournament_name(),
        location="Test Location",
        start_date=now.date(),
        owner=user,
        is_fake=True,
    )
    # Deliberately not is_test: a test division is hidden from logged-out users,
    # but a fake tournament should be fully visible. is_test is reserved for test
    # divisions inside real tournaments.
    division = Division.objects.create(tournament=tournament, name="Division 1")

    # Exclude provisional players (locally-created, not yet known to the registry)
    # so fake tournaments only draw from the real roster.
    eligible = list(Player.objects.filter(is_provisional=False))
    players = random.sample(eligible, num_players)
    Entrant.objects.bulk_create(
        Entrant(division=division, player=player, number=i)
        for i, player in enumerate(players, start=1)
    )

    blocks = [{"pairing": str(RP.KotH), "rounds": num_rounds, "pair_from": 1}]
    DivisionSettings.objects.create(
        division=division,
        pairing_blocks=blocks,
        round_pairings=[rp.to_dict() for rp in blocks_to_round_pairings(blocks)],
    )

    # Pair, publish, and simulate each round except the last. Each round pairs off
    # the previous round's standings, so a round only becomes pairable once its
    # predecessor is finished — hence the round-at-a-time loop rather than
    # generating the whole schedule up front. Stopping before the final round
    # leaves it pairable but unpaired.
    for round_num in range(1, num_rounds):
        regenerate_pairings(division)
        publish_rounds(division, [round_num])
        simulate_round(division, round_num)

    return division
