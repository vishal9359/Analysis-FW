// Package memory is an in-memory store — it proves the seam and lets the whole
// pipeline run in tests without a database. Not for production use.
package memory

import (
	"context"
	"sync"
	"time"

	"github.com/vishal9359/Analysis-FW/internal/store"
)

// Store keeps every inserted row in memory. Safe for concurrent use, because
// the runner loads units in parallel.
type Store struct {
	mu        sync.Mutex
	Tables    map[string][][]interface{}
	Columns   map[string][]store.Column
	OrderBy   map[string][]string
	Connected bool
}

// New builds an empty in-memory store.
func New() *Store {
	return &Store{
		Tables:  map[string][][]interface{}{},
		Columns: map[string][]store.Column{},
		OrderBy: map[string][]string{},
	}
}

// TimestampRange is unbounded — memory has no storage limit.
func (s *Store) TimestampRange() [2]time.Time { return store.WidestTimestampRange }

func (s *Store) Connect(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Connected = true
	return nil
}

func (s *Store) EnsureTable(ctx context.Context, table string, cols []store.Column, orderBy []string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Columns[table] = append([]store.Column(nil), cols...)
	s.OrderBy[table] = append([]string(nil), orderBy...)
	if _, ok := s.Tables[table]; !ok {
		s.Tables[table] = nil
	}
	return nil
}

func (s *Store) Insert(ctx context.Context, table string, cols []string, rows [][]interface{}) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, r := range rows {
		s.Tables[table] = append(s.Tables[table], append([]interface{}(nil), r...))
	}
	return nil
}

// Count is a test helper — deliberately NOT part of store.Store. Reconciliation
// counts come from the loader (worker.UnitResult); a DB-side count belongs with
// the future reload coordinator (ADR-0006).
func (s *Store) Count(table string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.Tables[table])
}

// Row is a test helper returning one row as a name->value map.
func (s *Store) Row(table string, i int) map[string]interface{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]interface{}{}
	if i >= len(s.Tables[table]) {
		return out
	}
	for j, c := range s.Columns[table] {
		if j < len(s.Tables[table][i]) {
			out[c.Name] = s.Tables[table][i][j]
		}
	}
	return out
}

func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Connected = false
	return nil
}
