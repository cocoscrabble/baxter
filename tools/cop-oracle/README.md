# COP parity oracle

Drives the **real, unmodified** `TSH::Command::COP` (Matthew O'Connor's COP
algorithm, from `jvc56/tournament_pairing_algorithms`) as a reference
implementation for the Rust port in `scrabble-pairing`. Feed it a test case as
JSON, get back COP's pairings and key decisions as JSON, and compare against the
Rust `COP` strategy.

Everything the oracle needs lives under `vendor/`, which is **gitignored** — the
oracle is a local-only development aid for building and hardening the Rust COP
code on this machine. Only `tools/cop-oracle/` (this harness, the stubs, and
this doc) is tracked.

## One-time setup on a dev machine

```bash
# 1. Vendor tsh (the user placed it at vendor/tsh).
# 2. Install the real COP.pm per its README, into the vendored tsh:
cp <path-to>/COP.pm vendor/tsh/lib/perl/TSH/Command/COP.pm
# 3. Install COP's native-path Perl dep into a vendored local-lib:
cpanm --local-lib=vendor/perl5 --notest Graph::Matching   # pulls Carp::Assert
```

`JSON::PP` and `threads` ship with core Perl. Verify it loads:

```bash
perl -Itools/cop-oracle/stubs -Ivendor/tsh/lib/perl -Ivendor/perl5/lib/perl5 \
     -e 'require TSH::Command::COP; print "ok\n"'
```

### Why the stubs

`COP.pm` `use`s `TSH::PairingCommand` and `TSH::Command::ShowPairings`, and the
vendored `TSH::PairingCommand` uses syntax a modern Perl (5.42) rejects. COP's
`cop()` computation never touches those modules — only its `Run()` method (which
the oracle does not call) does. `tools/cop-oracle/stubs/` holds empty
same-named packages; putting that dir first on `@INC` shadows the stale real
modules so the **unmodified** `COP.pm` loads. Nothing in `COP.pm` is edited.

## Usage

```bash
perl -Itools/cop-oracle/stubs -Ivendor/tsh/lib/perl -Ivendor/perl5/lib/perl5 \
     tools/cop-oracle/oracle.pl < case.json
```

See `oracle.pl`'s header for the full input/output schema and
`example_case.json` for a runnable example. Key points:

- **Wins are doubled** in the input (a win = 2, a draw = 1, a loss = 0), matching
  COP's internal integer representation and what the Rust port must feed in.
- Players are listed in current standings order; `times_played`/`previous_pairings`
  are keyed by player id, with the **bye as id 0**.
- Config takes the **raw** per-round arrays (as in TSH config); the harness
  forward-fills them to the round count exactly like COP's `Run()`, mirroring the
  scalar→array expansion the Rust adapter does.
- Runs **single-threaded** with `srand(seed)`, so a given case is reproducible
  run-to-run.

Output: `pairings` (`[id, opponent_id]`, opponent `0` = bye), `warnings`
(pairings COP was forced past the prohibitive weight), `gibson_rank`, and
`sim_player_ids` (the can-cash truncation).

## What parity can and cannot mean here

The oracle is authoritative for COP's **logic**, but bit-for-bit identical
*pairings* from the Rust port are not the right bar, for two reasons:

1. **Different RNG.** COP uses Perl's `rand`; the Rust engine uses seeded
   ChaCha8. The Monte Carlo draws differ, so the simulated contender sets can
   differ near a boundary even though both are correct.
2. **Different matching solver.** COP uses Perl `Graph::Matching`; Rust uses the
   vendored rustworkx-core blossom. On a weight graph with tied optima they may
   pick different (equally optimal) matchings.

So parity is layered (see `plans/PLAN_COP.md`):

- **Tier 1 — exact**, on the RNG-independent logic: `gibson_rank`,
  `sim_player_ids`, and (once the harness exposes it) the integer **weight
  matrix** given injected contender/gibson/control-loss values. This is where
  most of COP's decision logic lives and where the Rust port must match exactly.
- **Tier 2 — statistical**, on the simulation layer: with large sim counts, the
  contender sets / gibson / control-loss decisions must agree on constructed
  cases whose boundaries are clear.
- **Tier 3 — matching**, on final pairings: exact only for cases with a strictly
  unique optimal matching (well-separated weights, or the shared per-edge
  tiebreak); otherwise assert equal total weight, not identical edges.
