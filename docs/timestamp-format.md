# Timestamp format for Profile FW records

The `timestamp` field in `GenericFormat` should be **RFC 3339** (the strict
profile of ISO 8601), emitted in **UTC**, with **millisecond precision**:

```
2026-07-31T16:33:53.005Z
```

## Why

- **UTC (the `Z` form).** SUTs may be in different timezones, but they all feed
  one central database. A single clock removes offset/DST ambiguity, sorts
  correctly, and is the norm for machine/telemetry data. (The Analysis FW loader
  converts everything to UTC for its `ts` column anyway.)
- **RFC 3339 with an explicit zone.** Unambiguous and parseable everywhere.
- **Never build the zone by string concatenation.** That produced the invalid
  `2026-07-31T16:33:53.005Z05:30` (you cannot have both `Z` and an offset). Let
  the formatter write the zone — it is then impossible to get wrong.

Both `...Z` (UTC) and `...+05:30` (explicit offset) are accepted by the loader,
but **UTC is the recommended default**.

## Go

```go
package profile

import "time"

// RFC 3339 with millisecond precision and an explicit zone.
// The "Z07:00" element prints "Z" for UTC and "+05:30" for an offset — so it can
// never produce the invalid "Z05:30" that manual string-building caused.
const TimestampLayout = "2006-01-02T15:04:05.000Z07:00"

// FormatTimestamp renders t in UTC, e.g. "2026-07-31T16:33:53.005Z".
// Recommended: one clock for the whole fleet.
func FormatTimestamp(t time.Time) string {
	return t.UTC().Format(TimestampLayout)
}

// FormatTimestampLocal keeps local wall-clock with an explicit offset instead,
// e.g. "2026-07-31T22:03:53.005+05:30". Use FormatTimestamp (UTC) unless you
// specifically need local time.
func FormatTimestampLocal(t time.Time) string {
	return t.Format(TimestampLayout)
}
```

Setting it on the record header (proto3 `optional string` → `*string`):

```go
hdr := &pb.GenericFormat{
	Timestamp: proto.String(profile.FormatTimestamp(time.Now())),
	Hostname:  proto.String(hostname),
	Component: proto.Uint32(component),
	Tag:       proto.String(tag),
	LogLevel:  proto.Uint32(logLevel),
}
```

Want nanoseconds instead of milliseconds? Change `.000` to `.000000000` in the
layout.

## What the loader does with it

- Stores the exact string in the `timestamp` column, and a UTC `DateTime` in the
  `ts` column (used for time-range queries and ordering).
- An offset like `+05:30` is converted to the true UTC instant for `ts`.
- A malformed timestamp never crashes the load — `ts` falls back to the epoch
  (1970-01-01) and the original string is preserved in `timestamp` — but the
  `ts` value would be wrong, so emit valid RFC 3339.
