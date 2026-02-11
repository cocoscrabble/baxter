#!/usr/bin/env python
"""Runner script for tournament simulation.

Usage: uv run python scripts/run_simulate.py <csv_file> [n_entrants]
"""

import os
import sys

# Ensure the project root is on sys.path when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "baxter.settings")

import django

django.setup()

from tournaments.simulate import check_starts_balancing, read_round_pairings_from_csv

rps = read_round_pairings_from_csv(sys.argv[1])
n_entrants = int(sys.argv[2]) if len(sys.argv) > 2 else 24
check_starts_balancing(rps, n_entrants)
print("Starts balancing check passed.")
