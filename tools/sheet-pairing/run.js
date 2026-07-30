#!/usr/bin/env node
// Run the spreadsheet's own pairing code against an exported input file.
//
// The point is to get ground truth: this drives the real Code.ts algorithms
// (see sheet_pairing.js, generated verbatim by build.py) rather than a
// reimplementation, so what it prints is what the sheet would have produced.
//
//   node tools/sheet-pairing/run.js inputs.js                 # every round
//   node tools/sheet-pairing/run.js inputs.js --round 10      # one round
//   node tools/sheet-pairing/run.js inputs.js --through 9 --round 10 --trace
//   node tools/sheet-pairing/run.js inputs.js --json
//
// A round that already has all its results is reported straight from those
// results, not re-paired. To watch the algorithm actually pair round N, drop the
// results from round N onwards with --through (N-1).
//
// --trace lets the pairing code's own console.log through. That is the whole
// reason this exists: the Swiss helper narrates its score groups, promotions and
// repeat-limit escalations, which is the only direct view of why it chose a
// pairing.

const path = require("path");
const sheet = require("./sheet_pairing.js");

function parseArgs(argv) {
  const args = {
    file: null,
    round: null,
    trace: false,
    json: false,
    standings: null,
    through: null,
    weights: null,
    distances: null,
    activeAt: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--round") args.round = parseInt(argv[++i], 10);
    else if (a === "--standings") args.standings = parseInt(argv[++i], 10);
    else if (a === "--through") args.through = parseInt(argv[++i], 10);
    else if (a === "--active-at") args.activeAt = parseInt(argv[++i], 10);
    else if (a === "--sweep-weights") args.weights = argv[++i].split(",").map(Number);
    else if (a === "--sweep-distances") args.distances = argv[++i].split(",").map(Number);
    else if (a === "--trace") args.trace = true;
    else if (a === "--json") args.json = true;
    else if (a === "--help" || a === "-h") args.help = true;
    else if (!args.file) args.file = a;
    else throw new Error(`unexpected argument: ${a}`);
  }
  return args;
}

function usage() {
  console.log(
    [
      "usage: node run.js <inputs.js> [--through N] [--round N] [--standings N]",
      "                    [--trace] [--json]",
      "",
      "  <inputs.js>     produced by export_inputs.py from a tournament workbook",
      "  --through N     keep only results up to round N, so later rounds are",
      "                  really paired instead of read back from their results",
      "  --round N       show only round N",
      "  --active-at N   drop entrants who had already withdrawn by round N (no",
      "                  result in round N or later), reconstructing that round's",
      "                  field — the Entrants tab only records the final one",
      "  --sweep-weights a,b,c    with --sweep-distances and --round, re-pair that",
      "  --sweep-distances a,b,c  round once per settings combination and emit JSON",
      "  --standings N   print the standings after round N and exit",
      "  --trace         let the pairing code's own console.log output through",
      "  --json          emit JSON instead of a table",
    ].join("\n")
  );
}

// The pairing code logs heavily. Swallow it unless --trace, so normal runs are
// readable, and restore the real console for our own output.
function withLogging(trace, fn) {
  const real = console.log;
  if (!trace) console.log = () => {};
  try {
    return fn();
  } finally {
    console.log = real;
  }
}

// Entrants who had withdrawn by `round`: no result in that round or any later
// one. The sheet has no withdrawal concept — a director deletes the row — so the
// Entrants tab describes only the final field. Reconstructing a mid-tournament
// round means putting back everyone still playing then and removing everyone who
// had already gone. Uses the full results, not the truncated ones, since who
// withdrew is a fact about the whole event.
function activeEntrants(entrants, allResults, round) {
  const lastPlayed = new Map();
  for (const row of allResults) {
    const r = parseInt(row[0], 10);
    if (isNaN(r)) continue;
    for (const name of [row[1], row[3]]) {
      if (name && name !== "Bye") {
        lastPlayed.set(name, Math.max(lastPlayed.get(name) || 0, r));
      }
    }
  }
  const isBye = (row) => String(row[0]).trim().toLowerCase() === "bye";
  const active = entrants.filter((row) => {
    if (isBye(row)) return false;
    const last = lastPlayed.get(row[0]);
    return last === undefined || last >= round;
  });
  // The Bye row is part of the same mutable snapshot: the director adds it when
  // the field is odd and removes it when it is even, so its presence on the tab
  // reflects only the final parity. Put it back exactly when this round needed
  // one. (Word Cup 2022 D1: 39 players and a bye, 38 and none, then 35 and a bye
  // again — which is what its results show.)
  const bye = entrants.find(isBye);
  if (bye && active.length % 2 === 1) active.push(bye);
  return active;
}

function build(inputs, settingsRows) {
  const res = sheet.makeResults(inputs.results);
  const entrants = sheet.makeEntrants(inputs.entrants);
  const fixed = sheet.makeFixedPairings(inputs.fixedPairings);
  entrants.fixed_pairings = fixed.pairings;
  entrants.fixed_starts = fixed.starts;
  entrants.settings = sheet.makeSettings(settingsRows || inputs.settings);
  const roundPairings = sheet.makeRoundPairings(inputs.roundPairings);
  return { res, entrants, roundPairings };
}

// Pair one round once per (swiss_weight, swiss_distance) combination.
//
// Recovering what a round was configured with means re-running the *sheet's own*
// code over the grid and seeing which settings reproduce the pairing it actually
// produced — measuring Baxter against the sheet would confound the settings with
// the differences between the two algorithms. Sweeping inside one process keeps
// this to a single node start-up per round instead of one per grid point.
//
// Emits NDJSON, one line per combination, flushed as it goes, and walks
// swiss_distance from wide to narrow. That matters because the sheet's Swiss
// helper can genuinely hang: a narrow distance filter can make a perfect
// matching impossible, and its retry only ever raises the repeat limit, which
// never unlocks a distance-filtered edge — so `nrep` climbs forever. There is no
// termination guard in Code.ts and adding one here would stop this being the
// real algorithm. Streaming wide-to-narrow means a caller that kills a hung run
// still has every result up to the combination that hung.
function sweep(inputs, round, weights, distances) {
  // Distance outer, widest first: a hang is caused by the distance filter, so
  // this way one hung combination costs only the narrower distances, not every
  // remaining weight as well.
  const widest = [...distances].sort((a, b) => b - a);
  for (const swiss_distance of widest) {
    for (const swiss_weight of weights) {
      const rows = [
        ["swiss_weight", swiss_weight],
        ["swiss_distance", swiss_distance],
      ];
      const { res, entrants, roundPairings } = build(inputs, rows);
      const starts = new sheet.Starts(res, entrants);
      const all = sheet.runPairings(res, entrants, roundPairings, starts, new sheet.Diags());
      const pairings = all[round - 1] || [];
      process.stdout.write(
        JSON.stringify({
          swiss_weight,
          swiss_distance,
          pairings: pairings.map((p) => [p.first.name, p.second.name]),
        }) + "\n"
      );
    }
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.file) {
    usage();
    process.exit(args.file ? 0 : 1);
  }
  const loaded = require(path.resolve(args.file));
  // Truncating the results is what turns a played round back into one the
  // pairing code has to solve; makeResults keys everything off this array.
  let inputs =
    args.through === null
      ? loaded
      : { ...loaded, results: loaded.results.filter((r) => r[0] <= args.through) };
  if (args.activeAt !== null) {
    inputs = {
      ...inputs,
      entrants: activeEntrants(loaded.entrants, loaded.results, args.activeAt),
    };
  }
  if (args.weights && args.distances) {
    if (args.round === null) throw new Error("--sweep-* requires --round");
    withLogging(args.trace, () => sweep(inputs, args.round, args.weights, args.distances));
    return;
  }

  const { res, entrants, roundPairings } = withLogging(args.trace, () => build(inputs));

  if (args.standings !== null) {
    const standings = withLogging(args.trace, () =>
      sheet.standingsAfterRound(res, entrants, args.standings)
    );
    if (args.json) {
      console.log(JSON.stringify(standings, null, 2));
      return;
    }
    console.log(`Standings after round ${args.standings} — ${inputs.source}\n`);
    standings.forEach((p, i) => {
      const record = `${p.wins + 0.5 * p.ties}-${p.losses + 0.5 * p.ties}`;
      console.log(
        `${String(i + 1).padStart(3)}  ${p.name.padEnd(20)} ${record.padStart(9)}` +
          `  ${String(p.spread >= 0 ? "+" + p.spread : p.spread).padStart(6)}` +
          `  wins ${p.wins}`
      );
    });
    return;
  }

  const starts = new sheet.Starts(res, entrants);
  const diags = new sheet.Diags();
  const all = withLogging(args.trace, () =>
    sheet.runPairings(res, entrants, roundPairings, starts, diags)
  );

  const rounds = all
    .map((pairings, i) => ({ round: i + 1, pairings }))
    .filter((r) => args.round === null || r.round === args.round);

  if (args.json) {
    console.log(
      JSON.stringify(
        rounds.map((r) => ({
          round: r.round,
          type: roundPairings[r.round] ? roundPairings[r.round].type : null,
          status: diags.round_status[r.round],
          pairings: r.pairings.map((p) => ({
            first: p.first.name,
            second: p.second.name,
            repeats: p.repeats,
          })),
        })),
        null,
        2
      )
    );
    return;
  }

  console.log(`Sheet pairings — ${inputs.source}\n`);
  for (const { round, pairings } of rounds) {
    const rp = roundPairings[round];
    const status = diags.round_status[round] || "";
    console.log(
      `ROUND ${round}  ${rp ? rp.type : "?"}` +
        `${rp ? `  (off round ${rp.start})` : ""}  ${status}`
    );
    pairings.forEach((p, i) => {
      const rep = p.repeats > 1 ? `  rep ${p.repeats}` : "";
      console.log(
        `  ${String(i + 1).padStart(2)}  ${p.first.name.padEnd(20)}` +
          ` v ${p.second.name.padEnd(20)}${rep}`
      );
    });
    console.log("");
  }
}

main();
