# COP oracle (Go, upstream)

Runs **upstream COP, unmodified**, so Baxter's Rust port
(`scrabble-pairing/src/strategies/cop.rs`) can be checked against it.

Upstream is now liwords' Go port of COP —
[`woogles-io/liwords` `pkg/pair`](https://github.com/woogles-io/liwords/tree/master/pkg/pair) —
where development continued after the Perl `COP.pm`. It is the source of truth;
`tools/cop-oracle/` (the Perl oracle) describes where the Rust port came from,
not where COP is going.

Unlike the Perl oracle, nothing here needs setting up by hand: upstream is a Go
module dependency pinned in `go.mod`, with its hash in `go.sum`.

## Pin

`github.com/woogles-io/liwords v0.3.2-0.20260921030559-4cba544a9801`
— commit `4cba544` (2026-09-20).

To move it:

```bash
GONOSUMDB=github.com/woogles-io/liwords go get github.com/woogles-io/liwords@<commit>
go mod tidy
go run ./cmd/dump-fixtures -out fixtures   # and add any new upstream fixtures to its list
```

`GONOSUMDB` is needed **only when moving the pin**: sum.golang.org has no
record of liwords (it 404s even for tagged releases), so the first fetch of a
new version cannot be verified there. Once the hash is in `go.sum`, every build
checks against that and never asks sum.golang.org — a clean machine builds with
plain `go build`.

Also re-check `simTimeLimit` in `main.go` against upstream's sim budgets.

## Usage

```bash
go build -o cop-go-oracle .
./cop-go-oracle < fixtures/albany_after_round15.json
./cop-go-oracle -log=false < request.json      # drop COP's log
```

Input is a liwords `PairRequest` as protobuf JSON (`api/proto/ipc/pair.proto`
upstream). Output:

```json
{
  "response": { "errorCode": "SUCCESS", "pairings": [...], "gibsonizedPlayers": [...], "log": "..." },
  "oracle":   { "upstream": "v0.3.2-...", "workers": 4, "elapsedSeconds": 0.03,
                "totalSims": 100000, "maybeTimeLimited": false }
}
```

- `pairings[i]` is player `i`'s opponent index; `pairings[i] == i` is a bye.
  Requests use the same convention for past rounds, with `-1` for "not yet
  paired" in a partially paired last round, and `divisionResults[r][i]` is
  player `i`'s score in round `r`.
- `log` is COP's own account of its decisions — contender groups,
  gibsonization, control loss, the weight table. It is the thing to diff when
  pairings disagree.

## Reproducibility — read before comparing

A given request gives the same answer every time, **with two conditions**:

1. **Fixed worker count.** COP simulates on several goroutines, each drawing its
   own RNG stream, so the worker count changes the draws. Left to
   `runtime.NumCPU()` the same request pairs differently on different machines;
   the oracle pins it (`-workers`, default 4) through upstream's
   `NumSimWorkersOverride`. Keep it fixed across any comparison.
2. **Not clock-bound.** COP re-simulates until a hopefulness threshold is met
   *or a 6-second budget runs out*, and does not say which. When it is the clock,
   the simulation count — and, near a decision boundary, the pairings — depend
   on the machine's speed. The oracle flags any run that took at least one
   budget as `maybeTimeLimited`; compare those statistically, not exactly.
   The budget cannot be raised without patching upstream, and it is the loop's
   only stop condition when the threshold is never met.

Sweep of the 26 fixtures at the pin (8 cores): all 26 identical across repeat
runs; `albany_after_round15` and `lakegeorge_csw_after_round8` are clock-bound
(~6s), `kingston2023_after_round15` is close (~5s) and may be on a slower
machine. `albany_csw_july2026_round28` returns `ALL_ROUNDS_PAIRED` by design.

## Scenarios

`fixtures/scenarios/` holds requests captured from upstream's **own tests**
(`dump-scenarios.sh`): the real-tournament fixtures leave some of COP's rules
unexercised, and upstream's `cop_test.go` / `scenarios_test.go` construct
positions aimed at them, in Go code rather than as data. The script copies the
pinned module's source out of the module cache, adds a one-line hook to
`COPPair` that writes each request to disk, runs the tests there, and keeps the
distinct COP requests (named by content hash). The oracle itself is never
modified. It skips upstream's timing loops and non-COP tests, still takes about
an hour, and samples 20 of the ~1000 near-identical 53-player requests one test
loops over. Rerun it when the pin moves.

```bash
tools/cop-go-oracle/dump-scenarios.sh
uv run --no-sync python tools/cop-go-oracle/compare.py --stages tools/cop-go-oracle/fixtures/scenarios/*.json
```

## Fixtures

`fixtures/` holds upstream's own test positions — the real tournaments in
`pkg/pair/testutils` (Albany, Kingston, Belleville, Lake George, …) — as request
JSON, written by `go run ./cmd/dump-fixtures`. Committed so the corpus is pinned
with the version it came from; regenerate when the pin moves.

## Comparing with Baxter's port

Baxter's COP is a port of this code (`scrabble-pairing/src/strategies/cop_go`,
`plans/PLAN_COP_GO_PORT.md`). Two comparisons, at two levels:

```bash
uv run --no-sync python tools/cop-go-oracle/compare.py --stages   # the port's core, stage by stage
make rust-engine                                                   # if the crate changed
uv run --no-sync python tools/cop-go-oracle/compare.py             # end to end, through Baxter's engine
```

**`--stages`** runs upstream's own request through the port's core (the
`cop_trace` example in `scrabble-pairing`, built on demand) and diffs each
intermediate against the oracle's log: the baseline sim tallies, the Precomp
Data table and destiny's child, every pair's constraint code and weights in
the Pairing Weights table, and the final pairings. No converter is involved, so
these should be **exact** — the port reproduces upstream's RNG and worker
seeding — except where the oracle's run was clock-bound. The port's sim cap is
raised for this, so only upstream's clock can separate them.

**End to end** (no flag) converts each request to Baxter's engine input
(`convert.py`; its docstring has the mapping and what is lossy), pairs it with
the engine through the Python extension, and compares the games. A difference
is scored on upstream's own logged weights:

| Verdict | Meaning |
|---|---|
| `match` | Same games. |
| `tie` | Different games, equally optimal on **upstream's own weights** (COP logs every pair's weight; Baxter's pairing is totalled on that table). |
| `WORSE` | Heavier than upstream's choice on upstream's weights; the gap is shown. |
| `BARRED` | Uses a pair upstream excludes outright; the log's constraint code is shown (`GB` gibsonized, `CB` forced bye, `F3` Factor 3, `PP` prepaired, `KH` class KOTH, `TB` top-down bye, …). |
| `PIN` | Baxter broke a game already paired in the request — a director's fixed pairing. |
| `CONVERT` | The converted standings disagree with the ones COP logged: a converter bug, so nothing else in the case means anything. Checked on every run. |
| `both refuse` / `error` | One or both engines declined to pair. |

Lower case with `*` means the case is **not comparable**: clock-bound, or it
uses class prizes, which Baxter cannot express yet. `--out DIR` keeps each
case's log, converted input and both pairings; `--json` emits one object per
case; `--strict` exits 1 on any comparable failure.

**Any Baxter COP round** can be run through upstream too: the `cop_request`
example prints the request the port builds for a round of an engine input.

```bash
cd scrabble-pairing
cargo run --release --example cop_request -- 5 < engine_input.json \
    | ../tools/cop-go-oracle/cop-go-oracle
```

### Status (2026-09-21)

- `--stages`: all four stages exact on every fixture that is not clock-bound,
  and the final pairings match even on those.
- End to end: 22 of 26 match; one both-refuse (every round already paired);
  three differ only because class prizes decide their pairings.
- The earlier comparison against the COP.pm-era port found a real bug —
  fixed pairings enforced only as a weight, which the matching could outbid —
  fixed before the re-port (`scrabble-pairing/tests/cop_pins.rs`) and kept in
  it (upstream's `PP`).
- An upstream finding, from the `cop_request` route: in small fields, control
  loss (the leader must play the destiny's child) and the forced contender bye
  (that same player must take the bye) can together leave the leader no legal
  opponent, and upstream fails the round as overconstrained.
