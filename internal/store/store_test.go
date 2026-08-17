package store_test

import (
	"strings"
	"testing"

	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/store"
	"github.com/vishal9359/Analysis-FW/internal/store/clickhouse"
	"github.com/vishal9359/Analysis-FW/internal/store/factory"
	"github.com/vishal9359/Analysis-FW/internal/store/memory"
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
				t.Fatalf("build schema: %v", err)
			}
			return s
		}
	}
	t.Fatalf("unit %s not found", stem)
	return nil
}

// TestSchemaIsDatabaseNeutral is the seam guarantee: nothing above the store
// names a database type. A derived schema carries only neutral ColumnTypes —
// never "UInt64", "MergeTree", or any other ClickHouse spelling.
func TestSchemaIsDatabaseNeutral(t *testing.T) {
	for _, stem := range []string{testutil.Block1, testutil.NVMe} {
		s := schemaFor(t, stem)
		cols := append([]store.Column{}, s.Columns...)
		for _, c := range s.Children {
			cols = append(cols, c.Columns...)
		}
		for _, c := range cols {
			if c.Type < store.TypeString || c.Type > store.TypeDateTime {
				t.Errorf("%s.%s has a non-neutral type %v", stem, c.Name, c.Type)
			}
			// a neutral type never renders as a SQL spelling
			if n := c.Type.String(); strings.ToLower(n) != n {
				t.Errorf("%s.%s type %q looks like a SQL type, not a neutral one", stem, c.Name, n)
			}
		}
	}
}

// TestAdapterOwnsTheSQLDialect: the same neutral schema renders to a
// database-specific DDL only inside the adapter — so a second adapter is a new
// mapping, not a schema change.
func TestAdapterOwnsTheSQLDialect(t *testing.T) {
	s := schemaFor(t, testutil.Block1)
	ddl := clickhouse.CreateTableDDL("db", s.Stem, s.Columns, s.OrderBy)
	for _, want := range []string{"UInt64", "MergeTree"} { // ClickHouse spellings, in CH only
		if !strings.Contains(ddl, want) {
			t.Errorf("adapter DDL should contain %q, got:\n%s", want, ddl)
		}
	}
}

func TestFactoryBuildsAdaptersAndRejectsUnknown(t *testing.T) {
	st, err := factory.New("memory", "h", 1, "db", nil)
	if err != nil {
		t.Fatalf("memory: %v", err)
	}
	if _, ok := st.(*memory.Store); !ok {
		t.Errorf("memory kind built %T", st)
	}

	st, err = factory.New("clickhouse", "h", 8123, "db",
		map[string]interface{}{"async_insert": true})
	if err != nil {
		t.Fatalf("clickhouse: %v", err)
	}
	ch, ok := st.(*clickhouse.Store)
	if !ok {
		t.Fatalf("clickhouse kind built %T", st)
	}
	if !ch.AsyncInsert || ch.Database != "db" {
		t.Errorf("adapter options not applied: async=%v db=%s", ch.AsyncInsert, ch.Database)
	}

	if _, err := factory.New("oracle", "h", 1, "db", nil); err == nil {
		t.Error("an unknown store.kind must be rejected")
	} else if !strings.Contains(err.Error(), "unknown store.kind") {
		t.Errorf("unhelpful error for unknown kind: %v", err)
	}
}

// TestAdaptersSatisfyTheSeam is a compile-time-ish check that both adapters
// implement the interface — the whole point of the seam.
func TestAdaptersSatisfyTheSeam(t *testing.T) {
	var _ store.Store = memory.New()
	var _ store.Store = clickhouse.New("h", 8123, "db", false)
}
