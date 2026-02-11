"""Standalone tournament simulator.

Takes a list of round pairings and number of entrants, runs the pairing
algorithms on simulated data, and returns per-round DisplayPairings and
results. Useful for testing pairing strategies without needing DB-backed data.
"""

import csv
import random
from dataclasses import dataclass

from tournaments.pairing.base import (
    DisplayPairing,
    Pairing,
    PairingData,
    Player,
    Repeats,
    RoundPairing,
    Starts,
)
from tournaments.pairing.pair import pair_round


# ---------------------------------------------------------------------------
# Mock data classes (satisfy the interface used by pairing code)
# ---------------------------------------------------------------------------


@dataclass
class SimPlayer:
    name: str
    rating: int


@dataclass
class SimEntrant:
    player: SimPlayer


@dataclass
class SimResultSlip:
    round: int
    winner_name: str
    loser_name: str
    winner_score: int
    loser_score: int
    winner_started: bool

    @property
    def first_name(self) -> str:
        """Name of the player who went first."""
        return self.winner_name if self.winner_started else self.loser_name

    @property
    def second_name(self) -> str:
        """Name of the player who went second."""
        return self.loser_name if self.winner_started else self.winner_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BYE_SCORE = 50


def read_round_pairings_from_csv(path: str) -> list[dict]:
    """Read round pairings from a CSV file.

    Expected format: round,pairing,start_round (no header row).
    """
    with open(path) as f:
        reader = csv.reader(f)
        return [
            {"round": int(row[0]), "pairing": row[1], "start_round": int(row[2])}
            for row in reader
            if row
        ]


def create_entrants(n: int) -> list[SimEntrant]:
    step = 50 if n <= 16 else 25
    return [SimEntrant(SimPlayer(f"Player {i + 1}", 2300 - i * step)) for i in range(n)]


def simulate_match(rng, ratings, pairing, round) -> SimResultSlip:
    first, second = pairing.first.name, pairing.second.name

    # Bye handling
    if first.lower() == "bye":
        loser_score = BYE_SCORE
        winner_score = rng.randint(loser_score + 1, 600)
        return SimResultSlip(round, second, first, winner_score, loser_score, False)
    if second.lower() == "bye":
        loser_score = BYE_SCORE
        winner_score = rng.randint(loser_score + 1, 600)
        return SimResultSlip(round, first, second, winner_score, loser_score, True)

    # Normal match — win probability proportional to rating
    r1, r2 = ratings[first], ratings[second]
    first_wins = rng.random() < r1 / (r1 + r2)

    loser_score = rng.randint(200, 450)
    winner_score = rng.randint(loser_score, 600)

    if first_wins:
        winner_started = True  # first is the starter in the pairing
        return SimResultSlip(
            round, first, second, winner_score, loser_score, winner_started
        )
    else:
        winner_started = False
        return SimResultSlip(
            round, second, first, winner_score, loser_score, winner_started
        )


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------


def simulate(
    round_pairings: list[dict],
    n_entrants: int,
    seed: int | None = None,
) -> tuple[
    list[tuple[int, list[DisplayPairing], list[SimResultSlip]]], Starts, Repeats
]:
    """Simulate a tournament.

    Args:
        round_pairings: list of dicts like
            [{"round": 1, "pairing": "KotH", "start_round": 0}, ...]
        n_entrants: number of players
        seed: optional random seed for reproducibility

    Returns:
        (rounds, starts, repeats) where rounds is a list of
        (round_number, display_pairings, round_results) per round
    """
    rng = random.Random(seed)
    entrants = create_entrants(n_entrants)
    ratings = {e.player.name: e.player.rating for e in entrants}
    result_slips: list[SimResultSlip] = []
    pd = PairingData(result_slips=result_slips, entrants=entrants, repeats=Repeats())
    starts = Starts()
    rounds = []

    for rp_dict in round_pairings:
        rp = RoundPairing.from_dict(rp_dict)
        pairings = pair_round(pd, rp)
        display_pairings = []
        round_results = []
        for p in pairings:
            reps = pd.repeats.add(p)
            result = starts.add(p, rp.round)
            display_pairings.append(DisplayPairing(result.first, result.second, reps))
            slip = simulate_match(rng, ratings, result, rp.round)
            round_results.append(slip)
            result_slips.append(slip)
        rounds.append((rp.round, display_pairings, round_results))

    return rounds, starts, pd.repeats


def check_starts_balancing(
    round_pairings: list[dict],
    n_entrants: int,
    seed: int | None = None,
) -> None:
    """Verify that starts are balanced: the starter never has more starts than the non-starter.

    Runs simulate(), then replays the results through a fresh Starts tracker,
    asserting that for each game the player who went first has <= starts than
    the player who went second (i.e. the algorithm correctly gave the start
    to the player who needed it).
    """
    rounds, _, _ = simulate(round_pairings, n_entrants, seed=seed)
    starts = Starts()

    for round_num, _, round_results in rounds:
        for slip in round_results:
            first = slip.first_name
            second = slip.second_name
            assert starts.starts[first] <= starts.starts[second], (
                f"Round {round_num}: {first} has {starts.starts[first]} starts "
                f"but {second} has {starts.starts[second]} starts — "
                f"{first} should not be starting"
            )
            p = Pairing(Player(first), Player(second))
            starts.register(p, round_num)


if __name__ == "__main__":
    raise SystemExit("Run via: uv run python scripts/run_simulate.py <csv_file>")
