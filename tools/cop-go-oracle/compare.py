"""Pair the same positions with upstream COP (the Go oracle) and Baxter's port.

    uv run --no-sync python tools/cop-go-oracle/compare.py            # every fixture
    uv run --no-sync python tools/cop-go-oracle/compare.py a.json b.json
    ... --out DIR     # per case: both engines' pairings, the converted input, COP's log
    ... --json        # one JSON object per case instead of the table
    ... --strict      # exit 1 if any comparable case is worse than upstream
    ... --stages      # the Go re-port's intermediates vs the oracle's log, stage by stage

Each request is converted (``convert.py``), paired by ``cop-go-oracle`` (built
here, so it is never stale) and by ``scrabble_pairing_py.pair_json``, and the two
rounds are compared as sets of games — who plays whom, not who goes first,
which liwords does not decide.

Two checks run before any comparison, because each makes one meaningless:

- **Standings.** COP logs the standings it computed; they are checked against
  the records the converted slips give. A mismatch means the converter, not the
  port, is wrong — reported as ``CONVERT``.
- **Pins.** A game already paired in the request (``fixed_pairings`` after
  conversion) must appear in Baxter's round. One that does not is ``PIN``:
  Baxter broke a pairing a director fixed, whatever the weights say.

A disagreement is only a finding when the case is **comparable**: the converter
could express everything the request asks for, and the oracle's run was not
clock-bound (``maybeTimeLimited``: see the README). Other cases are still run
and shown, marked, since a gross difference there is worth knowing too.

**How much a disagreement matters** is scored on upstream's own terms. COP logs
the weight of every pair it considered and picks the minimum-weight matching, so
Baxter's pairing can be totalled on the same table: a gap of 0 is a different
but equally optimal matching (a tie upstream broke another way); a positive gap
is a pairing upstream considers worse. A pair upstream excluded outright has no
weight, only a code in the log's ``C`` column saying why — ``GB`` (gibsonized),
``CB`` (someone else is forced to take the bye), … — and a Baxter game on such
a pair is reported as **barred**, with the code, rather than scored.

The Rust engine is used as installed in the venv — after editing the crate, run
``make rust-engine`` first.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from convert import BYE, convert  # noqa: E402

ORACLE = HERE / "cop-go-oracle"


def build_oracle():
    subprocess.run(["go", "build", "-o", str(ORACLE), "."], cwd=HERE, check=True)


def run_oracle(request_text, workers):
    done = subprocess.run(
        [str(ORACLE), f"-workers={workers}"],
        input=request_text, capture_output=True, text=True, check=True,
    )
    return json.loads(done.stdout)


def games_from_oracle(pairings, names, skip):
    """liwords' opponent-index array as a set of games, by name."""
    games = set()
    for i, opp in enumerate(pairings):
        if i in skip or opp < 0:
            continue
        other = BYE if opp == i else names[opp]
        games.add(frozenset((names[i], other)))
    return games


def games_from_engine(round_result):
    return {frozenset((p["first"], p["second"])) for p in round_result["pairings"]}


_RANKED_NAME = re.compile(r"^\d+ \(#\d+/[^)]*\) (.*)$")


def _log_name(cell):
    cell = cell.strip()
    if cell == "BYE":
        return BYE
    m = _RANKED_NAME.match(cell)
    return m.group(1).strip() if m else None


def parse_weights(log):
    """COP's logged pairing weights and the total of the matching it chose.

    ``{frozenset(names): weight}``, where a barred pair's weight is its
    constraint code (a str) instead of an int. ``({}, None)`` if the log has no
    table."""
    m = re.search(r"\*\* Pairing Weights \*\*\n\*+\n\n(.*?)\n-+\n(.*?)\n\n", log, re.S)
    total = re.search(r"^Total Weight: (-?\d+)", log, re.M)
    if not m or not total:
        return {}, None
    header = [c.strip() for c in m.group(1).split("|")]
    total_col, code_col = header.index("Total"), header.index("C")
    weights = {}
    for line in m.group(2).splitlines():
        cols = line.split("|")
        a, b = _log_name(cols[0]), _log_name(cols[3])
        if a is None or b is None:
            continue
        raw = cols[total_col].strip()
        weights[frozenset((a, b))] = int(raw) if raw else (cols[code_col].strip() or "?")
    return weights, int(total.group(1))


def score(games, weights):
    """``games`` totalled on COP's weights: ``(total, barred codes)``.

    Barred games are listed, not summed. Games absent from the table (pinned,
    prepaired games, which COP does not weigh) are skipped — both sides hold the
    same ones, so they drop out of any difference.
    """
    total, barred = 0, []
    for game in games:
        weight = weights.get(game)
        if isinstance(weight, str):
            barred.append(weight)
        elif weight is not None:
            total += weight
    return total, barred


def logged_records(log):
    """``{name: (wins, spread)}`` from the standings table in COP's log."""
    m = re.search(r"Initial Sim Results.*?\n-+\n(.*?)\n\n", log, re.S)
    if not m:
        return None
    records = {}
    for line in m.group(1).splitlines():
        cols = [c.strip() for c in line.split("|")]
        records[cols[2]] = (float(cols[3]), int(cols[4]))
    return records


def converted_records(engine_input):
    """The same records as the engine will count them from the converted slips."""
    records = {}
    for slip in engine_input["result_slips"]:
        spread = slip["winner_score"] - slip["loser_score"]
        for name, x in ((slip["winner_name"], spread), (slip["loser_name"], -spread)):
            wins, total = records.get(name, (0.0, 0))
            records[name] = (wins + (1 if x > 0 else 0.5 if x == 0 else 0), total + x)
    return records


def standings_mismatches(log, engine_input):
    """Players whose record differs between COP's log and the conversion."""
    logged = logged_records(log)
    if logged is None:
        return []
    ours = converted_records(engine_input)
    return [
        f"{name}: upstream {rec}, converted {ours.get(name, (0.0, 0))}"
        for name, rec in logged.items()
        if ours.get(name, (0.0, 0)) != rec
    ]


def compare(path, workers):
    request_text = path.read_text()
    request = json.loads(request_text)
    conversion = convert(request)
    case = {
        "case": path.stem,
        "round": conversion.round,
        "warnings": conversion.warnings,
    }

    oracle = run_oracle(request_text, workers)
    response = oracle["response"]
    case["timeLimited"] = oracle["oracle"]["maybeTimeLimited"]
    case["oracleSeconds"] = round(oracle["oracle"]["elapsedSeconds"], 2)
    case["log"] = response.get("log", "")
    case["engineInput"] = conversion.engine_input

    import scrabble_pairing_py

    out = json.loads(scrabble_pairing_py.pair_json(json.dumps(conversion.engine_input)))
    engine_round = next((r for r in out if r["round"] == conversion.round), None)

    go_error = response["errorCode"] != "SUCCESS"
    rust_error = engine_round is None or bool(engine_round.get("error"))
    if go_error or rust_error:
        # Both refusing is agreement (e.g. every round already paired).
        case["status"] = "both refuse" if go_error and rust_error else "error"
        case["oracleError"] = (
            f"{response['errorCode']}: {response.get('errorMessage', '')}" if go_error else None
        )
        case["engineError"] = (
            (engine_round or {}).get("error") or "no pairing for that round"
        ) if rust_error else None
        return case

    removed = set(request.get("removedPlayers") or [])
    go = games_from_oracle(response["pairings"], conversion.names, removed)
    rust = games_from_engine(engine_round)

    case["standingsMismatch"] = standings_mismatches(case["log"], conversion.engine_input)
    pins = {
        frozenset(pair)
        for pairs in conversion.engine_input["fixed_pairings"].values()
        for pair in pairs
    }
    case["brokenPins"] = sorted(sorted(g) for g in pins - rust)
    case["oracleGames"] = sorted(sorted(g) for g in go)
    case["engineGames"] = sorted(sorted(g) for g in rust)
    case["differing"] = len(go - rust)
    case["games"] = len(go)
    case["status"] = "match" if go == rust else "differ"
    if go != rust:
        weights, chosen = parse_weights(case["log"])
        if chosen is not None:
            # Pinned games are placed, not matched — upstream codes them `PP`
            # in its own table — and both sides hold them, so they are left out.
            theirs, _ = score(go - pins, weights)
            ours, barred = score(rust - pins, weights)
            if barred:
                case["barred"] = barred
            else:
                case["weightGap"] = ours - theirs
    return case


def verdict(case):
    """match / tie / WORSE / error, starred when the case is not comparable.

    ``tie`` is a different pairing upstream scores no worse; ``BARRED`` uses a
    pair upstream excluded. Without a weight table to score against, a
    disagreement is plain ``DIFFER``.
    """
    if case["status"] in ("error", "both refuse"):
        return case["status"]
    if case.get("standingsMismatch"):
        return "CONVERT"
    if case.get("brokenPins"):
        return "PIN"
    if case["status"] == "match":
        return "match"
    if case.get("barred"):
        word = "BARRED"
    elif "weightGap" not in case:
        word = "DIFFER"
    elif case["weightGap"] <= 0:
        word = "tie"
    else:
        word = "WORSE"
    comparable = not case["warnings"] and not case["timeLimited"]
    return word if comparable else word.lower() + "*"


def notes(case):
    bits = []
    if case.get("standingsMismatch"):
        bits.append(f"converter disagrees with upstream on {len(case['standingsMismatch'])} record(s)")
    if case.get("brokenPins"):
        pins = ", ".join(" v ".join(g) for g in case["brokenPins"])
        bits.append(f"Baxter broke {len(case['brokenPins'])} pinned game(s): {pins}")
    if case.get("barred"):
        codes = ", ".join(sorted(set(case["barred"])))
        bits.append(f"{len(case['barred'])} game(s) on pairs upstream bars ({codes})")
    if case.get("weightGap"):
        bits.append(f"weight +{case['weightGap']:,}")
    if case["status"] in ("error", "both refuse"):
        if case.get("oracleError"):
            bits.append(f"oracle: {case['oracleError']}")
        if case.get("engineError"):
            bits.append(f"engine: {case['engineError']}")
    if case.get("timeLimited"):
        bits.append("clock-bound")
    bits.extend(case["warnings"][:2])
    if len(case["warnings"]) > 2:
        bits.append(f"+{len(case['warnings']) - 2} more warnings")
    return "; ".join(bits)


def write_case(out_dir, case):
    d = out_dir / case["case"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "cop.log").write_text(case["log"])
    (d / "engine_input.json").write_text(json.dumps(case["engineInput"], indent=2))
    (d / "pairings.json").write_text(json.dumps({
        k: case.get(k) for k in ("oracleGames", "engineGames", "warnings", "timeLimited")
    }, indent=2))


# ---------------------------------------------------------------------------
# --stages: the Go re-port (scrabble-pairing/src/strategies/cop_go) against the
# oracle's log, one intermediate at a time. The port runs upstream's own
# PairRequest (no converter), so every stage should match exactly unless the
# oracle's run was clock-bound. See plans/PLAN_COP_GO_PORT.md.
# ---------------------------------------------------------------------------

CRATE = HERE.parent.parent / "scrabble-pairing"
TRACE = CRATE / "target" / "release" / "examples" / "cop_trace"


def build_trace():
    subprocess.run(
        ["cargo", "build", "--release", "--quiet", "--example", "cop_trace"],
        cwd=CRATE, check=True,
    )


def run_trace(request_text):
    done = subprocess.run(
        [str(TRACE)], input=request_text, capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def _table_after(log, title_pattern, table_title="Totals"):
    """Rows of the ``table_title`` table that follows the section whose title
    matches ``title_pattern``, as lists of cell strings. None if absent."""
    m = re.search(title_pattern, log)
    if not m:
        return None, None
    rest = log[m.end():]
    t = re.search(rf"\*\* {table_title} \*\*\n\*+\n\n(.*?)\n-+\n(.*?)\n\n", rest, re.S)
    if not t:
        return m, None
    rows = [[c.strip() for c in line.split("|")] for line in t.group(2).splitlines()]
    return m, rows


def logged_sims(log, title):
    """A logged sim-results section: ``{factor, players, final_ranks, total_sims}``."""
    m, rows = _table_after(log, rf"\*\* {title} \(factor ceiling of (\d+)\) \*\*")
    if rows is None:
        return None
    total = re.search(r"^Total Sims: (\d+)$", log[m.end():], re.M)
    return {
        "factor": int(m.group(1)),
        "players": [int(r[1]) - 1 for r in rows],
        "final_ranks": [[int(c) for c in r[5:]] for r in rows],
        "total_sims": int(total.group(1)) if total else None,
    }


def _diff_sims(name, ours, theirs):
    if theirs is None and ours is None:
        return []
    if theirs is None or ours is None:
        return [f"{name}: {'only Rust' if theirs is None else 'only Go'} ran it"]
    out = []
    for key in ("factor", "players", "total_sims"):
        if ours[key] != theirs[key]:
            out.append(f"{name} {key}: Rust {ours[key]} vs Go {theirs[key]}")
    if ours["final_ranks"] != theirs["final_ranks"]:
        bad = [i for i, (a, b) in enumerate(zip(ours["final_ranks"], theirs["final_ranks"])) if a != b]
        out.append(f"{name} final_ranks differ at starting rank(s) {bad[:5]}")
    return out


def compare_stages(path, workers):
    request_text = path.read_text()
    oracle = run_oracle(request_text, workers)
    log = oracle["response"].get("log", "")
    trace = run_trace(request_text)
    code = oracle["response"]["errorCode"]
    if code != "SUCCESS" or trace.get("error"):
        ours = (trace.get("error") or {}).get("code", "SUCCESS")
        verdict = [] if ours == code else [f"Rust {ours} vs Go {code}"]
        return {"case": path.stem, "timeLimited": False, "stages": {"verify": verdict}}
    stages = {
        "sims": _diff_sims("initial", trace["initial"], logged_sims(log, "Initial Sim Results"))
        + _diff_sims("improved", trace["improved"], logged_sims(log, "Improved Factor Sim Results")),
    }
    return {
        "case": path.stem,
        "timeLimited": oracle["oracle"]["maybeTimeLimited"],
        "stages": stages,
    }


def stages_main(args):
    requests = args.requests or sorted((HERE / "fixtures").glob("*.json"))
    build_oracle()
    build_trace()
    failing = 0
    for path in requests:
        case = compare_stages(path, args.workers)
        cells = []
        for stage, problems in case["stages"].items():
            cells.append(f"{stage}:{'ok' if not problems else 'DIFF'}")
        star = " (clock-bound)" if case["timeLimited"] else ""
        print(f"{' '.join(cells):12} {case['case']}{star}")
        for stage, problems in case["stages"].items():
            for p in problems[:4]:
                print(f"    {stage}: {p}")
        if any(case["stages"].values()) and not case["timeLimited"]:
            failing += 1
    print(f"\n{failing} non-clock-bound case(s) differ")
    if args.strict and failing:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("requests", nargs="*", type=Path,
                    help="PairRequest JSON files (default: every fixture)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, help="write per-case detail here")
    ap.add_argument("--json", action="store_true", help="one JSON object per case")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any comparable case is worse than upstream")
    ap.add_argument("--stages", action="store_true",
                    help="compare the Go re-port's intermediates with the oracle's log")
    args = ap.parse_args()
    if args.stages:
        return stages_main(args)

    requests = args.requests or sorted((HERE / "fixtures").glob("*.json"))
    build_oracle()

    cases = []
    for path in requests:
        case = compare(path, args.workers)
        cases.append(case)
        if args.out:
            write_case(args.out, case)
        if args.json:
            slim = {k: v for k, v in case.items() if k not in ("log", "engineInput")}
            print(json.dumps(slim), flush=True)
        else:
            games = (f"{case['differing']:>2}/{case['games']:<2}"
                     if "games" in case else "  -  ")
            print(f"{verdict(case):11} {games} {case['case']:<62} {notes(case)}", flush=True)

    counts = {}
    for case in cases:
        counts[verdict(case)] = counts.get(verdict(case), 0) + 1
    if not args.json:
        print()
        print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        print("tie: different but equally optimal on upstream's weights; WORSE: "
              "heavier on them; BARRED: uses a pair upstream excludes; PIN: Baxter "
              "broke a pinned game; CONVERT: the converter's standings disagree with "
              "upstream's. Lower case and * = not comparable (clock-bound, or asks "
              "for something the engine cannot express). Games: differing/total.")
    failing = ("WORSE", "BARRED", "DIFFER", "PIN", "CONVERT", "error")
    if args.strict and any(counts.get(k) for k in failing):
        sys.exit(1)


if __name__ == "__main__":
    main()
