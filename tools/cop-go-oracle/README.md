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

## Fixtures

`fixtures/` holds upstream's own test positions — the real tournaments in
`pkg/pair/testutils` (Albany, Kingston, Belleville, Lake George, …) — as request
JSON, written by `go run ./cmd/dump-fixtures`. Committed so the corpus is pinned
with the version it came from; regenerate when the pin moves.

## Comparing with Baxter's port

```bash
make rust-engine                                           # if the crate changed
uv run --no-sync python tools/cop-go-oracle/compare.py     # every fixture
uv run --no-sync python tools/cop-go-oracle/compare.py fixtures/default.json --out /tmp/cop
```

`convert.py` maps a `PairRequest` onto Baxter's engine input (its docstring has
the full mapping and what is lossy); `compare.py` pairs each case both ways and
compares the rounds as sets of games. `--out DIR` keeps, per case, COP's log,
the converted input and both pairings; `--json` emits one object per case;
`--strict` exits 1 on any comparable failure.

Each case gets one verdict:

| Verdict | Meaning |
|---|---|
| `match` | Same games. |
| `tie` | Different games, equally optimal on **upstream's own weights** (COP logs every pair's weight; Baxter's pairing is totalled on that table). |
| `WORSE` | Heavier than upstream's choice on upstream's weights; the gap is shown. |
| `BARRED` | Uses a pair upstream excludes outright; the log's constraint code is shown (`GB` gibsonized, `CB` forced bye, `F3` Factor 3, `PP` prepaired, `KH` class KOTH, `TB` top-down bye, …). |
| `PIN` | Baxter broke a game already paired in the request — a director's fixed pairing. |
| `CONVERT` | The converted standings disagree with the ones COP logged: a converter bug, so nothing else in the case means anything. Checked on every run. |
| `both refuse` / `error` | One or both engines declined to pair. |

Lower case with `*` means the case is **not comparable**: clock-bound, or it asks
for something Baxter cannot express (class prizes, top-down byes, …).

### Findings at the pin (2026-09-21)

The converter reproduces upstream's standings on every fixture. Of the 26:
1 match, 1 both-refuse, and **no comparable case agrees**:

- **`PIN` ×3 — a real Baxter bug.** The port enforces a fixed pairing only as a
  weight (`PROHIBITIVE_WEIGHT` on the pinned player's other pairs, in
  `build_weight_edges`), and the pinned game can carry prohibitive weights of
  its own, so the matching can find it cheaper to break the pin. Upstream now
  excludes those pairs outright (`PP`). A director's fixed pairing in a COP
  round can therefore be silently ignored.
- **Round one.** With no results, upstream pairs adjacent seeds (1–2, 3–4);
  the port uses COP.pm's Swiss-style split (1–5, 2–6). Upstream also breaks
  equal records by *descending* player index.
- **Upstream logic the port lacks**: Factor 3 control loss (`F3`), stricter
  gibson bars (`GB`, `GG`), forced-bye handling (`CB`), and cash-contender
  penalties the port does not apply — the ~1.5 billion `WORSE` gaps are those
  weights.
