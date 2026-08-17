package registry_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/store"
	"github.com/vishal9359/Analysis-FW/internal/store/clickhouse"
	"github.com/vishal9359/Analysis-FW/internal/testutil"
)

func schemaFor(t *testing.T, stem string) *registry.TableSchema {
	t.Helper()
	units, err := discover.Discover(testutil.RunDir(t))
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	for _, u := range units {
		if u.Stem == stem {
			s, err := registry.Build(u.Stem, u.ProtoPath)
			if err != nil {
				t.Fatalf("build schema %s: %v", stem, err)
			}
			return s
		}
	}
	t.Fatalf("unit %s not found", stem)
	return nil
}

func colNames(cols []store.Column) []string {
	out := make([]string, len(cols))
	for i, c := range cols {
		out[i] = c.Name
	}
	return out
}

func has(names []string, want string) bool {
	for _, n := range names {
		if n == want {
			return true
		}
	}
	return false
}

// ---- generic detection (scalar payload) -----------------------------------

func TestDetectsRecordGenericPayloadWrapper(t *testing.T) {
	s := schemaFor(t, testutil.Block1)
	if got := string(s.Record.Name()); got != "StatLog" {
		t.Errorf("record message = %s, want StatLog", got)
	}
	if got := string(s.Wrapper.Name()); got != "StatLogs" {
		t.Errorf("wrapper message = %s, want StatLogs", got)
	}
	if got := string(s.RepeatedField.Name()); got != "stat_logs" {
		t.Errorf("repeated field = %s, want stat_logs", got)
	}
	if got := string(s.GenericField.Name()); got != "generic_format" {
		t.Errorf("generic field = %s", got)
	}
	if got := string(s.PayloadField.Name()); got != "payload" {
		t.Errorf("payload field = %s", got)
	}
}

// TestBlockHasNoRecordIDOrChildren: a scalar-only payload is unchanged — no
// record_id, no child tables.
func TestBlockHasNoRecordIDOrChildren(t *testing.T) {
	s := schemaFor(t, testutil.Block1)
	if s.HasRecordID {
		t.Error("scalar-only payload should not get a record_id")
	}
	if len(s.Children) != 0 {
		t.Errorf("children = %d, want 0", len(s.Children))
	}
	if has(colNames(s.Columns), "record_id") {
		t.Error("record_id must not appear in a scalar-only table")
	}
}

func TestContractNamesEnforced(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x.proto")
	src := `syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
message P { optional uint64 m=1; }
message Rec { GenericFormat generic_format=1; P blk_payload=2; }
message Log { repeated Rec rows=1; }`
	if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := registry.Build("x", p)
	if err == nil {
		t.Fatal("expected a schema error for a missing 'payload' field")
	}
	if !strings.Contains(err.Error(), "payload") {
		t.Errorf("error should name the missing field, got: %v", err)
	}
}

func TestFlatColumnsAndDDL(t *testing.T) {
	s := schemaFor(t, testutil.Block1)
	want := []string{"run_id", "ts", "timestamp", "hostname", "component", "tag", "log_level"}
	got := colNames(s.Columns)
	for i, w := range want {
		if i >= len(got) || got[i] != w {
			t.Fatalf("columns[:7] = %v, want prefix %v", got[:min(7, len(got))], want)
		}
	}
	ddl := clickhouse.CreateTableDDL("db", s.Stem, s.Columns, s.OrderBy)
	if !strings.Contains(ddl, "PARTITION BY run_id") {
		t.Error("DDL missing PARTITION BY run_id")
	}
	if !strings.Contains(ddl, "ORDER BY (run_id, hostname, ts)") {
		t.Error("DDL missing expected ORDER BY")
	}
}

// ---- repeated child tables (NVMe) -----------------------------------------

func TestNVMeChildrenDetected(t *testing.T) {
	s := schemaFor(t, testutil.NVMe)
	if !s.HasRecordID {
		t.Error("a payload with repeated sub-messages must get a record_id")
	}
	if !has(colNames(s.Columns), "record_id") {
		t.Error("record_id missing from main table")
	}
	kids := map[string]registry.ChildSchema{}
	for _, c := range s.Children {
		kids[c.Table] = c
	}
	for _, want := range []string{testutil.NVMe + "_per_queue", testutil.NVMe + "_per_core"} {
		if _, ok := kids[want]; !ok {
			t.Fatalf("missing child table %s (have %v)", want, keys(kids))
		}
	}

	q := kids[testutil.NVMe+"_per_queue"]
	if string(q.RepeatedField.Name()) != "per_queue" {
		t.Errorf("per_queue repeated field = %s", q.RepeatedField.Name())
	}
	qc := colNames(q.Columns)
	if !has(qc, "record_id") || !has(qc, "queue_id") {
		t.Errorf("per_queue columns missing record_id/queue_id: %v", qc)
	}
	// child ORDER BY puts the key dimension early for fast filtering
	wantOrder := []string{"run_id", "hostname", "queue_id", "ts"}
	if !equal(q.OrderBy, wantOrder) {
		t.Errorf("per_queue ORDER BY = %v, want %v", q.OrderBy, wantOrder)
	}
	c := kids[testutil.NVMe+"_per_core"]
	if !equal(c.OrderBy, []string{"run_id", "hostname", "cpu_id", "ts"}) {
		t.Errorf("per_core ORDER BY = %v", c.OrderBy)
	}
}

// ---- extensibility & seams ------------------------------------------------

func TestAddedPayloadFieldNeedsNoCodeChange(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x.proto")
	src := `syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
message Payload { optional uint64 old_metric=1; optional uint64 new_metric=2; }
message StatLog { GenericFormat generic_format=1; Payload payload=2; }
message StatLogs { repeated StatLog stat_logs=1; }`
	if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	s, err := registry.Build("x", p)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if !has(colNames(s.Columns), "new_metric") {
		t.Error("a new payload field should appear as a column with no code change")
	}
}

func TestDescriptorRebuildMatches(t *testing.T) {
	s := schemaFor(t, testutil.NVMe)
	rebuilt, err := registry.BuildFromDescriptor(testutil.NVMe, s.Descriptor, testutil.NVMe+".proto")
	if err != nil {
		t.Fatalf("rebuild: %v", err)
	}
	if !equal(colNames(rebuilt.Columns), colNames(s.Columns)) {
		t.Error("rebuilt columns differ from the original")
	}
	var a, b []string
	for _, c := range s.Children {
		a = append(a, c.Table)
	}
	for _, c := range rebuilt.Children {
		b = append(b, c.Table)
	}
	if !equal(a, b) {
		t.Errorf("rebuilt children = %v, want %v", b, a)
	}
}

func keys(m map[string]registry.ChildSchema) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
