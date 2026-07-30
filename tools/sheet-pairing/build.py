#!/usr/bin/env python
"""Extract the pairing logic out of the Google Sheets script into a Node module.

Code.ts is an Apps Script bound to a spreadsheet: the pairing algorithms are pure
functions, but they sit alongside code that reads and writes sheet ranges. This
lifts out everything except the sheet I/O, verbatim, and concatenates it with the
vendored blossom matcher into one loadable file.

Nothing is rewritten — each top-level declaration is copied byte-for-byte, so the
generated module runs the same algorithm the tournament actually used. Re-run
this after pulling a new Code.ts.

Usage: uv run python tools/sheet-pairing/build.py [--source DIR]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Declarations that talk to the spreadsheet (or only exist to drive the UI).
# Everything else is carried over untouched.
EXCLUDED = {
    # Apps Script UI / entry points
    "onOpen",
    "calculateStandings",
    "processSheet",
    # Range readers
    "collectSheetData",
    "collectResults",
    "collectEntrants",
    "collectRoundPairings",
    "collectFixedPairings",
    "collectSettings",
    # Sheet writers
    "outputPlayerStandings",
    "outputPairings",
    "outputStatistics",
    "outputPairingLog",
    "outputDiags",
    "getOrCreateSheet",
    "lastResultTimestamp",
    # Statistics display helpers, only used by outputStatistics
    "_show_win",
    "_show_loss",
    "_show_high_game",
    "_show_spread",
    "_show_high_spread",
}

# The module's public surface, mirroring the commented-out export block at the
# foot of Code.ts plus the pieces a runner needs to drive a whole tournament.
EXPORTS = [
    "makeResults",
    "makeEntrants",
    "makeRoundPairings",
    "makeFixedPairings",
    "makeSettings",
    "standingsAfterRound",
    "pairingsAfterRound",
    "runPairings",
    "getLastRound",
    "formatPairings",
    "calculateScoreGroups",
    "pairSwiss",
    "pairSwissPlusRandom",
    "pairSwissHelper",
    "pairSwissTop",
    "pairCandidates",
    "Results",
    "Entrants",
    "Repeats",
    "Starts",
    "Diags",
    "Settings",
    "Fixed",
    "BYES",
]


# Termination guards applied to the extracted code.
#
# Everything else is copied byte-for-byte; these three are the exception, and
# each is narrowly scoped to a loop or dereference that cannot terminate. That
# matters for faithfulness: a guard only fires where the original would hang
# forever or throw, so there is no pairing it can change — in those cases the
# original produces no output at all. Sweeping settings hits all three routinely
# (roughly half of a 105-point grid per round), and without the guards a single
# bad combination takes down the whole process instead of just reporting that it
# cannot pair.
#
# Each entry is (declaration, find, replace, why). build.py fails if one stops
# matching, so an upstream edit can't silently drop a guard.
PATCHES = [
    (
        "pairSwissHelper",
        """    while (groups[groups.length - 1].length < 6) {
      mergeBottom(groups);
      groups = groups.filter(e => e.length != 0);
    }""",
        """    while (groups.length > 1 && groups[groups.length - 1].length < 6) {
      mergeBottom(groups);
      groups = groups.filter(e => e.length != 0);
    }""",
        "mergeBottom returns early once a single group is left, so the loop "
        "spins forever on a field small enough to collapse to one group",
    ),
    (
        "pairSwissHelper",
        """  while (groups.length > 0) {
    dgroups = groups.map(g => g.map(p => [p.name, p.wins]));""",
        """  // Raising nrep past the field size cannot unlock any further candidate.
  var max_nrep = players.length + 1;
  while (groups.length > 0) {
    if (nrep > max_nrep) break;
    dgroups = groups.map(g => g.map(p => [p.name, p.wins]));""",
        "the retry path only ever raises the repeat limit, which never unlocks a "
        "distance-filtered edge, so a distance too narrow to admit a perfect "
        "matching loops forever",
    ),
    (
        "promote",
        """  var fst = groups[j].shift();
  groups[i].push(fst)""",
        """  if (top === undefined || top.length === 0) {
    return;
  }
  var fst = groups[j].shift();
  groups[i].push(fst)""",
        "promote2 can walk past the end of the group list; the original tests "
        "for undefined, logs, and then dereferences it anyway",
    ),
]


def apply_patches(blocks):
    """Apply PATCHES to the named declarations, failing loudly on a miss."""
    out = []
    applied = 0
    for name, text in blocks:
        for target, find, replace, _why in PATCHES:
            if name != target:
                continue
            if find not in text:
                raise SystemExit(
                    f"guard for {name!r} no longer matches Code.ts; "
                    "re-check the patch before trusting the output"
                )
            text = text.replace(find, replace, 1)
            applied += 1
        out.append((name, text))
    if applied != len(PATCHES):
        raise SystemExit(f"applied {applied} of {len(PATCHES)} guards")
    return out


DECL = re.compile(r"^(?:function|class|const|var)\s+(\w+)")


def split_declarations(source: str) -> list[tuple[str | None, str]]:
    """Slice the file into (name, text) top-level declarations.

    Every declaration in Code.ts starts in column 0, so the next one delimits the
    previous — no brace counting, and nothing inside a body can be mistaken for a
    boundary. Comment lines immediately above a declaration travel with it.
    """
    lines = source.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        match = DECL.match(line)
        if not match:
            continue
        # Walk back over the comment block (and blank lines) introducing it.
        start = i
        while start > 0:
            above = lines[start - 1].strip()
            if above.startswith("//") or above == "":
                start -= 1
            else:
                break
        starts.append((start, match.group(1)))

    blocks: list[tuple[str | None, str]] = []
    if starts and starts[0][0] > 0:
        blocks.append((None, "".join(lines[: starts[0][0]])))
    for idx, (start, name) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        blocks.append((name, "".join(lines[start:end])))
    return blocks


def build(source_dir: Path, out_path: Path) -> None:
    code = (source_dir / "Code.ts").read_text()
    blossom = (source_dir / "blossom.js").read_text()

    blocks = split_declarations(code)
    kept = [(n, t) for n, t in blocks if n is not None and n not in EXCLUDED]
    dropped = sorted(n for n, _ in blocks if n is not None and n in EXCLUDED)
    missing = EXCLUDED - {n for n, _ in blocks if n}
    if missing:
        raise SystemExit(f"EXCLUDED names not found in Code.ts: {sorted(missing)}")

    kept = apply_patches(kept)
    names = {n for n, _ in kept}
    unknown = [e for e in EXPORTS if e not in names]
    if unknown:
        raise SystemExit(f"EXPORTS not found among kept declarations: {unknown}")

    body = "".join(text for _, text in kept)
    guards = "\n".join(f"//   - {name}: {why}" for name, _, _, why in PATCHES)
    header = f'''// GENERATED by tools/sheet-pairing/build.py — do not edit.
//
// The pairing logic of the Google Sheets tournament script, lifted verbatim out
// of Code.ts with only the spreadsheet I/O removed, and bundled with the
// vendored blossom matcher it calls.
//
// Dropped from Code.ts ({len(dropped)} declarations, all sheet I/O or UI):
//   {", ".join(dropped)}
//
// Kept: {len(kept)} declarations, byte-for-byte apart from {len(PATCHES)}
// termination guards (see PATCHES in build.py). Each bounds a loop or
// dereference that cannot terminate, so it only fires where the original would
// hang or throw — there is no pairing it can change:
{guards}

'''
    footer = "\nmodule.exports = {\n" + "".join(f"  {n},\n" for n in EXPORTS) + "};\n"
    out_path.write_text(header + blossom + "\n" + body + footer)

    leaks = [
        line
        for line in (header + blossom + body).splitlines()
        if re.search(r"SpreadsheetApp|Utilities\.|getSheetByName|getRange\(", line)
    ]
    if leaks:
        raise SystemExit("spreadsheet API leaked into the bundle:\n" + "\n".join(leaks))

    print(f"wrote {out_path}  ({len(kept)} declarations kept, {len(dropped)} dropped)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / "github/coco/googlesheets-tournament-script",
        help="checkout of the googlesheets-tournament-script repo",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "sheet_pairing.js",
    )
    args = parser.parse_args()
    build(args.source.expanduser(), args.out)


if __name__ == "__main__":
    main()
