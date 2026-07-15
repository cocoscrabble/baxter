#!/usr/bin/env python
"""Runner script for tournament simulation.

Usage: uv run python scripts/run_simulate.py
"""

import os
import sys

# Ensure the project root is on sys.path when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tournaments.simulate import check_starts_balancing, simulate
from tournaments.pairing.round_pairing import make_pairings


def test_balanced_starts():
    cases = [(24, "KH:24"), (16, "SW:24"), (12, "RR:11 RR:11")]
    for n_entrants, spec in cases:
        rps = make_pairings(spec)
        rounds = simulate(rps, n_entrants)
        check_starts_balancing(rounds)
    print("Starts balancing checks passed.")


if __name__ == "__main__":
    test_balanced_starts()
