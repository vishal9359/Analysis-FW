package registry_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/testutil"
)

func writeProto(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "x.proto")
	src := `syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
` + body + `
message StatLog { GenericFormat generic_format=1; Payload payload=2; }
message StatLogs { repeated StatLog stat_logs=1; }`
	if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// A singular sub-message is 1:1 with the row, so it flattens into the SAME
// table with its field name as a prefix — never a child table, which would
// force a JOIN for data guaranteed to be one row.
func TestSingularSubMessageFlattensWithPrefix(t *testing.T) {
	p := writeProto(t, `
message IOFlags { optional uint64 direct=1; optional uint64 sync=2; }
message Payload { optional uint64 mmap_count=1; IOFlags io_flags=2; }`)

	s, err := registry.Build("x", p)
	if err != nil {
		t.Fatalf("a singular sub-message must be accepted: %v", err)
	}
	if len(s.Children) != 0 {
		t.Errorf("1:1 group must NOT become a child table, got %d children", len(s.Children))
	}
	if s.HasRecordID {
		t.Error("no children means no record_id is needed")
	}
	var names []string
	for _, c := range s.Columns {
		names = append(names, c.Name)
	}
	joined := strings.Join(names, ",")
	for _, want := range []string{"mmap_count", "io_flags_direct", "io_flags_sync"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing column %q in %v", want, names)
		}
	}
	for _, unwanted := range []string{",direct", ",sync"} { // bare, unprefixed
		if strings.Contains(joined, unwanted) {
			t.Errorf("column should be prefixed, found bare name in %v", names)
		}
	}
}

// A repeated sub-message is 1:N and still earns its own child table — the two
// shapes must not be confused.
func TestRepeatedStillBecomesChildTable(t *testing.T) {
	p := writeProto(t, `
message Item { optional uint64 id=1; optional uint64 n=2; }
message Payload { optional uint64 a=1; repeated Item items=2; }`)

	s, err := registry.Build("x", p)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Children) != 1 || s.Children[0].Table != "x_items" {
		t.Fatalf("repeated message must become a child table, got %+v", s.Children)
	}
	if !s.HasRecordID {
		t.Error("children exist, so record_id must link them")
	}
}

// Nesting deeper than one level keeps joining the prefix.
func TestNestedGroupsFlattenRecursively(t *testing.T) {
	p := writeProto(t, `
message Inner { optional uint64 leaf=1; }
message Outer { Inner inner=1; }
message Payload { Outer outer=1; }`)

	s, err := registry.Build("x", p)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, c := range s.Columns {
		if c.Name == "outer_inner_leaf" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected column outer_inner_leaf, got %+v", s.Columns)
	}
}

// The real syscall proto is the case that prompted this: io_flags must land on
// the main row, io_patterns must stay a child table.
func TestSyscallIOFlagsOnMainRowIOPatternsAsChild(t *testing.T) {
	s, err := registry.Build(testutil.Syscall,
		filepath.Join(testutil.ProtoDir(), testutil.Syscall+".proto"))
	if err != nil {
		t.Fatalf("syscall proto must load: %v", err)
	}
	var main []string
	for _, c := range s.Columns {
		main = append(main, c.Name)
	}
	for _, want := range []string{"io_flags_direct", "io_flags_hiprio", "mmap_count"} {
		if !strings.Contains(strings.Join(main, ","), want) {
			t.Errorf("main table missing %q: %v", want, main)
		}
	}
	if len(s.Children) != 1 || !strings.HasSuffix(s.Children[0].Table, "_io_patterns") {
		t.Errorf("io_patterns should be the only child table, got %+v", s.Children)
	}
}
