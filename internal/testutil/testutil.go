// Package testutil provides the shared fixture used across test packages.
//
// The fixture is generated once per test binary from the bundled .proto files,
// so tests can never drift from what the loader expects.
package testutil

import (
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/config"
	"github.com/vishal9359/Analysis-FW/internal/fixture"
)

// Stems of the units in the fixture.
const (
	Block1 = "linux_block_1_stats"
	Block2 = "linux_block_2_misc"
	NVMe   = "linux_nvme_1_stats"
)

var (
	once   sync.Once
	runDir string
	genErr error
)

// RepoRoot is the module root, resolved from this file's compile-time path so
// tests work regardless of the working directory.
func RepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(filepath.Dir(filepath.Dir(file))) // internal/testutil/x.go -> root
}

// ProtoDir is the bundled sample .proto directory.
func ProtoDir() string { return filepath.Join(RepoRoot(), "testdata", "protos") }

func buildOnce() (string, error) {
	once.Do(func() {
		var dir string
		dir, genErr = os.MkdirTemp("", "afw-fixture-")
		if genErr != nil {
			return
		}
		runDir, _, genErr = fixture.Generate(ProtoDir(), dir, nil)
	})
	return runDir, genErr
}

// RunDir generates (once) and returns the fixture run directory.
func RunDir(t *testing.T) string {
	t.Helper()
	dir, err := buildOnce()
	if err != nil {
		t.Fatalf("cannot build fixture: %v", err)
	}
	return dir
}

// RunDirB is RunDir for benchmarks.
func RunDirB(b *testing.B) string {
	b.Helper()
	dir, err := buildOnce()
	if err != nil {
		b.Fatalf("cannot build fixture: %v", err)
	}
	return dir
}

// Config is a test config: sequential by default, small batches, memory store.
func Config(workers, batch int) *config.Config {
	return &config.Config{
		ProducerName: "profile_fw",
		Store: config.StoreConfig{
			Kind: "memory", Host: "localhost", Port: 8123,
			Database: "profile_fw", BatchSize: batch, Workers: workers,
			Options: map[string]interface{}{},
		},
		LogLevel: "INFO", LogFormat: "text",
	}
}
