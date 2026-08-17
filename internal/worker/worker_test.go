package worker_test

import (
	"testing"
	"time"

	"github.com/vishal9359/Analysis-FW/internal/store"
	"github.com/vishal9359/Analysis-FW/internal/store/clickhouse"
	"github.com/vishal9359/Analysis-FW/internal/worker"
)

func TestParseTSFormats(t *testing.T) {
	got, ok := worker.ParseTS("Mon Jul 27 18:02:21 2026")
	if !ok || got.Year() != 2026 {
		t.Errorf("abbreviated names + year: got %v ok=%v", got, ok)
	}
	got, ok = worker.ParseTS("2026-07-27T18:02:21")
	if !ok || got.Minute() != 2 {
		t.Errorf("ISO: got %v ok=%v", got, ok)
	}
	// full names, no year, trailing ms
	got, ok = worker.ParseTS("Monday July 27 18:01:46:233")
	if !ok {
		t.Fatalf("full names/no year did not parse")
	}
	if got.Month() != time.July || got.Day() != 27 || got.Second() != 46 {
		t.Errorf("full names/no year: got %v", got)
	}
	// a no-year format gets the current year injected, so ts lands in the right
	// partition instead of year 0
	if got.Year() != time.Now().Year() {
		t.Errorf("no-year format: year = %d, want current year %d", got.Year(), time.Now().Year())
	}
	if _, ok := worker.ParseTS(""); ok {
		t.Error("empty string should not parse")
	}
	if _, ok := worker.ParseTS("garbage"); ok {
		t.Error("garbage should not parse")
	}
}

// TestISO8601RFC3339 covers fractional seconds and a Z/offset timezone.
func TestISO8601RFC3339(t *testing.T) {
	got, ok := worker.ParseTS("2026-07-31T16:33:53.005Z")
	if !ok || got.UTC().Hour() != 16 {
		t.Errorf("Z form: got %v ok=%v", got, ok)
	}
	// +05:30 offset is converted to the real UTC instant
	if h := worker.TSForDB("2026-07-31T16:33:53.005+05:30", clickhouse.TimestampRange).Hour(); h != 11 {
		t.Errorf("+05:30 -> UTC hour = %d, want 11", h)
	}
	if h := worker.TSForDB("2026-07-31T16:33:53.005Z", clickhouse.TimestampRange).Hour(); h != 16 {
		t.Errorf("Z -> UTC hour = %d, want 16", h)
	}
}

// TestMalformedZOffsetTreatedAsUTC covers a real producer bug: 'Z' followed by
// an offset ("...Z00:00"). 'Z' means UTC, so the trailing offset is dropped and
// it parses as UTC — not the 1970 epoch fallback.
func TestMalformedZOffsetTreatedAsUTC(t *testing.T) {
	got := worker.TSForDB("2026-08-03T07:58:57.876Z00:00", clickhouse.TimestampRange)
	want := time.Date(2026, 8, 3, 7, 58, 57, 0, time.UTC)
	if got.Year() != want.Year() || got.Month() != want.Month() || got.Day() != want.Day() ||
		got.Hour() != want.Hour() || got.Minute() != want.Minute() || got.Second() != want.Second() {
		t.Errorf("Z00:00 form = %v, want %v", got, want)
	}
	if h := worker.TSForDB("2026-07-31T16:33:53.005Z05:30", clickhouse.TimestampRange).Hour(); h != 16 {
		t.Errorf("Z wins over the offset: hour = %d, want 16", h)
	}
}

// TestTSForDBNeverOutOfRange: clamped to the target store's range — here
// ClickHouse's unsigned 32-bit epoch — so a bad producer timestamp can never
// fail the insert.
func TestTSForDBNeverOutOfRange(t *testing.T) {
	for _, s := range []string{
		"Monday July 27 18:01:46:233", "Mon Jul 27 18:02:21 2026",
		"", "garbage", "Sat Jun 27 14:40:48 1969",
	} {
		epoch := worker.TSForDB(s, clickhouse.TimestampRange).Unix()
		if epoch < 0 || epoch > 4294967295 {
			t.Errorf("%q -> epoch %d, outside ClickHouse's DateTime range", s, epoch)
		}
	}
}

// TestTSClampFollowsTheStoreNotTheLoader: the clamp is the adapter's limit, not
// a hardcoded one — a store with a wider range keeps a pre-1970 timestamp
// instead of flooring it to the epoch.
func TestTSClampFollowsTheStoreNotTheLoader(t *testing.T) {
	const old = "1969-06-27T14:40:48"
	if y := worker.TSForDB(old, clickhouse.TimestampRange).Year(); y != 1970 {
		t.Errorf("ClickHouse should floor a pre-1970 value, got year %d", y)
	}
	if y := worker.TSForDB(old, store.WidestTimestampRange).Year(); y != 1969 {
		t.Errorf("a wider store should keep the value, got year %d", y)
	}
}
