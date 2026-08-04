# sheet-pairing

Runs the Google Sheets tournament script's pairing code as a standalone Node
program, so a workbook's pairings can be reproduced and traced outside the
spreadsheet — and compared against Baxter's engine on identical inputs.

The pairing logic is **not** reimplemented here. `build.py` lifts each top-level
declaration out of `Code.ts` byte-for-byte, dropping only the functions that read
and write sheet ranges, and concatenates the result with the blossom matcher the
script calls. What runs is the algorithm the tournaments actually used.

## Files

| file | |
| --- | --- |
| `build.py` | regenerates `sheet_pairing.js` from a `googlesheets-tournament-script` checkout |
| `sheet_pairing.js` | generated bundle (checked in, so the tool runs without that checkout) |
| `export_inputs.py` | dumps a workbook's five input ranges as JS literals |
| `run.js` | drives the pairing code against an exported input file |

## Use

```bash
# 1. Export a workbook's inputs to hardcoded JS literals.
uv run python tools/sheet-pairing/export_inputs.py ~/tmp/results/tourney.xlsx \
    -o /tmp/tourney.input.js

# 2. Pair with the sheet's own code.
node tools/sheet-pairing/run.js /tmp/tourney.input.js              # every round
node tools/sheet-pairing/run.js /tmp/tourney.input.js --round 10   # one round
node tools/sheet-pairing/run.js /tmp/tourney.input.js --standings 9
node tools/sheet-pairing/run.js /tmp/tourney.input.js --json
```

### Re-pairing a played round

A round that already has all its results is reported straight back from those
results — `runPairings` only pairs rounds it has to. To make the algorithm
actually solve round *N*, drop the results from *N* onwards:

```bash
node tools/sheet-pairing/run.js /tmp/tourney.input.js --through 9 --round 10
```

### Tracing

`--trace` lets the pairing code's own `console.log` calls through. This is the
main reason the tool exists: the Swiss helper narrates its score groups, group
merges, promotions and repeat-limit escalations, which is the only direct view of
*why* it chose a pairing.

```bash
node tools/sheet-pairing/run.js /tmp/tourney.input.js --through 16 --round 17 --trace
```

```
swiss pairing based on round 16
merging bottom two groups
promoting two into 0
swiss settings: Settings { swiss_weight: 40, swiss_distance: 8 }
reps: 2          <- repeat limit escalated; a rematch is now allowed
```

## The input file

`export_inputs.py` writes the five ranges the Apps Script reads, in the shape its
`make*` builders expect — so the real parsers run too, not just the pairing:

| range | columns |
| --- | --- |
| `results` | `Results!B2:H` — round, winner, winner score, loser, loser score, first? |
| `entrants` | `Entrants!A2:E` — name, rating, _, table, seed |
| `roundPairings` | `Settings!A2:B` — round, pairing code |
| `settings` | `Settings!D2:E` — setting name, value |
| `fixedPairings` | `FixedPairing!A2:D` — round, player 1, player 2, force p1 start |

Because it is plain JS literals, it doubles as a what-if editor: change
`swiss_distance` in `settings`, or a score in `results`, and re-run.

That matters for these workbooks specifically. **The `Settings` tab records only
the config a tournament finished with**, and at least one event was retuned
mid-event — so re-pairing an early round with the stored settings will not always
reproduce what was played. Sweeping the setting here is how you recover what was
actually in effect.

## Regenerating the bundle

```bash
uv run python tools/sheet-pairing/build.py \
    --source ~/github/coco/googlesheets-tournament-script
```

`build.py` fails loudly if a name in its exclusion list is missing from `Code.ts`,
if an export is missing from the kept declarations, or if any spreadsheet API
call survives into the bundle — so an upstream rename cannot silently produce a
half-extracted module.
