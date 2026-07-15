#!/usr/bin/env python
"""Runner script for tournament simulation.

Usage: uv run python scripts/run_simulate.py
"""

import os
import sys

# Ensure the project root is on sys.path when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import Counter

from tournaments.simulate import (
    check_starts_balancing,
    compare_engines,
    simulate,
)
from tournaments.pairing.round_pairing import make_pairings


def test_balanced_starts():
    cases = [(24, "KH:24"), (16, "SW:24"), (12, "RR:11 RR:11")]
    for n_entrants, spec in cases:
        rps = make_pairings(spec)
        rounds, _, _ = simulate(rps, n_entrants)
        check_starts_balancing(rounds)
    print("Starts balancing checks passed.")


# Pre-cutover burn-in: pair many simulated tournaments with both the Python and
# Rust engines and report where they disagree. A clean run (or only equal-cost
# Swiss tie-break differences) is evidence the Rust engine can be cut over.
COMPARE_CASES = [
    ("KH:15", 24), ("KH:15", 23),
    ("QH:15", 20), ("QH:15", 18),
    ("SW:15", 24), ("SW:15", 17),
    ("RR:11", 12), ("RR:11", 11),
    ("DR:10", 8),
    ("QC:3", 16), ("QD:3", 12), ("QE:3", 12), ("SX:3", 12),
]


def compare_all_engines(seeds=range(8)):
    by_kind = Counter()
    n_sims = 0
    for spec, n_entrants in COMPARE_CASES:
        strat = spec.split(":")[0]
        for seed in seeds:
            n_sims += 1
            divs = compare_engines(make_pairings(spec), n_entrants, seed=seed)
            for d in divs:
                by_kind[(strat, d.kind)] += 1
    print(f"Compared {n_sims} simulated tournaments across both engines.")
    if not by_kind:
        print("No divergences — the engines agree everywhere.")
        return
    print("Divergences by (strategy, kind):")
    for (strat, kind), count in sorted(by_kind.items()):
        print(f"  {strat:4} {kind:16} {count}")
    print(
        "\nNote: Swiss orientation/different-pairs are the known blossom-matching "
        "tie-break (equal-cost, semantically arbitrary). different-pairs on other "
        "strategies would be a real divergence to investigate."
    )


if __name__ == "__main__":
    # Run through various simulation-based tests
    test_balanced_starts()
    compare_all_engines()
