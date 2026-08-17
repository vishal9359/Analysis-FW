// Command mkfixture generates a ProfileData-* run of realistic per-second
// time-series data, so the pipeline and the metric queries can be exercised
// without waiting for real profiling.
//
// Usage:
//
//	mkfixture [-protos DIR] [-count N] <output_root>
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/vishal9359/Analysis-FW/internal/fixture"
)

func main() {
	protos := flag.String("protos", "testdata/protos", "directory of .proto files")
	count := flag.Int("count", 0, "records per unit (0 = built-in per-unit defaults)")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr,
			"mkfixture — generate a ProfileData-* run of simulated profiling data.\n\n"+
				"Usage:\n  mkfixture [flags] <output_root>\n\nFlags:\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(2)
	}

	var counts map[string]fixture.Spec
	if *count > 0 {
		counts = map[string]fixture.Spec{}
		// applied per unit via DefaultSpec below
		fixture.DefaultSpec = fixture.Spec{Count: *count, Splits: 1}
	}

	runDir, results, err := fixture.Generate(*protos, flag.Arg(0), counts)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}

	total := 0
	for _, r := range results {
		total += r.Records
		fmt.Printf("  %s/%s: %d records, %d file(s), %d payload fields\n",
			r.Dir, r.Stem, r.Records, r.Files, r.PayloadFields)
	}
	fmt.Printf("\n%d records total -> %s\n", total, runDir)
}
