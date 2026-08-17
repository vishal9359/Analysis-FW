package worker_test

import (
	"context"
	"os"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/fixture"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/store/memory"
	"github.com/vishal9359/Analysis-FW/internal/testutil"
	"github.com/vishal9359/Analysis-FW/internal/worker"
)

// BenchmarkLoadUnit measures the CPU-bound path — protobuf decode plus row
// flattening — with an in-memory store, so no database or network is in the
// measurement. This is the number a language change actually moves.
func BenchmarkLoadUnit(b *testing.B) {
	const records = 50000

	dir, err := os.MkdirTemp("", "afw-bench-")
	if err != nil {
		b.Fatal(err)
	}
	defer os.RemoveAll(dir)

	runDir, _, err := fixture.Generate(testutil.ProtoDir(), dir, map[string]fixture.Spec{
		testutil.Block1: {Count: records, Splits: 1},
		testutil.Block2: {Count: 1, Splits: 1},
		testutil.NVMe:   {Count: 1, Splits: 1},
	})
	if err != nil {
		b.Fatal(err)
	}

	units, err := discover.Discover(runDir)
	if err != nil {
		b.Fatal(err)
	}
	var unit discover.Unit
	for _, u := range units {
		if u.Stem == testutil.Block1 {
			unit = u
		}
	}
	schema, err := registry.Build(unit.Stem, unit.ProtoPath)
	if err != nil {
		b.Fatal(err)
	}

	ctx := context.Background()
	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		st := memory.New()
		st.Connect(ctx)
		res, err := worker.LoadUnit(ctx, unit.PBParts, schema, st, "bench", 100000)
		if err != nil {
			b.Fatal(err)
		}
		if res.Records != records {
			b.Fatalf("read %d records, want %d", res.Records, records)
		}
	}
	b.SetBytes(int64(records)) // so ns/op converts to records/s
}

// BenchmarkSchemaBuild measures the fixed per-unit cost of compiling a .proto
// at runtime — pure Go, no protoc process.
func BenchmarkSchemaBuild(b *testing.B) {
	units, err := discover.Discover(testutil.RunDirB(b))
	if err != nil {
		b.Fatal(err)
	}
	var proto string
	for _, u := range units {
		if u.Stem == testutil.Block1 {
			proto = u.ProtoPath
		}
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := registry.Build(testutil.Block1, proto); err != nil {
			b.Fatal(err)
		}
	}
}
