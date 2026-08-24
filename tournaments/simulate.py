"""Standalone tournament simulator.

Takes a list of round pairings and number of entrants, runs the pairing
algorithms on simulated data, and returns per-round DisplayPairings and
results. Useful for testing pairing strategies without needing DB-backed data.
"""

import random
from dataclasses import dataclass

from tournaments.pairing.base import (
    DisplayPairing,
    EntrantData,
    Pairing,
    PairingData,
    Player,
    PlayerData,
    Repeats,
    ResultSlipData,
    Starts,
)
from tournaments.pairing.engine import pair_with_engine
from tournaments.pairing.round_pairing import RoundPairing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BYE_SCORE = 50


def create_entrants(n: int) -> list[EntrantData]:
    step = 50 if n <= 16 else 25
    return [
        # A synthetic player's identity *is* its label: this simulator never
        # touches the database, so there is no player number to stand in for.
        EntrantData(PlayerData(f"Player {i + 1}", f"Player {i + 1}", 2300 - i * step))
        for i in range(n)
    ]


def simulate_match(rng, ratings, pairing, round) -> ResultSlipData:
    first, second = pairing.first.key, pairing.second.key

    # Bye handling
    if first.lower() == "bye":
        loser_score = BYE_SCORE
        winner_score = rng.randint(loser_score + 1, 600)
        return ResultSlipData(round, second, first, winner_score, loser_score, False)
    if second.lower() == "bye":
        loser_score = BYE_SCORE
        winner_score = rng.randint(loser_score + 1, 600)
        return ResultSlipData(round, first, second, winner_score, loser_score, True)

    # Normal match — win probability proportional to rating
    r1, r2 = ratings[first], ratings[second]
    first_wins = rng.random() < r1 / (r1 + r2)

    loser_score = rng.randint(200, 450)
    winner_score = rng.randint(loser_score, 600)

    if first_wins:
        winner_started = True  # first is the starter in the pairing
        return ResultSlipData(
            round, first, second, winner_score, loser_score, winner_started
        )
    else:
        winner_started = False
        return ResultSlipData(
            round, second, first, winner_score, loser_score, winner_started
        )


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------


@dataclass
class Round:
    number: int
    pairings: list[DisplayPairing]
    results: list[ResultSlipData]


def _round_pairings(engine_output, round_num):
    for rnd, pairings in engine_output:
        if rnd == round_num:
            return pairings
    return []


def simulate(
    round_pairings: list[dict],
    n_entrants: int,
    seed: int | None = None,
) -> list[Round]:
    """Simulate a tournament.

    Args:
        round_pairings: list of dicts like
            [{"round": 1, "pairing": "KotH", "start_round": 0}, ...]
        n_entrants: number of players
        seed: optional random seed for reproducibility

    Returns a list of ``Round`` (pairings + simulated results). Each round is
    paired by the engine from the accumulated results, exactly as production
    does (whole-tournament ``pair_with_engine`` fed growing result slips).
    """
    rng = random.Random(seed)
    entrants = create_entrants(n_entrants)
    ratings = {e.player.key: e.player.rating for e in entrants}
    rps = [
        d if isinstance(d, RoundPairing) else RoundPairing.from_dict(d)
        for d in round_pairings
    ]
    result_slips: list[ResultSlipData] = []
    rounds = []

    for rp in rps:
        pd = PairingData(
            result_slips=list(result_slips),
            entrants=entrants,
            repeats=Repeats(),
            round_pairings=rps,
            seed=seed or 0,
        )
        pairings = _round_pairings(pair_with_engine(pd), rp.round)
        round_results = []
        for p in pairings:
            slip = simulate_match(rng, ratings, p, rp.round)
            round_results.append(slip)
            result_slips.append(slip)
        rounds.append(Round(rp.round, pairings, round_results))

    return rounds


def check_starts_balancing(rounds: list[Round]) -> None:
    """Verify that starts are balanced: the starter never has more starts than the non-starter.

    Runs simulate(), then replays the results through a fresh Starts tracker,
    asserting that for each game the player who went first has <= starts than
    the player who went second (i.e. the algorithm correctly gave the start
    to the player who needed it).
    """
    starts = Starts()

    for round in rounds:
        for slip in round.results:
            first = slip.first_key
            second = slip.second_key
            assert starts.starts[first] <= starts.starts[second], (
                f"Round {round.number}: {first} has {starts.starts[first]} starts "
                f"but {second} has {starts.starts[second]} starts — "
                f"{first} should not be starting"
            )
            p = Pairing(Player(first), Player(second))
            starts.register(p, round.number)


if __name__ == "__main__":
    raise SystemExit("Run via: uv run python scripts/run_simulate.py")
