// Package runner runs a load: discover units, build schemas, load each unit,
// summarize.
//
// Concurrency is a bounded goroutine pool sharing one connection — Go has no
// GIL, so unlike the process-pool design this replaced, the schema is compiled
// once and shared, and there is no per-worker connection or serialization step.
//
// There is no coordinator: units are independent append-only jobs, so the pool
// is a plain fan-out with no cross-unit state.
package runner

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"golang.org/x/sync/errgroup"

	"github.com/vishal9359/Analysis-FW/internal/config"
	"github.com/vishal9359/Analysis-FW/internal/discover"
	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/registry"
	"github.com/vishal9359/Analysis-FW/internal/store"
	"github.com/vishal9359/Analysis-FW/internal/store/factory"
	"github.com/vishal9359/Analysis-FW/internal/worker"
)

// RunPrefix marks a run directory.
const RunPrefix = "ProfileData-"

// LoadReport summarizes one run.
type LoadReport struct {
	RunID    string              `json:"run_id"`
	Producer string              `json:"producer"`
	Database string              `json:"database"`
	Status   string              `json:"status"`
	Units    []worker.UnitResult `json:"-"`
}

// Records is every parent record read across units.
func (r LoadReport) Records() int {
	n := 0
	for _, u := range r.Units {
		n += u.Records
	}
	return n
}

// Rows is every row written across units and tables.
func (r LoadReport) Rows() int {
	n := 0
	for _, u := range r.Units {
		n += u.TotalRows()
	}
	return n
}

// ToMap renders the report as the CLI's JSON summary.
func (r LoadReport) ToMap() map[string]interface{} {
	units := make([]map[string]interface{}, 0, len(r.Units))
	for _, u := range r.Units {
		units = append(units, map[string]interface{}{
			"stem": u.Stem, "files": u.Files, "records": u.Records,
			"tables": u.TableRows, "ok": u.OK(),
		})
	}
	return map[string]interface{}{
		"status": r.Status, "run_id": r.RunID, "producer": r.Producer,
		"database": r.Database,
		"totals": map[string]interface{}{
			"records": r.Records(), "rows": r.Rows(), "units": len(r.Units),
		},
		"units": units,
	}
}

// RunLoad discovers and loads one run. If st is non-nil it is used as-is (tests
// inject a memory store); otherwise the configured store is built and connected.
func RunLoad(ctx context.Context, inputDir string, cfg *config.Config, st store.Store) (LoadReport, error) {
	abs, err := filepath.Abs(inputDir)
	if err != nil {
		return LoadReport{}, fwerr.InputWrap(err, "cannot resolve %s", inputDir)
	}
	runID := filepath.Base(abs)

	report := LoadReport{
		RunID: runID, Producer: cfg.ProducerName,
		Database: cfg.Store.Database, Status: "complete",
	}

	units, err := discover.Discover(inputDir)
	if err != nil {
		return report, err
	}

	schemas := make(map[string]*registry.TableSchema, len(units))
	for _, u := range units {
		s, err := registry.Build(u.Stem, u.ProtoPath)
		if err != nil {
			return report, err
		}
		schemas[u.Stem] = s
	}

	own := st == nil
	if own {
		st, err = factory.New(cfg.Store.Kind, cfg.Store.Host, cfg.Store.Port,
			cfg.Store.Database, cfg.Store.Options)
		if err != nil {
			return report, err
		}
		if err := st.Connect(ctx); err != nil {
			return report, err
		}
		defer st.Close()
	}

	results := make([]worker.UnitResult, len(units))
	var mu sync.Mutex

	g, gctx := errgroup.WithContext(ctx)
	limit := cfg.Store.Workers
	if limit > len(units) {
		limit = len(units)
	}
	if limit < 1 {
		limit = 1
	}
	sem := make(chan struct{}, limit)

	for i, u := range units {
		i, u := i, u
		g.Go(func() error {
			select {
			case sem <- struct{}{}:
			case <-gctx.Done():
				return gctx.Err()
			}
			defer func() { <-sem }()

			res, err := worker.LoadUnit(gctx, u.PBParts, schemas[u.Stem], st,
				runID, cfg.Store.BatchSize)
			if err != nil {
				return err
			}
			mu.Lock()
			results[i] = res
			mu.Unlock()
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return report, err
	}

	report.Units = results
	return reconcile(report), nil
}

// reconcile marks a run failed if any unit's main-table rows != records read.
func reconcile(r LoadReport) LoadReport {
	for _, u := range r.Units {
		if !u.OK() {
			r.Status = "failed"
			return r
		}
	}
	return r
}

// --------------------------------------------------------------------------
// batch: load every ProfileData-* run under a parent directory, sequentially
// --------------------------------------------------------------------------

// FindRuns returns the ProfileData-* directories directly under parent,
// name-sorted. Empty if there are none (i.e. parent is itself a single run).
func FindRuns(parent string) []string {
	entries, err := os.ReadDir(parent)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), RunPrefix) {
			out = append(out, filepath.Join(parent, e.Name()))
		}
	}
	sort.Strings(out)
	return out
}

// RunOutcome is one run's result inside a batch.
type RunOutcome struct {
	RunID    string
	Status   string // "complete" | "failed" | "skipped"
	ExitCode int
	Error    string
	Report   map[string]interface{}
}

// BatchReport summarizes a batch of runs.
type BatchReport struct {
	Parent   string
	Outcomes []RunOutcome
}

// ExitCode is the first failing run's code (0 if none failed).
func (b BatchReport) ExitCode() int {
	for _, o := range b.Outcomes {
		if o.Status == "failed" {
			return o.ExitCode
		}
	}
	return fwerr.ExitOK
}

// Status is "complete" only when every run completed.
func (b BatchReport) Status() string {
	for _, o := range b.Outcomes {
		if o.Status != "complete" {
			return "failed"
		}
	}
	return "complete"
}

// ToMap renders the batch as the CLI's JSON summary.
func (b BatchReport) ToMap() map[string]interface{} {
	counts := map[string]int{"complete": 0, "failed": 0, "skipped": 0}
	runs := make([]map[string]interface{}, 0, len(b.Outcomes))
	for _, o := range b.Outcomes {
		counts[o.Status]++
		m := map[string]interface{}{"run_id": o.RunID, "status": o.Status}
		if o.Status == "failed" {
			m["exit_code"] = o.ExitCode
			m["error"] = o.Error
		}
		if o.Report != nil {
			m["totals"] = o.Report["totals"]
		}
		runs = append(runs, m)
	}
	return map[string]interface{}{
		"mode": "batch", "parent": b.Parent, "status": b.Status(),
		"totals": map[string]interface{}{
			"runs": len(b.Outcomes), "complete": counts["complete"],
			"failed": counts["failed"], "skipped": counts["skipped"],
		},
		"runs": runs,
	}
}

// RunBatch loads each ProfileData-* run under parent, sequentially (each run
// still parallelizes its own units). Default is fail-fast: the first failed run
// stops the batch and the rest are marked skipped. With continueOnError every
// run is attempted and failures are collected. A run "fails" on a raised error
// OR a reconciliation mismatch.
func RunBatch(ctx context.Context, parent string, cfg *config.Config, st store.Store,
	continueOnError bool) (BatchReport, error) {

	runDirs := FindRuns(parent)
	if len(runDirs) == 0 {
		return BatchReport{}, fwerr.Input("no %s* directories under %s", RunPrefix, parent)
	}

	batch := BatchReport{Parent: parent}
	stop := false
	for _, dir := range runDirs {
		name := filepath.Base(dir)
		if stop {
			batch.Outcomes = append(batch.Outcomes, RunOutcome{RunID: name, Status: "skipped"})
			continue
		}
		report, err := RunLoad(ctx, dir, cfg, st)
		if err != nil {
			batch.Outcomes = append(batch.Outcomes, RunOutcome{
				RunID: name, Status: "failed",
				ExitCode: fwerr.ExitCode(err), Error: err.Error(),
			})
			stop = !continueOnError
			continue
		}
		if report.Status != "complete" {
			var bad []string
			for _, u := range report.Units {
				if !u.OK() {
					bad = append(bad, u.Stem)
				}
			}
			batch.Outcomes = append(batch.Outcomes, RunOutcome{
				RunID: name, Status: "failed", ExitCode: fwerr.ExitIntegrity,
				Error:  fmt.Sprintf("reconciliation mismatch in: %s", strings.Join(bad, ", ")),
				Report: report.ToMap(),
			})
			stop = !continueOnError
			continue
		}
		batch.Outcomes = append(batch.Outcomes, RunOutcome{
			RunID: name, Status: "complete", Report: report.ToMap(),
		})
	}
	return batch, nil
}
