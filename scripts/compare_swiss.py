#!/usr/bin/env python
"""Compare the spreadsheet's Swiss pairings against Baxter's, round by round.

For every round the sheet paired with Swiss or SwissPlusRandom, prints the
standings going into that round, the pairing the sheet's script produced, and the
pairing Baxter's engine produces from the same inputs. Each matchup is annotated
with what the pairing code was weighing when it chose that matchup:

  dist  distance between the players in the standings the round pairs off.
        Usually this also equals the intra-group distance the algorithms weigh
        (the sheet's `distance < swiss_distance` filter and Baxter's
        `max_distance` test both read that) — score groups are normally
        contiguous slices of the standings. The sheet keys its groups on *wins*
        rather than match points, so once anyone has drawn its groups can be
        non-contiguous and the two diverge; Baxter keys on points.
  grp   "same" when both players were in one score group; "promoted" when the
        pairing crossed groups, which only happens when the pairing code pulls
        players up because someone in the top group has no acceptable opponent
        left. Computed against the grouping the round *starts* from, so it flags
        that a promotion happened, not which one.
  reps  how many times the two have already played each other.

Lower `dist` and fewer `reps` are both better, so the per-round totals say which
algorithm made the tighter, less repetitive round.

SwissPlusRandom only pairs the top `spr_split` players by Swiss; the rest are
random and will never agree between two implementations. Those are printed below
a divider and excluded from the totals.

Settings are **inferred per round**. The workbook stores only the configuration a
tournament finished with, and these settings get retuned mid-event, so each
round's (swiss_weight, max_distance, spr_split) is recovered by searching for the
combination that best reproduces the pairing that round actually produced. Where
a round cannot discriminate a parameter, it is reported as "any" rather than
quoted as if it had been measured. Pass --spr-split N to pin the split.

Where the inferred split actually divides the field — at most SPLIT_FRACTION of
it — the sheet is separating
players still in contention from those who are not — which is what COP decides
for itself by simulation instead of from a hand-set number. So on those rounds
**Baxter's side is paired with COP**, since that is what Baxter would really use.
Those rounds are reported but not scored: distance and rematches are Swiss's
objective, and COP pairs contenders against each other regardless of how far
apart they sit.

Inference re-runs the sheet's pairing code across a settings grid, which takes a
few minutes per workbook. These are finished tournaments, so the answer is cached
in `<workbook>.inferred.json` beside the .xlsx and reused on later runs; pass
--refresh-inference to recompute it.

Usage: uv run python scripts/compare_swiss.py ~/tmp/results/tourney.xlsx
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyse_tournament import (  # noqa: E402
    load_tournament,
    to_pairing_data,
    to_swiss_config,
)
from tournaments.pairing.base import PairingData, standings_after_round  # noqa: E402
from tournaments.pairing.engine import pair_with_engine  # noqa: E402

SWISS_CODES = ("S", "ST", "SPR", "STPR")
# The engine's built-in Swiss tuning, used to fill in whatever the caller does
# not override, so every round can be annotated with fully resolved values.
# Source of truth: DEFAULT_SWISS_CONFIG in tournaments/models.py and the
# defaults in scrabble-pairing/src/model.rs.
ENGINE_DEFAULTS = {"swiss_weight": 30, "max_distance": 11, "spr_split": 10}
# Grid searched when inferring what the sheet was tuned to for a round. The
# workbook stores only the settings the tournament *finished* with, so each
# round's settings have to be recovered from the pairings it actually produced.
# The sheet has exactly two knobs, and spends swiss_distance on both the
# candidate-distance filter and the SwissPlusRandom split.
CANDIDATE_WEIGHTS = (10, 20, 30, 40, 60)
CANDIDATE_DISTANCES = tuple(range(4, 25))

SHEET_RUNNER = Path(__file__).resolve().parent.parent / "tools/sheet-pairing/run.js"

# COP's prize/tuning config, used in place of SwissPlusRandom on rounds where the
# sheet split the field (see build_round). Mirrors DEFAULT_COP_CONFIG in
# tournaments/models.py; kept as a literal so this script needs no Django setup.
COP_CONFIG = {
    "place_prizes": 3,
    "gibson_spread": 250,
    "hopefulness": 0.05,
    "control_loss_threshold": 0.25,
    "control_loss_activation_round": 0,
    "simulations": 1000,
    "always_wins_simulations": 1000,
    "disallow_repeat_byes": True,
    # Standings still come from the round's own start_round, so both engines read
    # the same snapshot — but the sheet's ST/STPR rounds pair two rounds back, and
    # counting the horizon from there would tell COP one more round remains than
    # actually does, inflating the contention analysis it pairs on.
    "horizon_from_paired_round": True,
}
# Groups smaller than this are merged into the one above, so the bottom of the
# field is never left with a stub group. Mirrors both implementations.
MIN_BOTTOM_GROUP = 6
# A split only counts as dividing the field once it is at most this fraction of
# it. Above that the "random" remainder is a handful of tail players rather than
# a contender/non-contender split, so the round is still Swiss in substance and
# COP has nothing to add. It also keeps a shaky inference from changing the
# analysis: the widest exactly-fitting distance can land high (R8 of the NOLA
# event infers 20 of 24, while 8 fits too), and a rule stated in terms of the
# field is more robust than trusting that number.
SPLIT_FRACTION = 2 / 3


@dataclass
class Annotated:
    """A matchup plus the quantities the pairing algorithms weigh."""

    name1: str
    name2: str
    dist: int
    same_group: bool
    reps: int

    def key(self) -> frozenset:
        return frozenset((self.name1, self.name2))


def score_groups(standings) -> list[list[str]]:
    """The score groups a Swiss round is paired within.

    Buckets by match points (a draw is half a point), highest first, pulls a
    player up from the next group whenever a group is left odd, then merges the
    bottom group upward until it is big enough. Promotion during pairing can
    still change this, so treat the result as the grouping the round *starts*
    from.

    This matches Baxter. The sheet keys the same buckets on *wins*, which agrees
    whenever nobody has drawn but not otherwise — so on a tournament with drawn
    games this shows Baxter's grouping, not the sheet's.
    """
    by_points: dict[float, list[str]] = {}
    for p in standings:
        by_points.setdefault(p.wins + 0.5 * getattr(p, "ties", 0), []).append(p.name)
    groups = [by_points[k] for k in sorted(by_points, reverse=True)]

    for i in range(len(groups) - 1):
        if len(groups[i]) % 2 and groups[i + 1]:
            groups[i].append(groups[i + 1].pop(0))
    groups = [g for g in groups if g]

    while len(groups) > 1 and len(groups[-1]) < MIN_BOTTOM_GROUP:
        groups[-2].extend(groups.pop())
    return groups


def annotate(
    pairs, positions: dict[str, int], groups: list[list[str]], repeats: dict
) -> list[Annotated]:
    index = {name: (gi, i) for gi, g in enumerate(groups) for i, name in enumerate(g)}
    out = []
    for name1, name2 in pairs:
        # The bye is not a competitor and never appears in the standings, so a bye
        # pairing has no distance or score group to report. Both engines assign it
        # outside the strategy anyway, so there is nothing to compare — drop it
        # rather than inventing a position for it. Same for anyone else absent
        # from the pairable field (a withdrawal, say).
        if name1 not in positions or name2 not in positions:
            continue
        # Order each matchup by standings, so the two listings line up visually.
        if positions.get(name2, 1 << 30) < positions.get(name1, 1 << 30):
            name1, name2 = name2, name1
        g1, g2 = index.get(name1), index.get(name2)
        same_group = bool(g1 and g2 and g1[0] == g2[0])
        out.append(
            Annotated(
                name1=name1,
                name2=name2,
                dist=abs(positions[name1] - positions[name2]),
                same_group=same_group,
                reps=repeats.get(frozenset((name1, name2)), 0),
            )
        )
    out.sort(key=lambda a: positions[a.name1])
    return out


def prior_meetings(pd: PairingData, before_round: int) -> dict:
    counts: dict = {}
    for s in pd.result_slips:
        if s.round < before_round:
            key = frozenset((s.winner_name, s.loser_name))
            counts[key] = counts.get(key, 0) + 1
    return counts


def baxter_round(pd: PairingData, round_no: int) -> list[tuple[str, str]]:
    """Pair `round_no` with Baxter's engine off the results of earlier rounds."""
    pd.result_slips = [s for s in pd.result_slips if s.round < round_no]
    for rno, pairings in pair_with_engine(pd):
        if rno == round_no:
            return [(p.first.name, p.second.name) for p in pairings]
        break
    return []


def fmt(a: Annotated, marker: str, width: int) -> str:
    grp = "same" if a.same_group else "promoted"
    matchup = f"{a.name1:<{width}} v {a.name2:<{width}}"
    return f"    {marker} {matchup}  dist {a.dist:>2}  grp {grp:<8}  reps {a.reps}"


def totals(rows: list[Annotated]) -> str:
    # Report the count too: the two algorithms can leave different numbers of
    # matchups fully inside the Swiss slice, so the sums are only comparable
    # alongside it (hence the mean).
    n = len(rows) or 1
    return (
        f"{len(rows):>2} matchups   sum dist {sum(r.dist for r in rows):>3} "
        f"(mean {sum(r.dist for r in rows) / n:>4.1f})   "
        f"repeat games {sum(1 for r in rows if r.reps):>2}"
    )


_SHEET_INPUTS: dict[Path, Path] = {}


def sheet_inputs(xlsx: Path) -> Path:
    """Export the workbook's ranges to a JS literal file for the sheet runner.

    Written once per workbook into a temp file and reused, since the inference
    re-runs the sheet's pairing code for every round.
    """
    if xlsx not in _SHEET_INPUTS:
        spec = importlib.util.spec_from_file_location(
            "export_inputs", SHEET_RUNNER.parent / "export_inputs.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".input.js", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(module.render(module.export(xlsx), xlsx.name))
        _SHEET_INPUTS[xlsx] = Path(handle.name)
    return _SHEET_INPUTS[xlsx]


def _run_one(inputs: Path, round_no: int, weight: int, distance: int, timeout: float):
    cmd = [
        "node", str(SHEET_RUNNER), str(inputs),
        "--through", str(round_no - 1), "--round", str(round_no),
        "--sweep-weights", str(weight), "--sweep-distances", str(distance),
        # Put the field back to what it was: the Entrants tab records only the
        # final one, so without this the sheet cannot re-derive any round played
        # before a withdrawal.
        "--active-at", str(round_no),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return None
    for line in out.splitlines():
        if line.strip():
            return json.loads(line)
    return None


def sweep_sheet(
    inputs: Path, round_no: int, timeout: float = 5.0
) -> tuple[list[dict], int]:
    """Re-pair `round_no` with the sheet's own code, once per settings pair.

    Each combination runs in its own process. That is not paranoia — two of them
    genuinely fail, and both are defects in Code.ts rather than here:

    - It can spin forever. A narrow distance filter can make a perfect matching
      impossible, and the retry only raises the repeat limit, which never unlocks
      a distance-filtered edge; there is no termination guard.
    - It can crash. `promote2` walks off the end of the group list and `promote`
      dereferences the missing group — it even tests for `undefined`, logs, and
      then dereferences anyway.

    Sweeping in one process meant a single bad combination took the whole grid
    down with it, and since it is the *narrow* distances that misbehave and the
    real settings that turned out to be narrow, that silently discarded the
    answers worth having. Isolated, a failure costs only its own cell.

    Returns the combinations that produced a pairing, and how many were tried.
    """
    combos = [(w, d) for d in CANDIDATE_DISTANCES for w in CANDIDATE_WEIGHTS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_run_one, inputs, round_no, w, d, timeout) for w, d in combos
        ]
        rows = [f.result() for f in futures]
    return [r for r in rows if r], len(combos)


def inference_cache_path(xlsx: Path) -> Path:
    """Where a workbook's inferred settings live: beside the workbook itself."""
    return xlsx.parent / (xlsx.stem + ".inferred.json")


def load_inference_cache(xlsx: Path) -> dict:
    """Previously inferred per-round settings for this workbook, if any.

    Inference re-runs the sheet's pairing code across a large settings grid — a
    few minutes per workbook — and these are finished tournaments whose results
    never change, so the answer is computed once and kept. The cache records the
    grid it was produced from, so a stale file is recognisable rather than
    silently reused under different assumptions.
    """
    path = inference_cache_path(xlsx)
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("source") == xlsx.name:
            return cached
    return {
        "source": xlsx.name,
        "grid": {"weights": list(CANDIDATE_WEIGHTS), "distances": list(CANDIDATE_DISTANCES)},
        "rounds": {},
    }


def save_inference_cache(xlsx: Path, cache: dict) -> None:
    inference_cache_path(xlsx).write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def infer_settings(
    tournament,
    round_no: int,
    inputs: Path | None,
    field: list[str],
    code: str,
    cache: dict | None = None,
) -> tuple[dict, dict, int, int, tuple[int, int]]:
    """Recover the settings the sheet used for this round, from the sheet itself.

    Re-runs the *spreadsheet's* pairing code over the (swiss_weight,
    swiss_distance) grid and keeps the settings whose **Swiss portion** matches
    what the sheet actually produced. Two things make this the right measurement:

    - Scoring with Baxter would confound the settings with the differences
      between the two algorithms, so a wrong setting and a genuine algorithmic
      divergence would be indistinguishable. Run against its own code, the right
      settings reproduce the round exactly.
    - The random portion is unseeded `Math.random()` in the sheet, so it cannot
      be reproduced even in principle. Only pairings inside the candidate's Swiss
      slice are counted, top and bottom of the fraction alike.

    Returns ``(config, consistent, matched, of, coverage)`` where ``consistent``
    maps each parameter to every value that ties for best — more than one means
    the round doesn't discriminate it — and ``coverage`` is how many of the grid's
    combinations actually ran (the rest hung or crashed inside Code.ts).
    """
    if cache is not None and str(round_no) in cache["rounds"]:
        entry = cache["rounds"][str(round_no)]
        return (
            dict(entry["config"]),
            {k: list(v) for k, v in entry["consistent"].items()},
            entry["matched"],
            entry["of"],
            tuple(entry["coverage"]),
        )

    # Only reached on a cache miss, where the caller has exported the workbook.
    assert inputs is not None, "inference needs the sheet inputs on a cache miss"
    actual = {frozenset(p) for p in tournament.generated_pairings.get(round_no, [])}
    scored: list[tuple[float, int, int, int, int, int]] = []
    runs, attempted = sweep_sheet(inputs, round_no)
    for run in runs:
        distance = run["swiss_distance"]
        names = set(field) if code in ("S", "ST") else set(field[:distance])
        swiss = [p for p in run["pairings"] if set(p) <= names]
        if not swiss:
            continue
        hit = sum(1 for p in swiss if frozenset(p) in actual)
        # Rate first: a settings pair is right when its Swiss portion reproduces
        # exactly. Among exact fits prefer the one explaining the most matchups,
        # then the wider slice.
        scored.append((hit / len(swiss), len(swiss), distance, run["swiss_weight"], hit, len(swiss)))
    if not scored:
        return dict(ENGINE_DEFAULTS), {k: [] for k in ENGINE_DEFAULTS}, 0, 0, (0, attempted)

    # Rate first, then the widest slice — that much is evidence. Beyond that the
    # round genuinely cannot tell the settings apart (R11 of the NOLA event fits
    # exactly at distances 8, 9, 11, 13, 15 and 17), so break the tie on the value
    # the workbook actually stores if it is among the exact fits, and otherwise on
    # the narrowest. Picking the largest, as this used to, invents a distinctive
    # number out of an ambiguity — and since the split decides whether the round
    # is paired with COP, that is not a harmless choice. The full set of
    # consistent values is reported either way.
    stored = tournament.settings.swiss.get("swiss_distance")
    best_rate = max(x[0] for x in scored)
    best_slice = max(x[1] for x in scored if x[0] == best_rate)
    winners = [x for x in scored if x[0] == best_rate and x[1] == best_slice]
    distances = sorted({x[2] for x in winners})
    chosen = stored if stored in distances else distances[0]
    best = next(x for x in winners if x[2] == chosen)
    consistent = {
        "swiss_weight": sorted({x[3] for x in winners}),
        "max_distance": distances,
        "spr_split": distances,
    }
    config = {
        "swiss_weight": consistent["swiss_weight"][-1]
        if len(consistent["swiss_weight"]) == 1
        else ENGINE_DEFAULTS["swiss_weight"],
        "max_distance": best[2],
        "spr_split": best[2],
    }
    result = (config, consistent, best[4], best[5], (len(runs), attempted))
    if cache is not None:
        cache["rounds"][str(round_no)] = {
            "config": config,
            "consistent": consistent,
            "matched": best[4],
            "of": best[5],
            "coverage": [len(runs), attempted],
        }
    return result


@dataclass
class RoundReport:
    """Everything the renderers need about one compared round."""

    round_no: int
    code: str
    strategy: str
    start_round: int
    spr_split: int
    config: dict
    consistent: dict
    fit_score: tuple[int, int]
    coverage: tuple[int, int]
    baxter_engine: str
    split_applied: bool
    sheet_settings: dict
    standings: list
    group_index: dict
    swiss_names: set
    fixed: set
    sheet_swiss: list[Annotated]
    sheet_rest: list[Annotated]
    baxter_swiss: list[Annotated]
    baxter_rest: list[Annotated]
    shared: set

    @property
    def identical(self) -> bool:
        return {r.key() for r in self.sheet_swiss} == {r.key() for r in self.baxter_swiss}

    @property
    def agree(self) -> int:
        return len(self.shared)

    @property
    def is_swiss_only(self) -> bool:
        return self.code in ("S", "ST")

    @property
    def is_cop(self) -> bool:
        return self.baxter_engine.startswith("COP")

    def only(self, rows) -> list[Annotated]:
        return [r for r in rows if r.key() not in self.shared]

    @property
    def verdict(self) -> str:
        """Which engine made the better round, on rematches then tightness.

        Not scored for COP rounds: distance and rematches are *Swiss's* objective.
        COP deliberately pairs contenders against each other regardless of how far
        apart they sit, so grading it on tightness would mark correct behaviour as
        a regression.
        """
        if self.is_cop:
            return "cop, not scored"
        if self.identical:
            return "identical"
        _, sm, sr = stats(self.sheet_swiss)
        _, bm, br = stats(self.baxter_swiss)
        if (br, round(bm, 1)) < (sr, round(sm, 1)):
            return "baxter better"
        if (br, round(bm, 1)) > (sr, round(sm, 1)):
            return "sheet better"
        return "differ, comparable"


def stats(rows) -> tuple[int, float, int]:
    """(matchups, mean standings distance, number of rematches)."""
    n = len(rows) or 1
    return len(rows), sum(r.dist for r in rows) / n, sum(1 for r in rows if r.reps)


def settings_lines(rep: RoundReport) -> list[str]:
    """How this round was configured, as three comparable lines.

    Worth stating per round rather than once at the top: the workbook records
    only the settings the tournament *finished* with, so what was in force for a
    given round has to be inferred from the pairings that round produced.
    Parameters the round cannot discriminate are marked "any", so an inferred
    value is never quoted as if it were measured when it wasn't.
    """

    def show(name: str) -> str:
        values = rep.consistent[name]
        if len(values) == 1:
            return f"{name} {values[0]}"
        if len(values) >= len(_candidates(name)):
            return f"{name} any"
        return f"{name} {values[0]}-{values[-1]}"

    hit, total = rep.fit_score
    ran, tried = rep.coverage
    gaps = "" if ran == tried else f", {tried - ran} of {tried} settings hung/crashed"
    inferred = (
        f"{show('swiss_weight')}, {show('max_distance')}, {show('spr_split')}"
        f"   [sheet's own code reproduces {hit}/{total} of its Swiss portion{gaps}]"
    )
    weight = rep.sheet_settings.get("swiss_weight", "unset")
    distance = rep.sheet_settings.get("swiss_distance", "unset")
    stored = (
        f"swiss_weight {weight}, swiss_distance {distance} "
        "(drives both max_distance and the split)"
    )
    if rep.is_cop:
        used = (
            f"COP (place_prizes {COP_CONFIG['place_prizes']}, "
            f"hopefulness {COP_CONFIG['hopefulness']}) — the sheet split the field "
            f"at {rep.spr_split}, so COP replaces Swiss+random here. Same "
            f"standings (after round {rep.start_round}); horizon counted from "
            f"round {rep.round_no}"
        )
    else:
        used = (
            f"{rep.strategy} — swiss_weight {rep.config['swiss_weight']}, "
            f"max_distance {rep.config['max_distance']}, spr_split {rep.spr_split}"
            + (
                ""
                if rep.split_applied
                else " (this code pairs the whole field; no split)"
                if rep.is_swiss_only
                else f" (split {rep.spr_split} of {len(rep.standings)} is over "
                f"{SPLIT_FRACTION:.0%} of the field — treated as no split)"
            )
        )
    return [inferred, stored, used]


def _candidates(name: str) -> tuple:
    return {
        "swiss_weight": CANDIDATE_WEIGHTS,
        "max_distance": CANDIDATE_DISTANCES,
        "spr_split": CANDIDATE_DISTANCES,
    }[name]


def last_played(tournament) -> dict[str, int]:
    """The last round each player has a result in."""
    out: dict[str, int] = {}
    for r in tournament.results:
        for name in (r.winner, r.opponent):
            out[name] = max(out.get(name, 0), r.round)
    return out


def build_round(
    tournament, round_no: int, spr_split: int, swiss_config, cache: dict | None = None
) -> RoundReport:
    code = tournament.settings.round_codes[round_no]
    pd = to_pairing_data(tournament)
    # Mark anyone who had withdrawn by this round. The workbook has no field for
    # it — a director's only mechanism is deleting the Entrants row — so it is
    # recovered from the results: no game in this round or later means gone.
    # Baxter keeps a dropped entrant's played results (they still count for their
    # opponents) but never pairs them again, which is exactly the semantics.
    played = last_played(tournament)
    for entrant in pd.entrants:
        entrant.dropped = played.get(entrant.player.name, 0) < round_no
    rp = {x.round: x for x in pd.round_pairings}[round_no]
    standings = standings_after_round(pd, rp.start_round)
    positions = {p.name: i for i, p in enumerate(standings)}
    groups = score_groups(standings)
    repeats = prior_meetings(pd, round_no)

    # Fixed pairings are pulled out before the field is split, by both engines.
    fixed = {n for pair in pd.fixed_pairings.get(round_no, []) for n in pair}
    field = [p.name for p in standings if p.name not in fixed]

    cached = cache is not None and str(round_no) in cache["rounds"]
    inputs = None if cached else sheet_inputs(tournament.path)
    config, consistent, hit, total, coverage = infer_settings(
        tournament, round_no, inputs, field, code, cache
    )
    if swiss_config:  # explicit --sheet-settings overrides the inferred weights
        config = {**config, **swiss_config}
    if spr_split:  # explicit --spr-split pins the split
        config = {**config, "spr_split": spr_split}
    spr_split = config["spr_split"]
    pd.swiss_config = config
    swiss_names = set(field) if code in ("S", "ST") else set(field[:spr_split])

    # A split only "applies" when it divides the field meaningfully — at or below
    # SPLIT_FRACTION of it. At the field size it degenerates to plain Swiss over
    # everyone, and just short of it the remainder is a tail, not a field split.
    split_applied = code not in ("S", "ST") and spr_split <= SPLIT_FRACTION * len(field)

    # How many rounds back the standings come from: 1 for the sheet's S/SPR, 2 for
    # its ST/STPR. Worth carrying in the engine label because the strategy name
    # alone doesn't distinguish them, and it is the difference between pairing off
    # the latest table and pairing off a two-round-old one.
    lag = round_no - rp.start_round

    # Where the sheet split the field, it is separating players still in
    # contention from those who are not — which is exactly what COP decides for
    # itself, by simulation, instead of from a hand-set number. So COP is what
    # Baxter would actually use for such a round, and it is what we compare.
    baxter_pd = to_pairing_data(tournament)
    baxter_pd.swiss_config = config
    if split_applied:
        baxter_engine = f"COP-{lag}"
        baxter_pd.cop_config = COP_CONFIG
        for x in baxter_pd.round_pairings:
            if x.round == round_no:
                # Keep the round's own start_round so both engines read the same
                # standings; only the strategy changes.
                x.pairing = "COP"
    else:
        baxter_engine = f"{rp.pairing}-{lag}"

    sheet = annotate(
        tournament.generated_pairings.get(round_no, []), positions, groups, repeats
    )
    baxter = annotate(baxter_round(baxter_pd, round_no), positions, groups, repeats)

    def split(rows):
        swiss = [r for r in rows if {r.name1, r.name2} <= swiss_names]
        return swiss, [r for r in rows if r not in swiss]

    sheet_swiss, sheet_rest = split(sheet)
    baxter_swiss, baxter_rest = split(baxter)
    return RoundReport(
        round_no=round_no,
        code=code,
        strategy=str(rp.pairing),
        start_round=rp.start_round,
        spr_split=spr_split,
        config=config,
        consistent=consistent,
        fit_score=(hit, total),
        coverage=coverage,
        baxter_engine=baxter_engine,
        split_applied=split_applied,
        sheet_settings=dict(tournament.settings.swiss),
        standings=standings,
        group_index={n: (gi, i) for gi, g in enumerate(groups) for i, n in enumerate(g)},
        swiss_names=swiss_names,
        fixed=fixed,
        sheet_swiss=sheet_swiss,
        sheet_rest=sheet_rest,
        baxter_swiss=baxter_swiss,
        baxter_rest=baxter_rest,
        shared={r.key() for r in sheet_swiss} & {r.key() for r in baxter_swiss},
    )


# ---------------------------------------------------------------------------
# Plain-text rendering
# ---------------------------------------------------------------------------


def rep_rest_label(rep: RoundReport) -> str:
    """Label for the pairings outside the compared slice.

    For the sheet these are random and so not comparable. COP has no such
    notion — it pairs the whole field deliberately — so say that rather than
    calling its work a "random portion".
    """
    if rep.is_cop:
        return "baxter: rest of field (COP pairs everyone; not compared — the sheet's are random)"
    return "baxter: rest of field / fixed pairings (not compared)"


def render_round_text(out, rep: RoundReport) -> None:
    width = max((len(p.name) for p in rep.standings), default=20)
    print(f"\n{'=' * 100}", file=out)
    print(
        f"ROUND {rep.round_no}   sheet code {rep.code}   {rep.strategy}   "
        f"paired off standings after round {rep.start_round}",
        file=out,
    )
    print("=" * 100, file=out)
    inferred, stored, used = settings_lines(rep)
    print(f"\n  inferred for this round : {inferred}", file=out)
    print(f"  stored in the workbook  : {stored}", file=out)
    print(f"  baxter paired this with : {used}", file=out)

    print(f"\n  Standings after round {rep.start_round}"
          f"   (grp = win group / index within it)", file=out)
    for i, p in enumerate(rep.standings):
        gi, gj = rep.group_index.get(p.name, ("?", "?"))
        mark = " " if p.name in rep.swiss_names else "."
        note = "  [fixed]" if p.name in rep.fixed else ""
        print(
            f"   {mark}{i + 1:>3}  {p.name:<{width}}  {p.record:>9}  {p.spread:>+6}"
            f"   grp {gi}/{gj}{note}",
            file=out,
        )
    if not rep.is_swiss_only:
        print(f"\n   ' ' = Swiss-paired (top {rep.spr_split} of the unfixed field)"
              f",  '.' = random portion", file=out)

    def print_rest(label, rows):
        if not rows:
            return
        print(f"\n    {label}", file=out)
        for r in rows:
            print(fmt(r, " ", width), file=out)

    if rep.identical and rep.sheet_swiss:
        # Nothing to compare: list the agreed matchups once rather than twice.
        print("\n  PAIRINGS WERE IDENTICAL — sheet and Baxter produced the same "
              f"{len(rep.sheet_swiss)} Swiss matchups", file=out)
        for r in rep.sheet_swiss:
            print(fmt(r, "=", width), file=out)
        print(f"    {'-' * 46} swiss slice: {totals(rep.sheet_swiss)}", file=out)
        print_rest("sheet: rest of field / fixed pairings (not compared)", rep.sheet_rest)
        print_rest(rep_rest_label(rep), rep.baxter_rest)
    else:
        print(f"\n  PAIRINGS DIFFERED — {rep.agree} of {len(rep.sheet_swiss)} sheet "
              "matchups reproduced", file=out)
        for label, swiss_rows, rest_rows in (
            ("SHEET  (Code.ts)", rep.sheet_swiss, rep.sheet_rest),
            (f"BAXTER ({rep.baxter_engine})", rep.baxter_swiss, rep.baxter_rest),
        ):
            print(f"\n  {label}", file=out)
            if not swiss_rows and not rest_rows:
                print("    (no pairings recorded)", file=out)
                continue
            for r in swiss_rows:
                print(fmt(r, "=" if r.key() in rep.shared else "*", width), file=out)
            print(f"    {'-' * 46} swiss slice: {totals(swiss_rows)}", file=out)
            print_rest("random portion / fixed pairings (not compared)", rest_rows)

        # The differing matchups on their own, so the trade-off each algorithm
        # made is readable without diffing the two lists by eye.
        sheet_only, baxter_only = rep.only(rep.sheet_swiss), rep.only(rep.baxter_swiss)
        if sheet_only or baxter_only:
            print("\n  DIFFERENCES (matchups unique to each)", file=out)
            for i, (tag, rows) in enumerate((("sheet", sheet_only), ("baxter", baxter_only))):
                if i:
                    print(file=out)
                for r in rows:
                    print(f"    {tag:<7}{fmt(r, '', width)[5:]}", file=out)

    _, sm, sr = stats(rep.sheet_swiss)
    _, bm, br = stats(rep.baxter_swiss)
    print(
        f"\n  >> Swiss slice: {rep.agree}/{len(rep.sheet_swiss)} matchups agree.   "
        f"sheet: mean dist {sm:.1f}, {sr} repeat games   |   "
        f"baxter: mean dist {bm:.1f}, {br} repeat games",
        file=out,
    )


def render_text(out, reports: list[RoundReport], meta: dict) -> None:
    print(f"Swiss comparison for {meta['file']}", file=out)
    print(f"  sheet settings : {meta['sheet_settings']}", file=out)
    print(f"  baxter config  : {meta['baxter_config']}, spr_split {meta['split']}", file=out)
    print(f"  rounds compared: {meta['rounds']}", file=out)
    for warning in meta.get("warnings", []):
        print(f"\n  !! {warning}", file=out)

    for rep in reports:
        render_round_text(out, rep)

    print(f"\n\n{'=' * 100}\nSUMMARY (Swiss slice only; random portion excluded)"
          f"\n{'=' * 100}", file=out)
    print(
        "  NOTE: settings are inferred per round from the pairings the sheet\n"
        "        produced, because the workbook stores only the final config.\n"
        "        Rounds where the sheet split the field are paired with COP on\n"
        "        the Baxter side and are reported but not scored.",
        file=out,
    )
    print(
        "  round  code/split  baxter engine       agree"
        "     SHEET n/mean-dist/reps     BAXTER n/mean-dist/reps",
        file=out,
    )
    for rep in reports:
        sn, sm, sr = stats(rep.sheet_swiss)
        bn, bm, br = stats(rep.baxter_swiss)
        print(
            f"  R{rep.round_no:<4}  {rep.code + '/' + str(rep.spr_split):<10}  "
            f"{rep.baxter_engine:<18}  {rep.agree:>2}/{len(rep.sheet_swiss):<3}  "
            f"{sn:>8} / {sm:>4.1f} / {sr:<3}  "
            f"{bn:>12} / {bm:>4.1f} / {br:<3}  {rep.verdict}",
            file=out,
        )
    swiss = [r for r in reports if not r.is_cop]
    cop = [r for r in reports if r.is_cop]
    sn, sm, sr = stats([r for rep in swiss for r in rep.sheet_swiss])
    bn, bm, br = stats([r for rep in swiss for r in rep.baxter_swiss])
    print(
        f"\n  Swiss-vs-Swiss rounds ({len(swiss)}): "
        f"{sum(r.agree for r in swiss)}/{sum(len(r.sheet_swiss) for r in swiss)}"
        " matchups agree\n"
        f"    sheet : {sn} matchups, mean dist {sm:.2f}, {sr} repeat games\n"
        f"    baxter: {bn} matchups, mean dist {bm:.2f}, {br} repeat games",
        file=out,
    )
    if cop:
        cn, cm, cr = stats([r for rep in cop for r in rep.baxter_swiss])
        xn, xm, xr = stats([r for rep in cop for r in rep.sheet_swiss])
        print(
            f"\n  COP rounds ({len(cop)}): the sheet split the field, so Baxter used\n"
            "  COP instead of Swiss+random. Reported, not scored — COP optimises\n"
            "  prize equity, not tightness, so a higher mean distance here is the\n"
            "  algorithm working, not a regression.\n"
            f"    sheet : {xn} matchups, mean dist {xm:.2f}, {xr} repeat games\n"
            f"    baxter: {cn} matchups, mean dist {cm:.2f}, {cr} repeat games",
            file=out,
        )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
/* The report commits to a light look rather than following the viewer's system
   theme. It is a dense reference document — read next to terminal output,
   screenshotted and printed — and the tinted diff rows and status colours were
   picked against a white background. color-scheme keeps scrollbars and any form
   controls light to match. */
:root {
  color-scheme: light;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6a6a6a; --line: #e2e2e2;
  --panel: #f7f7f8; --accent: #2563eb;
  --ok: #15803d; --ok-bg: #dcfce7; --warn: #b45309; --warn-bg: #fef3c7;
  --bad: #b91c1c; --bad-bg: #fee2e2; --info: #1d4ed8; --info-bg: #dbeafe;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 6rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .35rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .6rem; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.meta { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: .8rem 1rem; margin-bottom: 1.5rem; font-size: .85rem; }
.meta div { margin: .15rem 0; }
.meta b { font-weight: 600; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1.5rem; }
.card { flex: 1 1 200px; background: var(--panel); border: 1px solid var(--line);
        border-radius: 8px; padding: .75rem .9rem; }
.card .k { color: var(--muted); font-size: .75rem; text-transform: uppercase;
           letter-spacing: .04em; }
.card .v { font-size: 1.35rem; font-weight: 600; margin-top: .15rem; }
.card .n { color: var(--muted); font-size: .78rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .3rem .55rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .72rem;
     text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.diff td { background: color-mix(in srgb, var(--warn-bg) 55%, transparent); }
.badge { display: inline-block; padding: .1rem .45rem; border-radius: 999px;
         font-size: .72rem; font-weight: 600; white-space: nowrap; }
.b-identical { background: var(--ok-bg); color: var(--ok); }
.b-cop { background: var(--panel); color: var(--muted); border: 1px solid var(--line); }
.b-baxter { background: var(--info-bg); color: var(--info); }
.b-sheet { background: var(--bad-bg); color: var(--bad); }
.b-comparable { background: var(--warn-bg); color: var(--warn); }
.tag { font-size: .72rem; color: var(--muted); }
.rep { color: var(--bad); font-weight: 600; }
.promoted { color: var(--warn); }
details.round { border: 1px solid var(--line); border-radius: 8px; margin: .5rem 0;
                background: var(--bg); }
details.round > summary { cursor: pointer; padding: .6rem .9rem; display: flex;
  flex-wrap: wrap; gap: .6rem; align-items: center; font-weight: 600; }
details.round > summary::-webkit-details-marker { display: none; }
details.round > summary::before { content: "▸"; color: var(--muted); }
details.round[open] > summary::before { content: "▾"; }
details.round[open] > summary { border-bottom: 1px solid var(--line); }
.body { padding: .9rem; }
table.cfg { width: auto; margin: 0 0 .8rem; font-size: .78rem; }
table.cfg th { text-transform: none; letter-spacing: 0; padding-right: .8rem;
               vertical-align: top; }
table.cfg th, table.cfg td { border-bottom: none; padding-top: .1rem;
                             padding-bottom: .1rem; }
table.cfg td { color: var(--muted); }
details.sub-d { margin: .6rem 0; }
details.sub-d > summary { cursor: pointer; color: var(--accent); font-size: .82rem; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
@media (max-width: 860px) { .cols { grid-template-columns: 1fr; } }
.col h3 { font-size: .8rem; margin: 0 0 .35rem; text-transform: uppercase;
          letter-spacing: .04em; color: var(--muted); }
.tot { font-size: .78rem; color: var(--muted); margin-top: .35rem; }
.controls { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0; }
button { font: inherit; font-size: .82rem; padding: .35rem .7rem; cursor: pointer;
         background: var(--panel); color: var(--fg); border: 1px solid var(--line);
         border-radius: 6px; }
button:hover { border-color: var(--accent); }
.warn-box { background: var(--warn-bg); color: var(--warn); border-radius: 8px;
            padding: .7rem .9rem; margin: 0 0 1.5rem; font-size: .85rem; }
.legend { font-size: .8rem; color: var(--muted); margin-top: 2.5rem;
          border-top: 1px solid var(--line); padding-top: 1rem; }
.legend dt { font-weight: 600; color: var(--fg); margin-top: .5rem; }
.legend dd { margin: 0 0 .1rem; }
summary.hidden, details.round.hidden { display: none; }
a.rlink { color: var(--accent); text-decoration: none; }
a.rlink:hover { text-decoration: underline; }
"""

JS = """
function setAll(open) {
  document.querySelectorAll('details.round').forEach(d => { d.open = open; });
}
function onlyDiffs(on) {
  document.querySelectorAll('details.round').forEach(d => {
    d.classList.toggle('hidden', on && d.dataset.verdict === 'identical');
  });
}
document.getElementById('expand').onclick = () => setAll(true);
document.getElementById('collapse').onclick = () => setAll(false);
const filter = document.getElementById('filter');
filter.onclick = () => {
  const on = filter.dataset.on !== '1';
  filter.dataset.on = on ? '1' : '0';
  filter.textContent = on ? 'Show all rounds' : 'Hide identical rounds';
  onlyDiffs(on);
};
"""

BADGE = {
    "cop, not scored": "b-cop",
    "identical": "b-identical",
    "baxter better": "b-baxter",
    "sheet better": "b-sheet",
    "differ, comparable": "b-comparable",
}


def esc(text) -> str:
    return html.escape(str(text))


def pair_rows(rows, shared=None, mark_diff=True) -> str:
    """A matchup table body. Differing rows are tinted when `shared` is given."""
    out = []
    for r in rows:
        differs = mark_diff and shared is not None and r.key() not in shared
        grp = (
            '<span class="promoted">promoted</span>'
            if not r.same_group
            else '<span class="tag">same</span>'
        )
        reps = f'<span class="rep">{r.reps}</span>' if r.reps else "0"
        out.append(
            f'<tr class="{"diff" if differs else ""}">'
            f"<td>{esc(r.name1)}</td><td>{esc(r.name2)}</td>"
            f'<td class="num">{r.dist}</td><td>{grp}</td><td class="num">{reps}</td></tr>'
        )
    return "".join(out)


def pair_table(rows, shared=None, mark_diff=True) -> str:
    if not rows:
        return '<p class="tag">(none)</p>'
    return (
        '<div class="scroll"><table><thead><tr><th>Player</th><th>Opponent</th>'
        '<th class="num">Dist</th><th>Grp</th><th class="num">Reps</th></tr></thead>'
        f"<tbody>{pair_rows(rows, shared, mark_diff)}</tbody></table></div>"
    )


def totals_line(rows) -> str:
    n, mean, reps = stats(rows)
    return (
        f'<div class="tot">{n} matchups &middot; mean dist {mean:.1f} &middot; '
        f"{reps} repeat game{'' if reps == 1 else 's'}</div>"
    )


def render_round_html(rep: RoundReport) -> str:
    inferred, stored, used = settings_lines(rep)
    settings = (
        '<table class="cfg"><tbody>'
        f'<tr><th>Inferred</th><td class="mono">{esc(inferred)}</td></tr>'
        f'<tr><th>Stored</th><td class="mono">{esc(stored)}</td></tr>'
        f'<tr><th>Baxter used</th><td class="mono">{esc(used)}</td></tr>'
        "</tbody></table>"
    )

    standings_rows = []
    for i, p in enumerate(rep.standings):
        gi, gj = rep.group_index.get(p.name, ("?", "?"))
        tags = []
        if p.name in rep.fixed:
            tags.append('<span class="tag">fixed</span>')
        elif p.name not in rep.swiss_names:
            tags.append('<span class="tag">random</span>')
        standings_rows.append(
            f'<tr><td class="num">{i + 1}</td><td>{esc(p.name)}</td>'
            f'<td class="num">{esc(p.record)}</td><td class="num">{p.spread:+d}</td>'
            f'<td class="num">{gi}/{gj}</td><td>{"".join(tags)}</td></tr>'
        )
    standings = (
        '<details class="sub-d"><summary>Standings going into the round '
        f"({len(rep.standings)} players)</summary>"
        '<div class="scroll"><table><thead><tr><th class="num">#</th><th>Player</th>'
        '<th class="num">Record</th><th class="num">Spread</th>'
        '<th class="num">Grp</th><th></th></tr></thead>'
        f"<tbody>{''.join(standings_rows)}</tbody></table></div></details>"
    )

    if rep.identical and rep.sheet_swiss:
        body = (
            f'<p><b>Pairings were identical</b> — sheet and Baxter produced the same '
            f"{len(rep.sheet_swiss)} Swiss matchups.</p>"
            + pair_table(rep.sheet_swiss, rep.shared)
            + totals_line(rep.sheet_swiss)
        )
        for label, rest in (("Sheet", rep.sheet_rest), ("Baxter", rep.baxter_rest)):
            if rest:
                body += (
                    f'<details class="sub-d"><summary>{label} random portion / fixed '
                    f"pairings ({len(rest)}, not compared)</summary>"
                    + pair_table(rest, None, False)
                    + "</details>"
                )
    else:
        cols = ""
        for label, swiss_rows, rest in (
            ("Sheet (Code.ts)", rep.sheet_swiss, rep.sheet_rest),
            (f"Baxter ({rep.baxter_engine})", rep.baxter_swiss, rep.baxter_rest),
        ):
            rest_html = ""
            if rest:
                rest_html = (
                    f'<details class="sub-d"><summary>random portion / fixed pairings '
                    f"({len(rest)}, not compared)</summary>"
                    + pair_table(rest, None, False)
                    + "</details>"
                )
            cols += (
                f'<div class="col"><h3>{label}</h3>'
                + pair_table(swiss_rows, rep.shared)
                + totals_line(swiss_rows)
                + rest_html
                + "</div>"
            )
        sheet_only, baxter_only = rep.only(rep.sheet_swiss), rep.only(rep.baxter_swiss)
        diffs = ""
        if sheet_only or baxter_only:
            diffs = (
                '<h3 style="font-size:.8rem;margin:1.2rem 0 .35rem;color:var(--muted);'
                'text-transform:uppercase;letter-spacing:.04em">Differences '
                "(matchups unique to each)</h3>"
                '<div class="cols">'
                f'<div class="col"><h3>Sheet only</h3>{pair_table(sheet_only, set())}</div>'
                f'<div class="col"><h3>Baxter only</h3>{pair_table(baxter_only, set())}</div>'
                "</div>"
            )
        body = (
            f"<p><b>Pairings differed</b> — {rep.agree} of {len(rep.sheet_swiss)} "
            "sheet matchups reproduced.</p>"
            f'<div class="cols">{cols}</div>{diffs}'
        )

    badge = BADGE[rep.verdict]
    return (
        f'<details class="round" id="r{rep.round_no}" data-verdict="{esc(rep.verdict)}"'
        f'{"" if rep.identical else " open"}>'
        f"<summary>Round {rep.round_no}"
        f'<span class="tag">{esc(rep.code)} &middot; {esc(rep.strategy)} &middot; '
        f"off round {rep.start_round} &middot; split {rep.spr_split}</span>"
        f'<span class="badge {badge}">{esc(rep.verdict)}</span>'
        f'<span class="tag">{rep.agree}/{len(rep.sheet_swiss)} agree</span>'
        f'</summary><div class="body">{settings}{standings}{body}</div></details>'
    )


def render_html(reports: list[RoundReport], meta: dict) -> str:
    swiss = [r for r in reports if not r.is_cop]
    cop = [r for r in reports if r.is_cop]
    sn, sm, sr = stats([r for rep in swiss for r in rep.sheet_swiss])
    bn, bm, br = stats([r for rep in swiss for r in rep.baxter_swiss])
    agree = sum(r.agree for r in swiss)
    total = sum(len(r.sheet_swiss) for r in swiss)

    summary_rows = ""
    for rep in reports:
        s_n, s_m, s_r = stats(rep.sheet_swiss)
        b_n, b_m, b_r = stats(rep.baxter_swiss)
        summary_rows += (
            f'<tr><td><a class="rlink" href="#r{rep.round_no}">R{rep.round_no}</a></td>'
            f"<td>{esc(rep.code)}/{rep.spr_split}</td>"
            f"<td>{esc(rep.baxter_engine)}</td>"
            f'<td class="num">{rep.agree}/{len(rep.sheet_swiss)}</td>'
            f'<td class="num">{s_n}</td><td class="num">{s_m:.1f}</td>'
            f'<td class="num">{s_r}</td>'
            f'<td class="num">{b_n}</td><td class="num">{b_m:.1f}</td>'
            f'<td class="num">{b_r}</td>'
            f'<td><span class="badge {BADGE[rep.verdict]}">{esc(rep.verdict)}</span></td></tr>'
        )

    counts: dict[str, int] = {}
    for rep in reports:
        counts[rep.verdict] = counts.get(rep.verdict, 0) + 1
    tally = " &middot; ".join(f"{v} {k}" for k, v in sorted(counts.items()))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Swiss comparison — {esc(meta["file"])}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Swiss pairing comparison</h1>
<p class="sub">{esc(meta["file"])} — the sheet's own Swiss output against Baxter's engine,
for every round paired with Swiss or SwissPlusRandom.</p>

<div class="meta">
  <div><b>Sheet settings</b> <code>{esc(meta["sheet_settings"])}</code></div>
  <div><b>Baxter config</b> <code>{esc(meta["baxter_config"])}</code>,
       spr_split <code>{esc(meta["split"])}</code></div>
  <div><b>Rounds compared</b> <code>{esc(meta["rounds"])}</code></div>
</div>
{"".join(f'<p class="warn-box">{esc(w)}</p>' for w in meta.get("warnings", []))}

<div class="cards">
  <div class="card"><div class="k">Matchups agreeing</div>
    <div class="v">{agree}/{total}</div>
    <div class="n">Swiss-vs-Swiss rounds only &middot; Swiss slice, random portion
      excluded</div></div>
  <div class="card"><div class="k">Sheet</div>
    <div class="v">{sm:.2f}</div>
    <div class="n">mean dist over {sn} matchups &middot; {sr} repeat games</div></div>
  <div class="card"><div class="k">Baxter</div>
    <div class="v">{bm:.2f}</div>
    <div class="n">mean dist over {bn} matchups &middot; {br} repeat games</div></div>
  <div class="card"><div class="k">COP rounds</div>
    <div class="v">{len(cop)}</div>
    <div class="n">the sheet split the field, so Baxter used COP &middot; reported,
      not scored</div></div>
  <div class="card"><div class="k">Per-round verdicts</div>
    <div class="v" style="font-size:.95rem;font-weight:500">{tally}</div></div>
</div>

<h2>Summary</h2>
<p class="sub" style="margin:-.2rem 0 .8rem">Settings are inferred per round from
the pairings the sheet produced, because the workbook stores only the final
config. Rounds where the sheet split the field are paired with COP on the Baxter
side, and are reported but not scored.</p>
<div class="scroll"><table>
<thead><tr><th>Round</th><th>Code/split</th><th>Baxter engine</th><th class="num">Agree</th>
<th class="num">Sheet n</th><th class="num">dist</th><th class="num">reps</th>
<th class="num">Baxter n</th><th class="num">dist</th><th class="num">reps</th>
<th>Verdict</th></tr></thead>
<tbody>{summary_rows}</tbody></table></div>

<h2>Rounds</h2>
<div class="controls">
  <button id="expand">Expand all</button>
  <button id="collapse">Collapse all</button>
  <button id="filter" data-on="0">Hide identical rounds</button>
</div>
{"".join(render_round_html(r) for r in reports)}

<div class="legend"><dl>
<dt>dist</dt><dd>Distance between the players in the standings the round pairs off.
  A win group is always a contiguous slice of the standings, so this is also the
  intra-group distance the algorithms weigh — what the sheet's
  <code>distance &lt; swiss_distance</code> filter and Baxter's
  <code>max_distance</code> both test.</dd>
<dt>grp</dt><dd><i>same</i> when both players were in one win group;
  <i>promoted</i> when the pairing crossed groups, which happens only when the
  pairing code pulls players up because someone in the top group has no
  acceptable opponent left. Measured against the grouping the round starts from,
  so it flags that a promotion happened, not which one.</dd>
<dt>reps</dt><dd>How many times the two have already played each other. Non-zero
  is a rematch, shown in red.</dd>
<dt>Swiss slice vs random portion</dt><dd>SwissPlusRandom pairs only the top
  <code>spr_split</code> players by Swiss; the rest are random and will never
  agree between two implementations, so they are listed separately and excluded
  from every total.</dd>
<dt>Verdict</dt><dd>Fewer rematches wins; a tie on rematches goes to the round
  with the lower mean distance. COP rounds are <b>not scored</b> — distance and
  rematches are Swiss's objective, and COP deliberately pairs contenders against
  each other however far apart they sit, so grading it on tightness would mark
  correct behaviour as a regression.</dd>
<dt>Baxter engine</dt><dd><i>Swiss</i> / <i>SwissPlusRandom</i> where the round
  paired the whole field, <i>COP</i> where the inferred split actually divided it.
  A split is the sheet's hand-set estimate of who is still in contention; COP
  computes that by simulation, so it is what Baxter would really use there. The
  trailing number is how many rounds back the standings come from &mdash; 1 for
  the sheet's S/SPR, 2 for its ST/STPR &mdash; which the strategy name alone does
  not distinguish.</dd>
<dt>Per-round settings</dt><dd><i>Inferred</i> is what best reproduces the
  pairing that round produced, searched over the whole parameter grid; a
  parameter the round cannot discriminate shows as "any" or a range rather than a
  single value, and the trailing count says how much of the round the fit
  actually explains. <i>Stored</i> is what the workbook holds — the configuration
  the tournament <b>finished</b> with, not necessarily what was in force for that
  round, so the two lines can legitimately disagree. <i>Baxter used</i> is the
  strategy and settings its side was actually paired with.</dd>
</dl></div>
</div><script>{JS}</script></body></html>
"""


def build_reports(tournament, args, config) -> tuple[list, dict]:
    """Compare every Swiss round of one workbook, filling the inference cache."""
    rounds = [
        r
        for r in sorted(tournament.generated_pairings)
        if tournament.settings.round_codes.get(r) in SWISS_CODES
    ]
    meta = {
        "file": tournament.path.name,
        "sheet_settings": tournament.settings.swiss,
        "baxter_config": config or "inferred per round (see each round)",
        "split": "inferred per round" if args.spr_split == 0 else args.spr_split,
        "rounds": rounds,
        "warnings": [],
    }
    cache = load_inference_cache(tournament.path)
    if args.refresh_inference:
        cache["rounds"] = {}
        cache["grid"] = {
            "weights": list(CANDIDATE_WEIGHTS),
            "distances": list(CANDIDATE_DISTANCES),
        }
    known = len(cache["rounds"])
    reports = [build_round(tournament, r, args.spr_split, config, cache) for r in rounds]
    if len(cache["rounds"]) != known:
        save_inference_cache(tournament.path, cache)

    # Sanity check on the whole workbook: given the right settings, the sheet's
    # own code reproduces its own Swiss pairings exactly — that is what the
    # inference searches for. If it cannot, the inputs no longer describe the
    # tournament that was played (state the workbook overwrites and cannot
    # recover, an edited tab, a manual override), and nothing downstream of that
    # means anything. Say so rather than presenting the agreement figures.
    matched = sum(r.fit_score[0] for r in reports)
    of = sum(r.fit_score[1] for r in reports)
    if of and matched < of:
        meta["warnings"].append(
            f"The sheet's own pairing code reproduces only {matched} of {of} of "
            "its recorded Swiss matchups for this workbook, even at its best-fit "
            "settings. It cannot re-derive the tournament it recorded, so the "
            "inputs and the results disagree about something this tooling has "
            "not identified. Treat every agreement figure below as unreliable."
        )
    return reports, meta


def write_report(reports, meta, path: Path, fmt_name: str) -> None:
    with open(path, "w", encoding="utf-8") as out:
        if fmt_name == "html":
            out.write(render_html(reports, meta))
        else:
            render_text(out, reports, meta)


def workbooks(path: Path) -> list[Path]:
    """The workbook(s) a path refers to: one file, or every .xlsx in a directory.

    Excel lock files (`~$name.xlsx`) are skipped — they are not workbooks.
    """
    if path.is_dir():
        return sorted(p for p in path.glob("*.xlsx") if not p.name.startswith("~$"))
    return [path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="an .xlsx workbook, or a directory of them (all are reported)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write a single report here. Without it, reports are named after "
        "each workbook and written to a reports/ subdirectory beside them.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "html", "both"),
        help="output format. Defaults to html when --out ends in .html, else "
        "text; when writing to reports/, defaults to both.",
    )
    parser.add_argument(
        "--spr-split",
        type=int,
        default=0,
        help="size of the Swiss-paired slice in SwissPlusRandom rounds. Default 0 "
        "fits it per round to whatever best reproduces the sheet; pass a number to "
        "pin it (Baxter's own default is 10).",
    )
    parser.add_argument(
        "--refresh-inference",
        action="store_true",
        help="recompute the per-round settings even if the .inferred.json cache "
        "beside the workbook already has them",
    )
    parser.add_argument(
        "--sheet-settings",
        action="store_true",
        help="run Baxter with the sheet's swiss_weight/swiss_distance instead of "
        "Baxter's own defaults",
    )
    args = parser.parse_args()

    found = workbooks(args.path.expanduser())
    if not found:
        raise SystemExit(f"no .xlsx workbooks found in {args.path}")
    if args.out and len(found) > 1:
        raise SystemExit(
            f"--out names a single file but {len(found)} workbooks were found; "
            "drop it to write into reports/"
        )

    failures = []
    for workbook in found:
        try:
            tournament = load_tournament(workbook)
            config = (
                to_swiss_config(tournament.settings) if args.sheet_settings else None
            )
            reports, meta = build_reports(tournament, args, config)
        except Exception as exc:  # noqa: BLE001 - one bad workbook must not stop the batch
            print(f"skipped {workbook.name}: {exc!r}", file=sys.stderr)
            failures.append(workbook.name)
            continue

        if args.out:
            fmt_name = args.format or (
                "html" if args.out.suffix.lower() == ".html" else "text"
            )
            if fmt_name == "both":
                raise SystemExit("--format both needs the reports/ directory, not --out")
            write_report(reports, meta, args.out, fmt_name)
            print(f"wrote {args.out}")
            continue

        out_dir = workbook.parent / "reports"
        out_dir.mkdir(exist_ok=True)
        for fmt_name in ("text", "html") if (args.format or "both") == "both" else [args.format]:
            suffix = ".html" if fmt_name == "html" else ".txt"
            target = out_dir / (workbook.stem + suffix)
            write_report(reports, meta, target, fmt_name)
            print(f"wrote {target}")

    if failures:
        print(f"\n{len(failures)} workbook(s) skipped: {', '.join(failures)}", file=sys.stderr)


if __name__ == "__main__":
    main()
