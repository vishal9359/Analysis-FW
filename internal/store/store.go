// Package store is the database seam — the only boundary the database lives
// behind.
//
// No SQL, no database-specific type, and no database-specific limit appears
// outside an implementation of Store. The schema layer produces neutral
// Columns; each adapter maps those to its own SQL types, writes its own DDL,
// and declares the timestamp range it can represent.
//
// Swapping databases is a new adapter package plus one line in NewStore.
package store

import (
	"context"
	"time"
)

// ColumnType is a database-neutral column type. The schema layer speaks only
// these; adapters map them to their own SQL types.
type ColumnType int

const (
	TypeString ColumnType = iota
	TypeBool
	TypeInt32
	TypeInt64
	TypeUint32
	TypeUint64
	TypeFloat32
	TypeFloat64
	TypeDateTime
)

var typeNames = [...]string{
	"string", "bool", "int32", "int64", "uint32", "uint64",
	"float32", "float64", "datetime",
}

func (t ColumnType) String() string {
	if int(t) < len(typeNames) {
		return typeNames[t]
	}
	return "unknown"
}

// Column is one column of a derived table, in neutral terms.
type Column struct {
	Name string
	Type ColumnType
}

// WidestTimestampRange is what an adapter with no real limit reports.
var WidestTimestampRange = [2]time.Time{
	time.Date(1, 1, 1, 0, 0, 0, 0, time.UTC),
	time.Date(9999, 12, 31, 23, 59, 59, 0, time.UTC),
}

// Store is the seam. Implementations are in sub-packages; nothing above this
// interface names a database.
type Store interface {
	// TimestampRange is the (min, max) timestamp this store can represent. The
	// loader clamps ts to it so a bad producer timestamp can never fail an
	// insert.
	TimestampRange() [2]time.Time

	// Connect opens the connection and ensures the target database exists.
	Connect(ctx context.Context) error

	// EnsureTable creates the table if absent. Called once per table — the main
	// table and each child table.
	EnsureTable(ctx context.Context, table string, cols []Column, orderBy []string) error

	// Insert writes a batch of rows, each in cols order.
	Insert(ctx context.Context, table string, cols []string, rows [][]interface{}) error

	Close() error
}
