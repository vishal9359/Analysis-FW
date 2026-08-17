// Package factory is the one place a concrete store adapter is named.
//
// Adding a database is: write an adapter implementing store.Store, then add one
// case here. Nothing above the store seam changes.
package factory

import (
	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/store"
	"github.com/vishal9359/Analysis-FW/internal/store/clickhouse"
	"github.com/vishal9359/Analysis-FW/internal/store/memory"
)

// New builds the configured store. opts carries adapter-specific settings (e.g.
// ClickHouse's async_insert); unknown keys are the adapter's business.
func New(kind, host string, port int, database string, opts map[string]interface{}) (store.Store, error) {
	switch kind {
	case "clickhouse":
		async := false
		if v, ok := opts["async_insert"].(bool); ok {
			async = v
		}
		return clickhouse.New(host, port, database, async), nil
	case "memory":
		return memory.New(), nil
	default:
		return nil, fwerr.Config("config: unknown store.kind %q (known: clickhouse, memory)", kind)
	}
}
