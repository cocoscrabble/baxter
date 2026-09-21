# PLAN_COP_GO_PORT.md — Re-port COP from liwords' Go implementation

**Status:** planned (2026-09-21). Supersedes the COP.pm-derived port described in
`PLAN_COP.md`, which stays as history.

## Why a re-port

COP's development moved to liwords' Go port
([`woogles-io/liwords` `pkg/pair`](https://github.com/woogles-io/liwords/tree/master/pkg/pair)),
now the source of truth. It has outgrown `COP.pm`: named constraint and weight
policies instead of one weight function, a retry that relaxes contention
windows, Factor 3, top-4 lock, leader-vs-3rd, contender-group parity,
top-down byes, class prizes, re-simulation to a confidence threshold. The
comparison harness (`tools/cop-go-oracle/compare.py`) found no comparable case
where the current port agrees. Modifying `cop.rs` would mean rewriting its core
anyway, so this ports the Go code afresh, keeping Baxter's glue.

Replay no longer constrains this: publishes record their boards
(`PLAN_EVENT_LOG.md`), and no COP division is under way, so the new port
replaces `COP` in place.

## Decisions

1. **Exact parity is the bar, not statistical.** Port upstream's RNG
   (`golang.org/x/exp/rand`'s 128-bit PCG, `Intn`/`Int63`/`Shuffle` exactly) and
   its worker seeding (each sim worker is seeded from `copRand.Int63()` in worker
   order). With the same seed and the same fixed worker count, simulations are
   bit-identical to the oracle, so every stage — sim tallies, precomp data, every
   weight, the final pairings — can be checked *exactly* against the oracle's
   log. Integer tallies summed across workers are order-independent, so running
   workers in parallel threads changes nothing.
2. **Fixed worker count: 4**, matching the oracle's `-workers` default. Part of
   the algorithm, not a tuning knob.
3. **Simulation cap instead of time budgets.** Upstream's re-sim loop runs until
   a Clopper–Pearson threshold is met *or 6s pass*; control-loss sims also carry
   a clock. Baxter needs a replayable answer and a bounded request, so the clock
   becomes a total-sims cap (`max_simulations`), sized by benchmark. Where the
   oracle's run is not clock-bound, the cap is never reached and parity is exact;
   where it is (`maybeTimeLimited`), the two legitimately differ.
4. **Player index = position in `PairingInput.players`.** Upstream breaks exact
   record ties on player index (higher index ranks first). Baxter's order is
   its input order; the converter already writes liwords' index order.
5. **Seed.** A COP round seeds its PCG from `PairingInput.seed` and the round
   (`seed + round − 1`), so rounds differ and replay is stable. The converter
   maps a request's seed (including liwords' derived FNV-of-names default) onto
   that, so parity runs use upstream's exact seed.
6. **Port class prizes and top-down byes too**, as optional config/input. They
   are inert until Baxter supplies them (class prizes still need an in-division
   class model: `PLAN_COP.md` Phase 5), but porting them now keeps the
   upstream-shaped code whole and makes the oracle's class-prize fixtures
   comparable.
7. **Not ported:** upstream's non-COP methods (`simplePair`, `autoPair`) — Baxter
   has its own strategies — and its log text. Instead the port emits a
   structured **trace** (sim tallies, precomp rows, weight table, decisions),
   exposed to Python for the comparison tools only.
8. **Fixed pairings** stay as today's hard pins (upstream's `PP`): pinned players
   sit outside the matching, their games placed as given.

## Layout

`scrabble-pairing/src/strategies/cop/` replaces `cop.rs`:

| Module | Upstream | Contents |
|---|---|---|
| `rand.rs` | `x/exp/rand` | PCG source, `Uint64n`/`Intn`/`Int63`/`Int31n`/`Shuffle` |
| `score_diffs.rs` | `standings/score_differences.go` | the empirical spread table |
| `standings.rs` | `standings/standings.go` | packed records, gibsonization, segment pairings, sims, force-winner sims, Clopper–Pearson |
| `precomp.rs` | `copdata/copdata.go` | `PrecompData`, hopeful/absolute ranks, parity promotion, control loss |
| `policies.rs` | `cop/cop.go` | constraint + weight policies and their precomputations |
| `factor3.rs` | `cop/cop.go` | Factor 3 |
| `matching.rs` | `cop/cop.go` | graph build, min-weight matching, retry, result assembly |
| `mod.rs` | — | Baxter adapter: `PairingInput` → request shape, pins, bye, output |
| `trace.rs` | — | structured trace for parity tooling |

The matcher is the vendored `max_weight_matching` if it agrees with upstream's
(both descend from Van Rantwijk's `mwmatching.py`; verified in stage 4),
otherwise a port of `pkg/matching`.

## Stages — each verified against the oracle before the next

Parity tooling grows with the port: the Go log already prints the stage-1–3
intermediates (sim tallies, precomp table, pairing weights); `compare.py
--stages` parses them and diffs against the Rust trace.

0. **Tooling.** Converter fixes found while reading upstream (a zero bye scores
   as a draw; `factor`/`initialNonperfRounds` only affect non-COP methods), seed
   mapping, the `cop_trace_json` binding, `--stages` scaffolding.
1. **RNG + standings + simulation.** Check: identical "Initial Sim Results" /
   "Improved Factor Sim Results" tallies and `Total Sims` on every
   non-clock-bound fixture.
2. **Precomp.** Check: the "Precomp Data" table (Gb, Gr, H, A, vs1st, vsFactor)
   and the destiny's child, exactly.
3. **Policies + weights.** Check: the "Pairing Weights" table, every constraint
   code and every weight column, exactly.
4. **Factor 3, matching, retry, assembly.** Check: final pairings exactly on
   every comparable fixture; `compare.py` all `match`.
5. **Integration.** Swap `cop.rs` out; map `CopConfig` (+ `max_simulations`,
   `top_down_byes`); benchmark and size the sim cap against the 30s request;
   settings form; docs; retire the COP.pm-era notes in `PLAN_COP.md`.

## Risks

- **Clopper–Pearson floats.** Upstream uses gonum's Beta quantile; a
  last-bit difference could flip a threshold decision and change the sim
  count. Port gonum's algorithm if a generic implementation ever disagrees.
- **Cost.** Upstream burns up to 6s × 8 cores; Baxter pairs inside a web request.
  The cap is the control; stage 5 measures it.
