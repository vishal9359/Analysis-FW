package runner_test

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/runner"
	"github.com/vishal9359/Analysis-FW/internal/store/memory"
	"github.com/vishal9359/Analysis-FW/internal/testutil"
)

// ---- discover -------------------------------------------------------------

func TestDiscoverFindsUnitsAndSplits(t *testing.T) {
	units, err := discover.Discover(testutil.RunDir(t))
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	got := map[string]int{}
	for _, u := range units {
		got[u.Stem] = len(u.PBParts)
	}
	for _, want := range []string{testutil.Block1, testutil.Block2, testutil.NVMe} {
		if _, ok := got[want]; !ok {
			t.Errorf("missing unit %s (have %v)", want, got)
		}
	}
	if got[testutil.Block1] != 2 {
		t.Errorf("%s should have 2 split wrapper files, got %d", testutil.Block1, got[testutil.Block1])
	}
}

// ---- end to end -----------------------------------------------------------

func loadFixture(t *testing.T, workers int) (runner.LoadReport, *memory.Store) {
	t.Helper()
	st := memory.New()
	if err := st.Connect(context.Background()); err != nil {
		t.Fatal(err)
	}
	rep, err := runner.RunLoad(context.Background(), testutil.RunDir(t),
		testutil.Config(workers, 250), st)
	if err != nil {
		t.Fatalf("run load: %v", err)
	}
	return rep, st
}

func TestFullLoadCounts(t *testing.T) {
	rep, st := loadFixture(t, 1)
	if rep.Status != "complete" {
		t.Errorf("status = %s, want complete", rep.Status)
	}
	if got := st.Count(testutil.Block1); got != 1000 { // both split files
		t.Errorf("%s rows = %d, want 1000", testutil.Block1, got)
	}
	if got := st.Count(testutil.Block2); got != 400 {
		t.Errorf("%s rows = %d, want 400", testutil.Block2, got)
	}
	if got := st.Count(testutil.NVMe); got != 300 { // main = 1 per record
		t.Errorf("%s rows = %d, want 300", testutil.NVMe, got)
	}
}

// TestParallelMatchesSequential: the goroutine pool must produce exactly the
// same rows as the sequential path (this replaced the Python process pool).
func TestParallelMatchesSequential(t *testing.T) {
	seq, seqStore := loadFixture(t, 1)
	par, parStore := loadFixture(t, 4)
	if seq.Records() != par.Records() || seq.Rows() != par.Rows() {
		t.Errorf("parallel differs: seq %d/%d rows, par %d/%d",
			seq.Records(), seq.Rows(), par.Records(), par.Rows())
	}
	for _, tbl := range []string{testutil.Block1, testutil.Block2, testutil.NVMe} {
		if seqStore.Count(tbl) != parStore.Count(tbl) {
			t.Errorf("%s: seq %d rows, parallel %d", tbl, seqStore.Count(tbl), parStore.Count(tbl))
		}
	}
}

// TestNVMeAdditiveChildRows: child rows are additive (per_queue + per_core),
// never a cross-product.
func TestNVMeAdditiveChildRows(t *testing.T) {
	_, st := loadFixture(t, 1)
	q := st.Count(testutil.NVMe + "_per_queue")
	c := st.Count(testutil.NVMe + "_per_core")
	if q <= 300 || c <= 300 {
		t.Errorf("repeated fields should give >1 row per record: q=%d c=%d", q, c)
	}
	if c <= q {
		t.Errorf("8..12 cores should exceed 3..6 queues: q=%d c=%d", q, c)
	}
	// additive, not multiplicative: nowhere near q*c
	if q >= 300*6+1 || c >= 300*12+1 {
		t.Errorf("child rows look multiplicative: q=%d c=%d", q, c)
	}
}

// TestNVMeRecordIDCorrelation: record_id is unique per record in main, and
// every child row links back.
func TestNVMeRecordIDCorrelation(t *testing.T) {
	_, st := loadFixture(t, 1)

	mainIDs := map[uint64]bool{}
	for i := 0; i < st.Count(testutil.NVMe); i++ {
		id, ok := st.Row(testutil.NVMe, i)["record_id"].(uint64)
		if !ok {
			t.Fatalf("main row %d has no uint64 record_id", i)
		}
		if mainIDs[id] {
			t.Fatalf("duplicate record_id %d in main table", id)
		}
		mainIDs[id] = true
	}
	if len(mainIDs) != 300 {
		t.Errorf("distinct record_ids = %d, want 300", len(mainIDs))
	}

	for _, child := range []string{testutil.NVMe + "_per_queue", testutil.NVMe + "_per_core"} {
		seen := map[uint64]bool{}
		for i := 0; i < st.Count(child); i++ {
			id, ok := st.Row(child, i)["record_id"].(uint64)
			if !ok {
				t.Fatalf("%s row %d has no uint64 record_id", child, i)
			}
			if !mainIDs[id] {
				t.Fatalf("%s row %d references unknown record_id %d", child, i, id)
			}
			seen[id] = true
		}
		if len(seen) != 300 {
			t.Errorf("%s links back to %d records, want 300", child, len(seen))
		}
	}
}

func TestRowMatchesIllustration(t *testing.T) {
	_, st := loadFixture(t, 1)
	row := st.Row(testutil.Block1, 0)
	if got := row["run_id"]; got != filepath.Base(testutil.RunDir(t)) {
		t.Errorf("run_id = %v", got)
	}
	if got := row["hostname"]; got != "spark-e97e" {
		t.Errorf("hostname = %v", got)
	}
	if got := row["timestamp"]; got != "2026-07-27T18:02:21.000Z" { // RFC 3339 UTC
		t.Errorf("timestamp = %v", got)
	}
	ts, ok := row["ts"].(time.Time)
	if !ok || ts.Year() != 2026 {
		t.Errorf("ts = %v", row["ts"])
	}
	if got := row["device"]; got != "nvme0n1" {
		t.Errorf("device = %v", got)
	}
}

// ---- batch loading --------------------------------------------------------

// makeBatch builds a parent directory of runs: `good` are copies of the
// fixture, `broken` have a valid .proto but a corrupt .pb.
func makeBatch(t *testing.T, good, broken []string) string {
	t.Helper()
	parent := filepath.Join(t.TempDir(), "runs")
	if err := os.MkdirAll(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	src := testutil.RunDir(t)
	for _, tag := range good {
		dst := filepath.Join(parent, "ProfileData-"+tag+"-20260727-180221")
		if err := copyTree(src, dst); err != nil {
			t.Fatal(err)
		}
	}
	units, err := discover.Discover(src)
	if err != nil {
		t.Fatal(err)
	}
	var protoSrc string
	for _, u := range units {
		if u.Stem == testutil.Block1 {
			protoSrc = u.ProtoPath
		}
	}
	for _, tag := range broken {
		d := filepath.Join(parent, "ProfileData-"+tag+"-20260727-180221", "Linux", "Block")
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		b, err := os.ReadFile(protoSrc)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, testutil.Block1+".proto"), b, 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, testutil.Block1+".pb"),
			[]byte{0xff, 0xff, ' ', 'n', 'o', 't', ' ', 'p', 'b'}, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return parent
}

func TestFindRunsAndAutodetect(t *testing.T) {
	parent := makeBatch(t, []string{"t1", "t2"}, nil)
	var names []string
	for _, d := range runner.FindRuns(parent) {
		names = append(names, filepath.Base(d))
	}
	want := []string{"ProfileData-t1-20260727-180221", "ProfileData-t2-20260727-180221"}
	if strings.Join(names, ",") != strings.Join(want, ",") {
		t.Errorf("FindRuns = %v, want %v", names, want)
	}
	// a single run directory has no ProfileData-* children -> not batch
	if got := runner.FindRuns(testutil.RunDir(t)); len(got) != 0 {
		t.Errorf("a single run should not look like a batch, got %v", got)
	}
}

func TestBatchAllGood(t *testing.T) {
	parent := makeBatch(t, []string{"t1", "t2", "t3"}, nil)
	b, err := runner.RunBatch(context.Background(), parent, testutil.Config(1, 250), memory.New(), false)
	if err != nil {
		t.Fatalf("batch: %v", err)
	}
	if b.Status() != "complete" || b.ExitCode() != fwerr.ExitOK {
		t.Errorf("status=%s exit=%d", b.Status(), b.ExitCode())
	}
	for _, o := range b.Outcomes {
		if o.Status != "complete" {
			t.Errorf("%s = %s", o.RunID, o.Status)
		}
	}
}

func TestBatchFailFastStopsAndSkips(t *testing.T) {
	// "bad" sorts before "t1"/"t2", so it fails first and the rest are skipped
	parent := makeBatch(t, []string{"t1", "t2"}, []string{"bad"})
	b, err := runner.RunBatch(context.Background(), parent, testutil.Config(1, 250), memory.New(), false)
	if err != nil {
		t.Fatalf("batch: %v", err)
	}
	status := map[string]string{}
	for _, o := range b.Outcomes {
		status[o.RunID] = o.Status
	}
	if status["ProfileData-bad-20260727-180221"] != "failed" {
		t.Errorf("bad run = %s, want failed", status["ProfileData-bad-20260727-180221"])
	}
	for _, tag := range []string{"t1", "t2"} {
		k := "ProfileData-" + tag + "-20260727-180221"
		if status[k] != "skipped" {
			t.Errorf("%s = %s, want skipped", k, status[k])
		}
	}
	if b.ExitCode() != fwerr.ExitInput { // the failed run's class code
		t.Errorf("exit code = %d, want %d", b.ExitCode(), fwerr.ExitInput)
	}
}

func TestBatchContinueOnError(t *testing.T) {
	parent := makeBatch(t, []string{"t1", "t2"}, []string{"bad"})
	b, err := runner.RunBatch(context.Background(), parent, testutil.Config(1, 250), memory.New(), true)
	if err != nil {
		t.Fatalf("batch: %v", err)
	}
	counts := map[string]int{}
	for _, o := range b.Outcomes {
		counts[o.Status]++
	}
	if counts["complete"] != 2 || counts["failed"] != 1 || counts["skipped"] != 0 {
		t.Errorf("counts = %v, want 2 complete / 1 failed / 0 skipped", counts)
	}
	if b.ExitCode() != fwerr.ExitInput { // first failing run's code
		t.Errorf("exit code = %d, want %d", b.ExitCode(), fwerr.ExitInput)
	}
}

func TestBatchEmptyParentErrors(t *testing.T) {
	empty := filepath.Join(t.TempDir(), "empty")
	if err := os.MkdirAll(empty, 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := runner.RunBatch(context.Background(), empty, testutil.Config(1, 250), memory.New(), false)
	if err == nil {
		t.Fatal("an empty parent must be an error")
	}
	if !strings.Contains(err.Error(), "no ProfileData") {
		t.Errorf("unhelpful error: %v", err)
	}
}

// ---- failure path ---------------------------------------------------------

func TestCorruptPBGivesClearError(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "ProfileData-bad-1", "Linux", "Block")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	units, err := discover.Discover(testutil.RunDir(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, u := range units {
		if u.Stem == testutil.Block1 {
			b, _ := os.ReadFile(u.ProtoPath)
			os.WriteFile(filepath.Join(dir, testutil.Block1+".proto"), b, 0o644)
		}
	}
	os.WriteFile(filepath.Join(dir, testutil.Block1+".pb"),
		[]byte{0xff, 0xff, ' ', 'n', 'o', 't', ' ', 'a', ' ', 'p', 'b', 0x00, 0x01}, 0o644)

	st := memory.New()
	st.Connect(context.Background())
	_, err = runner.RunLoad(context.Background(), filepath.Dir(filepath.Dir(dir)),
		testutil.Config(1, 250), st)
	if err == nil {
		t.Fatal("a corrupt .pb must be an error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "does not match") && !strings.Contains(msg, "truncated") &&
		!strings.Contains(msg, "could not parse") {
		t.Errorf("error should explain the .pb/.proto mismatch, got: %v", err)
	}
	if code := fwerr.ExitCode(err); code != fwerr.ExitInput {
		t.Errorf("corrupt .pb exit code = %d, want %d (input)", code, fwerr.ExitInput)
	}
}

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if fi.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		in, err := os.Open(p)
		if err != nil {
			return err
		}
		defer in.Close()
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		out, err := os.Create(target)
		if err != nil {
			return err
		}
		defer out.Close()
		_, err = io.Copy(out, in)
		return err
	})
}
