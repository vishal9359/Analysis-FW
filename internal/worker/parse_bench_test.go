package worker_test

import (
	"os"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/fixture"
	"github.com/vishal9359/Analysis-FW/internal/reader"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/testutil"
)

// BenchmarkParseOnly isolates dynamicpb unmarshal from row building.
func BenchmarkParseOnly(b *testing.B) {
	dir, _ := os.MkdirTemp("", "afw-pbench-")
	defer os.RemoveAll(dir)
	runDir, _, err := fixture.Generate(testutil.ProtoDir(), dir, map[string]fixture.Spec{
		testutil.Block1: {Count: 50000, Splits: 1},
		testutil.Block2: {Count: 1, Splits: 1},
		testutil.NVMe:   {Count: 1, Splits: 1},
	})
	if err != nil {
		b.Fatal(err)
	}
	units, _ := discover.Discover(runDir)
	var u discover.Unit
	for _, x := range units {
		if x.Stem == testutil.Block1 {
			u = x
		}
	}
	schema, err := registry.Build(u.Stem, u.ProtoPath)
	if err != nil {
		b.Fatal(err)
	}

	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		list, err := reader.Records(u.PBParts[0], schema)
		if err != nil {
			b.Fatal(err)
		}
		if list.Len() != 50000 {
			b.Fatalf("got %d", list.Len())
		}
	}
}
