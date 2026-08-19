// Package worker loads one unit (one <layer>_<type>) into its table.
//
// For each record (from the wrapper's repeated field), the generic header
// sub-message and the payload sub-message are flattened into one flat row,
// prefixed with run_id and a parsed ts, suffixed with the ingest time. Column
// order matches TableSchema.Columns.
package worker

import (
	"context"
	"regexp"
	"strings"
	"time"

	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/vishal9359/Analysis-FW/internal/reader"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/store"
)

// Header timestamp. Producers have shipped a few shapes, so we try several:
//
//	"Mon Jul 27 18:02:21 2026"        (abbreviated names + year)
//	"Monday July 27 18:01:46:233"     (full names, NO year, trailing ms)
//	ISO-8601 / RFC 3339               "2026-07-27T18:02:21.000Z"
//
// Layouts without a year get the current year injected.
var tsLayouts = []struct {
	layout  string
	hasYear bool
}{
	{"2006-01-02T15:04:05.999999999Z07:00", true},
	{"2006-01-02T15:04:05Z07:00", true},
	{"2006-01-02T15:04:05", true},
	{"2006-01-02 15:04:05", true},
	{"Mon Jan 2 15:04:05 2006", true},
	{"Monday January 2 15:04:05 2006", true},
	{"Mon Jan 2 15:04:05", false},
	{"Monday January 2 15:04:05", false},
}

var (
	bracketed = regexp.MustCompile(`^\[(.*)\]$`)
	// a millisecond group tacked onto the time, e.g. "18:01:46:233"
	millis = regexp.MustCompile(`(\d{2}:\d{2}:\d{2})[:.]\d{1,6}\b`)
	// a stray offset appended after 'Z' (e.g. "...Z00:00"). RFC 3339 uses
	// either 'Z' (UTC) OR an offset, never both — this is a producer bug. 'Z'
	// already means UTC, so drop the trailing offset and keep 'Z'.
	zOffset = regexp.MustCompile(`Z[+-]?\d{2}:?\d{2}$`)
	// collapse repeated spaces so "Jul  2" matches a single-space layout
	spaces = regexp.MustCompile(`\s+`)
)

// ParseTS parses a header timestamp string, or reports ok=false if it matches
// no known format. The original string is always kept in the `timestamp`
// column, so a parse miss loses nothing.
func ParseTS(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false
	}
	if m := bracketed.FindStringSubmatch(s); m != nil {
		s = strings.TrimSpace(m[1])
	}
	s = zOffset.ReplaceAllString(s, "Z") // normalize malformed "...Z00:00"

	for _, l := range tsLayouts {
		candidate := s
		if !strings.Contains(l.layout, ".999") {
			// layouts without a fractional slot: drop a trailing ms group
			candidate = millis.ReplaceAllString(candidate, "$1")
		}
		candidate = spaces.ReplaceAllString(candidate, " ")
		t, err := time.Parse(l.layout, candidate)
		if err != nil {
			continue
		}
		if !l.hasYear {
			t = t.AddDate(time.Now().Year(), 0, 0)
		}
		return t, true
	}
	return time.Time{}, false
}

// TSForDB is the `ts` column value: a UTC time clamped to tsRange (the target
// store's representable range) so it always serializes successfully.
// Unparseable timestamps become the range's lower bound, never a failure.
func TSForDB(s string, tsRange [2]time.Time) time.Time {
	lo, hi := tsRange[0], tsRange[1]
	t, ok := ParseTS(s)
	if !ok {
		return lo
	}
	// A tz-aware timestamp is converted to the real UTC instant; a naive one is
	// interpreted as UTC. Either way ts is deterministic regardless of the
	// loader machine's timezone.
	t = t.UTC()
	if t.Before(lo) {
		return lo
	}
	if t.After(hi) {
		return hi
	}
	return t
}

// UnitResult is what one unit's load produced.
type UnitResult struct {
	Stem      string
	Files     int
	Records   int            // parent records read
	TableRows map[string]int // table name -> rows inserted (main + children)
}

// OK reports whether the main table got exactly one row per parent record.
func (r UnitResult) OK() bool { return r.TableRows[r.Stem] == r.Records }

// TotalRows is every row written across main and child tables.
func (r UnitResult) TotalRows() int {
	n := 0
	for _, v := range r.TableRows {
		n += v
	}
	return n
}

// LoadUnit loads one unit into its main table plus a child table per repeated
// payload sub-message. A record_id (64-bit sequence, per record) links a main
// row to its child rows.
func LoadUnit(ctx context.Context, pbParts []string, schema *registry.TableSchema,
	st store.Store, runID string, batchSize int) (UnitResult, error) {

	res := UnitResult{Stem: schema.Stem, Files: len(pbParts), TableRows: map[string]int{schema.Stem: 0}}

	if err := st.EnsureTable(ctx, schema.Stem, schema.Columns, schema.OrderBy); err != nil {
		return res, err
	}
	for _, c := range schema.Children {
		if err := st.EnsureTable(ctx, c.Table, c.Columns, c.OrderBy); err != nil {
			return res, err
		}
		res.TableRows[c.Table] = 0
	}

	loadedAt := time.Now().Truncate(time.Second)
	tsRange := st.TimestampRange() // the target store's limits, not ours

	mainCols := columnNames(schema.Columns)
	childCols := map[string][]string{}
	for _, c := range schema.Children {
		childCols[c.Table] = columnNames(c.Columns)
	}

	mainBatch := make([][]interface{}, 0, batchSize)
	childBatch := map[string][][]interface{}{}
	for _, c := range schema.Children {
		childBatch[c.Table] = make([][]interface{}, 0, batchSize)
	}

	flushMain := func() error {
		if len(mainBatch) == 0 {
			return nil
		}
		if err := st.Insert(ctx, schema.Stem, mainCols, mainBatch); err != nil {
			return err
		}
		res.TableRows[schema.Stem] += len(mainBatch)
		mainBatch = mainBatch[:0]
		return nil
	}
	flushChild := func(table string) error {
		b := childBatch[table]
		if len(b) == 0 {
			return nil
		}
		if err := st.Insert(ctx, table, childCols[table], b); err != nil {
			return err
		}
		res.TableRows[table] += len(b)
		childBatch[table] = b[:0]
		return nil
	}

	// Reused across records; its values are copied into each row.
	genVals := make([]interface{}, len(schema.GenericFields))

	recordID := uint64(0)
	for _, part := range pbParts {
		records, err := reader.Records(part, schema)
		if err != nil {
			return res, err
		}
		for i := 0; i < records.Len(); i++ {
			rec := records.Get(i).Message()
			res.Records++

			g := rec.Get(schema.GenericField).Message()
			p := rec.Get(schema.PayloadField).Message()
			ts := TSForDB(g.Get(schema.TimestampFd).String(), tsRange)

			for j, path := range schema.GenericFields {
				genVals[j] = goValueAt(g, path)
			}

			row := make([]interface{}, 0, len(schema.Columns))
			row = append(row, runID, ts)
			if schema.HasRecordID {
				row = append(row, recordID)
			}
			row = append(row, genVals...)
			for _, path := range schema.PayloadFields {
				row = append(row, goValueAt(p, path))
			}
			row = append(row, loadedAt)
			mainBatch = append(mainBatch, row)

			for _, c := range schema.Children {
				subs := p.Get(c.RepeatedField).List()
				for k := 0; k < subs.Len(); k++ {
					sub := subs.Get(k).Message()
					crow := make([]interface{}, 0, len(c.Columns))
					crow = append(crow, runID, ts, recordID)
					crow = append(crow, genVals...)
					for _, path := range c.SubFields {
						crow = append(crow, goValueAt(sub, path))
					}
					crow = append(crow, loadedAt)
					childBatch[c.Table] = append(childBatch[c.Table], crow)
				}
				if len(childBatch[c.Table]) >= batchSize {
					if err := flushChild(c.Table); err != nil {
						return res, err
					}
				}
			}

			recordID++
			if len(mainBatch) >= batchSize {
				if err := flushMain(); err != nil {
					return res, err
				}
			}
		}
	}

	if err := flushMain(); err != nil {
		return res, err
	}
	for _, c := range schema.Children {
		if err := flushChild(c.Table); err != nil {
			return res, err
		}
	}
	return res, nil
}

func columnNames(cols []store.Column) []string {
	out := make([]string, len(cols))
	for i, c := range cols {
		out[i] = c.Name
	}
	return out
}

// goValueAt reads the scalar a FieldPath points at, descending through singular
// sub-messages (a 1:1 group flattened onto this row). An absent sub-message
// yields its fields' proto3 defaults rather than nil, matching a missing scalar.
func goValueAt(m protoreflect.Message, path registry.FieldPath) interface{} {
	for i := 0; i < len(path)-1; i++ {
		// Get on an unset message field returns a read-only empty message, so
		// the remaining reads produce zero values.
		m = m.Get(path[i]).Message()
	}
	return goValue(m, path[len(path)-1])
}

// goValue converts a protobuf field value to the concrete Go type the store
// driver expects for that column. A missing proto3 field yields its zero value,
// never nil — matching the Python loader's behaviour.
func goValue(m protoreflect.Message, fd protoreflect.FieldDescriptor) interface{} {
	v := m.Get(fd)
	switch fd.Kind() {
	case protoreflect.StringKind:
		return v.String()
	case protoreflect.BytesKind:
		return string(v.Bytes())
	case protoreflect.BoolKind:
		return v.Bool()
	case protoreflect.Int32Kind, protoreflect.Sint32Kind, protoreflect.Sfixed32Kind:
		return int32(v.Int())
	case protoreflect.Int64Kind, protoreflect.Sint64Kind, protoreflect.Sfixed64Kind:
		return v.Int()
	case protoreflect.Uint32Kind, protoreflect.Fixed32Kind:
		return uint32(v.Uint())
	case protoreflect.Uint64Kind, protoreflect.Fixed64Kind:
		return v.Uint()
	case protoreflect.FloatKind:
		return float32(v.Float())
	case protoreflect.DoubleKind:
		return v.Float()
	case protoreflect.EnumKind:
		return int32(v.Enum())
	default:
		return v.Interface()
	}
}
