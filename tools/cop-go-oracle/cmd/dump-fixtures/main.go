// Command dump-fixtures writes upstream's own COP test positions — the real
// tournaments in liwords' pkg/pair/testutils — as PairRequest JSON files, one
// per fixture, so the oracle and Baxter's port can be run on the same cases.
//
//	go run ./cmd/dump-fixtures -out fixtures/
//
// The list is upstream's, by name; when the pin moves, new fixtures are added
// here by hand (a missing one is a compile error, not a silent gap).
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/woogles-io/liwords/pkg/pair/testutils"
	pb "github.com/woogles-io/liwords/rpc/api/proto/ipc"
)

var fixtures = map[string]func() *pb.PairRequest{
	"CreateDefaultPairRequest":                                               testutils.CreateDefaultPairRequest,
	"CreateDefaultOddPairRequest":                                            testutils.CreateDefaultOddPairRequest,
	"CreateKingston2023AfterRound15PairRequest":                              testutils.CreateKingston2023AfterRound15PairRequest,
	"CreateAlbanyjuly4th2024AfterRound21PairRequest":                         testutils.CreateAlbanyjuly4th2024AfterRound21PairRequest,
	"CreateLakeGeorgeAfterRound13PairRequest":                                testutils.CreateLakeGeorgeAfterRound13PairRequest,
	"CreateAlbanyCSWAfterRound24PairRequest":                                 testutils.CreateAlbanyCSWAfterRound24PairRequest,
	"CreateAlbanyCSWAfterRound24OddPairRequest":                              testutils.CreateAlbanyCSWAfterRound24OddPairRequest,
	"CreateAlbany3rdGibsonizedAfterRound25PairRequest":                       testutils.CreateAlbany3rdGibsonizedAfterRound25PairRequest,
	"CreateAlbany4thGibsonizedAfterRound25PairRequest":                       testutils.CreateAlbany4thGibsonizedAfterRound25PairRequest,
	"CreateAlbany1stAnd4thGibsonizedAfterRound25PairRequest":                 testutils.CreateAlbany1stAnd4thGibsonizedAfterRound25PairRequest,
	"CreateAlbany1stAnd4thAnd8thGibsonizedAfterRound25PairRequest":           testutils.CreateAlbany1stAnd4thAnd8thGibsonizedAfterRound25PairRequest,
	"CreateLakegeorgeCSWAfterRound8PairRequest":                              testutils.CreateLakegeorgeCSWAfterRound8PairRequest,
	"CreateBellevilleCSWAfterRound12PairRequest":                             testutils.CreateBellevilleCSWAfterRound12PairRequest,
	"CreateBellevilleCSW4thCLAfterRound12PairRequest":                        testutils.CreateBellevilleCSW4thCLAfterRound12PairRequest,
	"CreateAlbanyAfterRound15PairRequest":                                    testutils.CreateAlbanyAfterRound15PairRequest,
	"CreateAlbanyAfterRound16PairRequest":                                    testutils.CreateAlbanyAfterRound16PairRequest,
	"CreateAlbanyCSWNewYearsAfterRound27PairRequest":                         testutils.CreateAlbanyCSWNewYearsAfterRound27PairRequest,
	"CreateAlbanyCSWNewYearsAfterRound27LastRoundPartiallyPairedPairRequest": testutils.CreateAlbanyCSWNewYearsAfterRound27LastRoundPartiallyPairedPairRequest,
	"CreateAlbanyCSWNewYearsRound25PartiallyPairedPairRequest":               testutils.CreateAlbanyCSWNewYearsRound25PartiallyPairedPairRequest,
	"CreateAlmostGibsonizedPairRequest":                                      testutils.CreateAlmostGibsonizedPairRequest,
	"CreateBLSRound32PairRequest":                                            testutils.CreateBLSRound32PairRequest,
	"CreateLG2025Round15PairRequest":                                         testutils.CreateLG2025Round15PairRequest,
	"CreateManhattanAfterRound14PairRequest":                                 testutils.CreateManhattanAfterRound14PairRequest,
	"CreateAlbanyCSWJuly2026Round28PairRequest":                              testutils.CreateAlbanyCSWJuly2026Round28PairRequest,
	"CreateHypothetical12p7rRound7PairRequest":                               testutils.CreateHypothetical12p7rRound7PairRequest,
	"CreateJuly4th2026RandomStartRun305AfterRound24PairRequest":              testutils.CreateJuly4th2026RandomStartRun305AfterRound24PairRequest,
}

var (
	wordStart    = regexp.MustCompile(`([a-z0-9])([A-Z])`)
	acronymStart = regexp.MustCompile(`([A-Z]+)([A-Z][a-z])`)
)

// fileName turns CreateAlbanyCSWAfterRound24PairRequest into
// albany_csw_after_round24.json.
func fileName(fn string) string {
	name := strings.TrimSuffix(strings.TrimPrefix(fn, "Create"), "PairRequest")
	name = acronymStart.ReplaceAllString(name, "${1}_${2}")
	name = wordStart.ReplaceAllString(name, "${1}_${2}")
	return strings.ToLower(name) + ".json"
}

func main() {
	out := flag.String("out", "fixtures", "directory to write the JSON requests to")
	flag.Parse()
	if err := os.MkdirAll(*out, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	marshal := protojson.MarshalOptions{Multiline: true, Indent: "  "}
	for fn, create := range fixtures {
		encoded, err := marshal.Marshal(create())
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", fn, err)
			os.Exit(1)
		}
		path := filepath.Join(*out, fileName(fn))
		if err := os.WriteFile(path, append(encoded, '\n'), 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	fmt.Printf("wrote %d fixtures to %s\n", len(fixtures), *out)
}
