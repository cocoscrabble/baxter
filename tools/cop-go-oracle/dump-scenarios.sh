#!/usr/bin/env bash
# Capture every PairRequest upstream's own COP tests pair, as fixtures.
#
#   tools/cop-go-oracle/dump-scenarios.sh
#
# The shipped fixtures (cmd/dump-fixtures) are real tournaments, which leave
# some of COP's rules unexercised. Upstream's tests (pkg/pair/cop's
# cop_test.go and scenarios_test.go) construct positions aimed at exactly
# those rules, in Go code rather than as data, and cop_test.go is white-box, so
# it only runs inside liwords' own tree. So: copy the pinned module's source
# out of the module cache into a scratch directory, add one line to COPPair
# that writes each request to disk before pairing it, run the tests there
# (COP_SCENARIOS=1 enables the scenario suite), and keep the distinct COP
# requests. The oracle itself is never modified.
#
# Skipped: upstream's profiling/timing loops and random-comparison runs
# (thousands of near-identical 53-player requests, over an hour) and the
# non-COP methods' tests. Tests that call COP's internals directly never reach
# COPPair, so they are not captured; the scenario suite covers those rules.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/fixtures/scenarios"
cd "$here"
version="$(go list -m -f '{{.Version}}' github.com/woogles-io/liwords)"
src="$(go env GOMODCACHE)/github.com/woogles-io/liwords@$version"
scratch="$(mktemp -d)"
trap 'chmod -R u+w "$scratch"; rm -rf "$scratch"' EXIT
cp -r "$src/." "$scratch/"
chmod -R u+w "$scratch"
dump="$scratch/.dump"
mkdir -p "$dump"

# The hook: write the request (before COPPair derives a seed into it) to
# $COP_DUMP_DIR, named by its content hash so repeats collapse.
python3 - "$scratch/pkg/pair/cop/cop.go" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
hook = '''func COPPair(req *pb.PairRequest) *pb.PairResponse {
	if dir := os.Getenv("COP_DUMP_DIR"); dir != "" {
		if b, err := protojson.Marshal(req); err == nil {
			h := fnv.New64a()
			h.Write(b)
			_ = os.WriteFile(fmt.Sprintf("%s/%016x.json", dir, h.Sum64()), b, 0o644)
		}
	}
'''
old = "func COPPair(req *pb.PairRequest) *pb.PairResponse {\n"
assert old in s
s = s.replace(old, hook, 1).replace('import (\n', 'import (\n\t"os"\n', 1)
open(p, "w").write(s)
PY

(cd "$scratch" && GOFLAGS=-mod=mod COP_SCENARIOS=1 COP_DUMP_DIR="$dump" \
    go test ./pkg/pair/cop/ -count=1 -timeout 60m \
    -skip 'TestCOPProf|TestCOPTime|TestCOPDebug|TestCompare|TestRandom|TestRoundRobin|TestKingOfTheHill|TestFactor$|TestInitialFontes|TestSwiss|TestTeamRoundRobin|TestInterleavedRoundRobin|TestMultiroundPairings' \
    >/dev/null) || true

mkdir -p "$out"
rm -f "$out"/*.json
# COP only: protobuf JSON omits the default method, which is COP.
for f in "$dump"/*.json; do
    if python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("pairMethod", "COP") != "COP")' "$f"; then
        python3 -m json.tool "$f" > "$out/$(basename "$f")"
    fi
done
# One of upstream's tests loops over ~1000 random 53-player positions; keep an
# even sample of 20 so the corpus stays reviewable.
python3 - "$out" <<'PY'
import glob, json, os, sys
big = sorted(f for f in glob.glob(f"{sys.argv[1]}/*.json")
             if json.load(open(f)).get("allPlayers") == 53)
keep = set(big[::max(1, len(big) // 20)][:20])
for f in big:
    if f not in keep:
        os.remove(f)
PY
echo "captured $(ls "$out" | wc -l) distinct requests into $out"
