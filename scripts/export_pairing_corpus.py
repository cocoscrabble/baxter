#!/usr/bin/env python
"""Export a parity corpus for the scrabble-pairing Rust crate.

Builds a set of pairing scenarios, runs each through the *Python* pairing engine
(the oracle), and writes ``{input, expected}`` cases in the serialized JSON
boundary shape the Rust crate consumes. Deterministic strategies carry their
expected output for exact comparison; random strategies are marked
``deterministic: false`` and the Rust side checks invariants instead.

Run:  uv run python scripts/export_pairing_corpus.py
Out:  scrabble-pairing/tests/corpus/cases.json
"""

import json
import os
import sys
from pathlib import Path

import django

# Allow running as `python scripts/export_pairing_corpus.py` (the project root,
# which holds the `baxter` settings package, isn't on sys.path otherwise).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "baxter.settings")
django.setup()

from tournaments.pairing.base import (  # noqa: E402
    EntrantData,
    PairingData,
    PlayerData,
    Repeats,
    ResultSlipData,
)
from tournaments.pairing.pair import pair  # noqa: E402
from tournaments.pairing.round_pairing import (  # noqa: E402
    RP,
    RoundPairing,
    normalize_round_robin_start_rounds,
)

OUT = Path(__file__).resolve().parent.parent / "scrabble-pairing" / "tests" / "corpus" / "cases.json"


def entrants(n):
    """n entrants P01..Pn with distinct, descending ratings (stable seeding)."""
    return [EntrantData(PlayerData(f"P{i + 1:02d}", 2000 - 10 * i)) for i in range(n)]


def slip(rnd, winner, loser, ws=450, ls=400, winner_started=True):
    return ResultSlipData(rnd, winner, loser, ws, ls, winner_started)


def sliding_schedule(strategy, n_rounds, start_round_base=0):
    """Rounds 1..n_rounds, each pairing off the previous round."""
    return [
        RoundPairing(r, max(r - 1, start_round_base), strategy)
        for r in range(1, n_rounds + 1)
    ]


def block_schedule(strategy, n_rounds):
    """A single block of one strategy (round-robin/quad style, start at block)."""
    return [RoundPairing(r, 0, strategy) for r in range(1, n_rounds + 1)]


def serialize_input(es, slips, rps, fixed, seed):
    return {
        "players": [
            {"name": e.player.name, "rating": e.player.rating, "dropped": e.dropped}
            for e in es
        ],
        "result_slips": [
            {
                "round": s.round,
                "winner_name": s.winner_name,
                "loser_name": s.loser_name,
                "winner_score": s.winner_score,
                "loser_score": s.loser_score,
                "winner_started": s.winner_started,
            }
            for s in slips
        ],
        "round_pairings": [
            {"round": r.round, "start_round": r.start_round, "pairing": str(r.pairing)} for r in rps
        ],
        "fixed_pairings": {str(k): [[a, b] for (a, b) in v] for k, v in fixed.items()},
        "seed": seed,
    }


def serialize_output(out):
    return [
        {
            "round": round_num,
            "pairings": [
                {"first": p.first.name, "second": p.second.name, "repeats": p.repeats}
                for p in pairings
            ],
        }
        for round_num, pairings in out
    ]


def run_oracle(es, slips, rps, fixed):
    rps_norm = [RoundPairing(r.round, r.start_round, r.pairing) for r in rps]
    normalize_round_robin_start_rounds(rps_norm)
    pd = PairingData(
        result_slips=list(slips),
        entrants=list(es),
        repeats=Repeats(),
        round_pairings=rps_norm,
        fixed_pairings=fixed,
    )
    return pair(pd)


def case(name, deterministic, es, rps, slips=(), fixed=None, seed=0):
    fixed = fixed or {}
    out = run_oracle(es, slips, rps, fixed)
    entry = {
        "name": name,
        "deterministic": deterministic,
        "input": serialize_input(es, slips, rps, fixed, seed),
    }
    if deterministic:
        entry["expected"] = serialize_output(out)
    return entry


def round1_results(es, winners_idx):
    """KotH-style round-1 history: seed pairs (0,1),(2,3),… with chosen winners."""
    s = []
    for i in range(0, len(es) - 1, 2):
        a, b = es[i].player.name, es[i + 1].player.name
        if i // 2 in winners_idx:
            s.append(slip(1, a, b))
        else:
            s.append(slip(1, b, a, winner_started=False))
    return s


def build_cases():
    cases = []

    # --- Deterministic, no result history (pair off seedings / fixed rotation) ---
    cases.append(case("koth_r1_n8", True, entrants(8), block_schedule(RP.KotH, 1)))
    cases.append(case("qoth_r1_n8", True, entrants(8), block_schedule(RP.QotH, 1)))
    cases.append(case("qoth_r1_n10", True, entrants(10), block_schedule(RP.QotH, 1)))
    cases.append(case("swiss_initial_n8", True, entrants(8), block_schedule(RP.Swiss, 1)))
    cases.append(case("round_robin_n6", True, entrants(6), block_schedule(RP.RoundRobin, 5)))
    cases.append(case("round_robin_n8", True, entrants(8), block_schedule(RP.RoundRobin, 7)))
    cases.append(case("double_rr_n4", True, entrants(4), block_schedule(RP.DoubleRoundRobin, 6)))
    cases.append(case("charlottesville_n8", True, entrants(8), block_schedule(RP.Charlottesville, 4)))
    cases.append(case("quads_clustered_n8", True, entrants(8), block_schedule(RP.Quads_Clustered, 3)))
    cases.append(case("quads_distributed_n12", True, entrants(12), block_schedule(RP.Quads_Distributed, 3)))
    cases.append(case("quads_equalized_n12", True, entrants(12), block_schedule(RP.Quads_Equalized, 3)))
    cases.append(case("quads_clustered_hex_n10", True, entrants(10), block_schedule(RP.Quads_Clustered, 3)))
    cases.append(case("sixes_n12", True, entrants(12), block_schedule(RP.Sixes, 3)))

    # --- Deterministic, with a round of results (exercises start_round >= 1) ---
    es8 = entrants(8)
    hist8 = round1_results(es8, winners_idx={0, 1, 2, 3})  # top of each pair wins
    sched_koth = [RoundPairing(1, 0, RP.KotH), RoundPairing(2, 1, RP.KotH)]
    cases.append(case("koth_r2_n8", True, es8, sched_koth, slips=hist8))
    sched_swiss = [RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.Swiss)]
    cases.append(case("swiss_r2_n8", True, es8, sched_swiss, slips=hist8))
    # Tiny field: after round 1 the four players split into two win-groups, and
    # merging the bottom group collapses everything into one sub-6 group. This is
    # the case that hung the Python engine before the merge-loop guard.
    es4 = entrants(4)
    hist4 = round1_results(es4, winners_idx={0, 1})  # top of each pair wins
    cases.append(case("swiss_r2_n4", True, es4, [RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.Swiss)], slips=hist4))

    # --- Late entrant mid-Swiss: P05 joins after round 1 (no results yet) and
    # must appear in round 2 as a zero record (round1_results leaves the odd
    # last seed unpaired). ---
    es5 = entrants(5)
    hist_late = round1_results(es5, winners_idx={0, 1})
    cases.append(case("swiss_late_add_r2_n5", True, es5, [RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.Swiss)], slips=hist_late))

    # --- Dropout mid-Swiss: 8 play round 1, P08 withdraws -> 7 active in round
    # 2, so the even field turns odd and a bye appears. ---
    es8d = entrants(8)
    es8d[7].dropped = True
    hist_drop = round1_results(es8d, winners_idx={0, 1, 2, 3})
    cases.append(case("swiss_dropout_r2_n8", True, es8d, [RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.Swiss)], slips=hist_drop))
    es12 = entrants(12)
    hist12 = round1_results(es12, winners_idx={0, 1, 2, 3, 4, 5})
    cases.append(case("swiss_r2_n12", True, es12, [RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.Swiss)], slips=hist12))

    # --- Odd field -> bye (deterministic: bye to lowest seed with fewest byes) ---
    cases.append(case("koth_bye_n5", True, entrants(5), block_schedule(RP.KotH, 1)))
    cases.append(case("swiss_bye_n7", True, entrants(7), block_schedule(RP.Swiss, 1)))

    # --- Fixed pairings (deterministic) ---
    es6 = entrants(6)
    cases.append(
        case(
            "koth_fixed_n6",
            True,
            es6,
            block_schedule(RP.KotH, 1),
            fixed={1: [("P01", "P06")]},
        )
    )

    # --- Random strategies (non-deterministic; invariant checks on the Rust side) ---
    cases.append(case("random_r1_n8", False, entrants(8), block_schedule(RP.Random, 1)))
    es10 = entrants(10)
    hist10 = round1_results(es10, winners_idx={0, 1, 2, 3, 4})
    cases.append(
        case(
            "random_no_repeats_r2_n10",
            False,
            es10,
            [RoundPairing(1, 0, RP.RandomNoRepeats), RoundPairing(2, 1, RP.RandomNoRepeats)],
            slips=hist10,
        )
    )
    es14 = entrants(14)
    hist14 = round1_results(es14, winners_idx={0, 1, 2, 3, 4, 5, 6})
    cases.append(
        case(
            "swiss_plus_random_r2_n14",
            False,
            es14,
            [RoundPairing(1, 0, RP.SwissPlusRandom), RoundPairing(2, 1, RP.SwissPlusRandom)],
            slips=hist14,
        )
    )

    return cases


def main():
    cases = build_cases()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2) + "\n")
    n_det = sum(1 for c in cases if c["deterministic"])
    print(f"wrote {len(cases)} cases ({n_det} deterministic) to {OUT}")


if __name__ == "__main__":
    main()
