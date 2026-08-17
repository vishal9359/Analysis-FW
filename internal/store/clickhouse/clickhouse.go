// Package clickhouse is the ClickHouse store adapter — the only place
// ClickHouse SQL, ClickHouse types, and the ClickHouse driver live.
//
// Everything ClickHouse-specific is here: the neutral-type -> ClickHouse-type
// map, the CREATE TABLE DDL (MergeTree / PARTITION BY), the representable
// timestamp range, and the client.
package clickhouse

import (
	"context"
	"fmt"
	"strings"
	"time"

	ch "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/store"
)

// neutral column type -> ClickHouse SQL type
var toCH = map[store.ColumnType]string{
	store.TypeString:   "String",
	store.TypeBool:     "Bool",
	store.TypeInt32:    "Int32",
	store.TypeInt64:    "Int64",
	store.TypeUint32:   "UInt32",
	store.TypeUint64:   "UInt64",
	store.TypeFloat32:  "Float32",
	store.TypeFloat64:  "Float64",
	store.TypeDateTime: "DateTime",
}

// TimestampRange is ClickHouse's DateTime span: an unsigned 32-bit epoch,
// 1970-01-01 .. 2106-02-07 UTC.
var TimestampRange = [2]time.Time{
	time.Date(1970, 1, 1, 0, 0, 0, 0, time.UTC),
	time.Date(2106, 2, 7, 0, 0, 0, 0, time.UTC),
}

// CreateTableDDL renders a neutral column list as ClickHouse CREATE TABLE.
func CreateTableDDL(database, table string, cols []store.Column, orderBy []string) string {
	parts := make([]string, len(cols))
	for i, c := range cols {
		parts[i] = fmt.Sprintf("`%s` %s", c.Name, toCH[c.Type])
	}
	return fmt.Sprintf(
		"CREATE TABLE IF NOT EXISTS `%s`.`%s`\n(\n    %s\n)\n"+
			"ENGINE = MergeTree\nPARTITION BY run_id\nORDER BY (%s)",
		database, table, strings.Join(parts, ",\n    "), strings.Join(orderBy, ", "))
}

// Store is the ClickHouse adapter. It satisfies store.Store.
type Store struct {
	Host        string
	Port        int
	Database    string
	AsyncInsert bool

	conn driver.Conn
}

// New builds an unconnected adapter.
func New(host string, port int, database string, asyncInsert bool) *Store {
	return &Store{Host: host, Port: port, Database: database, AsyncInsert: asyncInsert}
}

func (s *Store) TimestampRange() [2]time.Time { return TimestampRange }

func (s *Store) settings() ch.Settings {
	v := 0
	if s.AsyncInsert {
		v = 1
	}
	// async_insert must stay off by default so row counts are truthful
	// immediately after insert.
	return ch.Settings{"async_insert": v}
}

func (s *Store) open(database string) (driver.Conn, error) {
	opts := &ch.Options{
		Addr:     []string{fmt.Sprintf("%s:%d", s.Host, s.Port)},
		Settings: s.settings(),
	}
	if database != "" {
		opts.Auth = ch.Auth{Database: database}
	}
	return ch.Open(opts)
}

// Connect opens the connection, creating the target database if absent.
func (s *Store) Connect(ctx context.Context) error {
	// connect without a database first, so we can CREATE it
	boot, err := s.open("")
	if err != nil {
		return fwerr.DatabaseWrap(err, "cannot connect to ClickHouse at %s:%d", s.Host, s.Port)
	}
	if err := boot.Exec(ctx, fmt.Sprintf("CREATE DATABASE IF NOT EXISTS `%s`", s.Database)); err != nil {
		boot.Close()
		return fwerr.DatabaseWrap(err, "cannot create database %s", s.Database)
	}
	boot.Close()

	conn, err := s.open(s.Database)
	if err != nil {
		return fwerr.DatabaseWrap(err, "cannot connect to ClickHouse at %s:%d", s.Host, s.Port)
	}
	if err := conn.Ping(ctx); err != nil {
		return fwerr.DatabaseWrap(err, "cannot reach ClickHouse at %s:%d", s.Host, s.Port)
	}
	s.conn = conn
	return nil
}

func (s *Store) EnsureTable(ctx context.Context, table string, cols []store.Column, orderBy []string) error {
	if err := s.conn.Exec(ctx, CreateTableDDL(s.Database, table, cols, orderBy)); err != nil {
		return fwerr.DatabaseWrap(err, "failed creating table %s", table)
	}
	return nil
}

func (s *Store) Insert(ctx context.Context, table string, cols []string, rows [][]interface{}) error {
	quoted := make([]string, len(cols))
	for i, c := range cols {
		quoted[i] = "`" + c + "`"
	}
	stmt := fmt.Sprintf("INSERT INTO `%s` (%s)", table, strings.Join(quoted, ", "))
	batch, err := s.conn.PrepareBatch(ctx, stmt)
	if err != nil {
		return fwerr.DatabaseWrap(err, "insert into %s failed (prepare)", table)
	}
	for _, r := range rows {
		if err := batch.Append(r...); err != nil {
			return fwerr.DatabaseWrap(err, "insert into %s failed (append)", table)
		}
	}
	if err := batch.Send(); err != nil {
		return fwerr.DatabaseWrap(err, "insert into %s failed", table)
	}
	return nil
}

func (s *Store) Close() error {
	if s.conn != nil {
		err := s.conn.Close()
		s.conn = nil
		return err
	}
	return nil
}
