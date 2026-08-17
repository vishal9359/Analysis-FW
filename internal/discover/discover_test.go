package discover_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/fwerr"
)

// writeRun builds a minimal run tree: files is name -> contents, relative to a
// Linux/Block layer directory.
func writeRun(t *testing.T, files map[string]string) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "ProfileData-x-1")
	dir := filepath.Join(root, "Linux", "Block")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "Config"), 0o755); err != nil {
		t.Fatal(err)
	}
	// Config/ must be ignored, so drop a decoy .pb in it
	if err := os.WriteFile(filepath.Join(root, "Config", "run_config.pb"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestSplitPartsOrderedAndConfigIgnored(t *testing.T) {
	root := writeRun(t, map[string]string{
		"blk.proto":  "x",
		"blk.001.pb": "b",
		"blk.000.pb": "a",
		"blk.002.pb": "c",
	})
	units, err := discover.Discover(root)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if len(units) != 1 {
		t.Fatalf("units = %d, want 1 (Config/ must be ignored)", len(units))
	}
	u := units[0]
	if u.Stem != "blk" {
		t.Errorf("stem = %s", u.Stem)
	}
	if u.LayerDir != "Linux/Block" {
		t.Errorf("layer dir = %s, want Linux/Block", u.LayerDir)
	}
	var got []string
	for _, p := range u.PBParts {
		got = append(got, filepath.Base(p))
	}
	want := "blk.000.pb,blk.001.pb,blk.002.pb"
	if strings.Join(got, ",") != want {
		t.Errorf("parts = %v, want %s", got, want)
	}
}

// TestMissingSplitPartIsAnError is an integrity guard: a gap in the split run
// means a part never arrived, and loading the rest would silently store less
// data than the producer wrote.
func TestMissingSplitPartIsAnError(t *testing.T) {
	root := writeRun(t, map[string]string{
		"blk.proto":  "x",
		"blk.000.pb": "a",
		"blk.002.pb": "c", // 001 is missing
	})
	_, err := discover.Discover(root)
	if err == nil {
		t.Fatal("a missing split part must be an error")
	}
	if !strings.Contains(err.Error(), "not contiguous") {
		t.Errorf("error should name the gap, got: %v", err)
	}
	if code := fwerr.ExitCode(err); code != fwerr.ExitInput {
		t.Errorf("exit code = %d, want %d (input)", code, fwerr.ExitInput)
	}
}

// TestDataWithoutSchemaIsAnError: a .pb with no .proto beside it cannot be
// decoded, and silently skipping it would lose a whole layer.
func TestDataWithoutSchemaIsAnError(t *testing.T) {
	root := writeRun(t, map[string]string{"blk.pb": "a"}) // no blk.proto
	_, err := discover.Discover(root)
	if err == nil {
		t.Fatal("data with no schema must be an error")
	}
	if !strings.Contains(err.Error(), "no schema") {
		t.Errorf("error should say the schema is missing, got: %v", err)
	}
}

func TestNoDataFilesIsAnError(t *testing.T) {
	root := writeRun(t, map[string]string{"blk.proto": "x"}) // schema only
	_, err := discover.Discover(root)
	if err == nil {
		t.Fatal("a run with no .pb files must be an error")
	}
	if !strings.Contains(err.Error(), "no .pb data files") {
		t.Errorf("unhelpful error: %v", err)
	}
}

func TestNotADirectory(t *testing.T) {
	_, err := discover.Discover(filepath.Join(t.TempDir(), "nope"))
	if err == nil {
		t.Fatal("a missing input path must be an error")
	}
	if code := fwerr.ExitCode(err); code != fwerr.ExitInput {
		t.Errorf("exit code = %d, want %d", code, fwerr.ExitInput)
	}
}
