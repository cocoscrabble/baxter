// Command cop-go-oracle runs upstream COP pairings, unmodified, for Baxter to
// compare its Rust port against.
//
// Upstream is liwords' Go port of COP (github.com/woogles-io/liwords,
// pkg/pair/cop), which is now COP's source of truth; the pinned commit is in
// go.mod. This does what liwords' own cop-lambda does with a request —
// cop.COPPair — minus AWS: a PairRequest as protobuf JSON on stdin, and on
// stdout
//
//	{"response": <PairResponse as protobuf JSON>, "oracle": {...}}
//
// The response's `log` is COP's own account of its decisions (contender
// groups, gibsonization, control loss, the weight table), which is what makes
// this useful for more than comparing final pairings. `oracle` is what a
// comparison needs to know about the run itself; see runInfo.
//
//	go run . < request.json
//	go run . -log=false < request.json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"regexp"
	"runtime/debug"
	"strconv"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/woogles-io/liwords/pkg/pair/cop"
	pkgstnd "github.com/woogles-io/liwords/pkg/pair/standings"
	pb "github.com/woogles-io/liwords/rpc/api/proto/ipc"
)

// simTimeLimit is upstream's per-phase simulation budget (the unexported
// initialSimTimeLimit / reSimTimeLimit / controlSimTimeLimit in
// pkg/pair/standings, and the Factor 3 check in pkg/pair/cop). Check it when
// the pin moves.
const simTimeLimit = 6 * time.Second

// runInfo is what a comparison needs to know about the run, beyond COP's answer.
type runInfo struct {
	// The liwords module version this binary was built against.
	Upstream string  `json:"upstream"`
	Workers  int     `json:"workers"`
	Elapsed  float64 `json:"elapsedSeconds"`
	// "Total Sims" from COP's log: the initial sims plus however many re-sim
	// batches ran. Absent when COP did not simulate (e.g. an error, or a round
	// paired by a simple method).
	TotalSims *int `json:"totalSims,omitempty"`
	// True when the run took at least one simulation budget. COP re-simulates
	// until a hopefulness threshold is met *or its clock runs out*, and says
	// nothing when it is the clock; then the sim count, and near a decision
	// boundary the pairings, depend on the machine's speed. Such a run is not
	// reproducible and should be compared statistically, not exactly.
	MaybeTimeLimited bool `json:"maybeTimeLimited"`
}

type output struct {
	Response json.RawMessage `json:"response"`
	Oracle   runInfo         `json:"oracle"`
}

var totalSims = regexp.MustCompile(`(?m)^Total Sims: (\d+)$`)

func main() {
	withLog := flag.Bool("log", true, "include COP's decision log in the response")
	workers := flag.Int("workers", 4, "simulation goroutines; fixed so results do not depend on the machine's CPU count")
	flag.Parse()

	if err := run(os.Stdin, os.Stdout, *withLog, *workers); err != nil {
		fmt.Fprintln(os.Stderr, "cop-go-oracle:", err)
		os.Exit(1)
	}
}

func run(in io.Reader, out io.Writer, withLog bool, workers int) error {
	// Each worker draws its own RNG stream from COP's, so the worker count is
	// part of the input: left to runtime.NumCPU() the same request pairs
	// differently on different machines. Upstream exposes this for exactly
	// that reason (its tests set it too).
	pkgstnd.NumSimWorkersOverride = workers

	raw, err := io.ReadAll(in)
	if err != nil {
		return fmt.Errorf("reading request: %w", err)
	}
	var req pb.PairRequest
	// Strict: a misspelled field would otherwise be dropped silently and COP
	// would run on its zero value — a wrong answer that looks like a right one.
	if err := protojson.Unmarshal(raw, &req); err != nil {
		return fmt.Errorf("parsing PairRequest: %w", err)
	}

	start := time.Now()
	resp := cop.COPPair(&req)
	elapsed := time.Since(start)

	info := runInfo{
		Upstream:         upstreamVersion(),
		Workers:          workers,
		Elapsed:          elapsed.Seconds(),
		MaybeTimeLimited: elapsed >= simTimeLimit,
	}
	if m := totalSims.FindStringSubmatch(resp.Log); m != nil {
		n, _ := strconv.Atoi(m[1])
		info.TotalSims = &n
	}
	if !withLog {
		resp.Log = ""
	}

	encoded, err := protojson.MarshalOptions{
		// Zero values matter here: pairing index 0 and error_code SUCCESS are
		// both zero, and omitting them makes the output ambiguous to read.
		EmitUnpopulated: true,
	}.Marshal(resp)
	if err != nil {
		return fmt.Errorf("encoding PairResponse: %w", err)
	}
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	return enc.Encode(output{Response: encoded, Oracle: info})
}

func upstreamVersion() string {
	bi, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, dep := range bi.Deps {
		if dep.Path == "github.com/woogles-io/liwords" {
			return dep.Version
		}
	}
	return "unknown"
}
