// Command analysis-fw loads a ProfileData-* directory into the configured
// database.
//
// Usage:
//
//	analysis-fw <input_dir>                        # one ProfileData-* run
//	analysis-fw <parent_dir>                       # batch: many runs (auto-detect)
//	analysis-fw --batch <parent_dir>               # batch: force
//	analysis-fw --batch --continue-on-error <dir>
//
// A single run directory holds one ProfileData-<tag>-<timestamp> run. A parent
// directory holds several ProfileData-* runs; batch mode loads each
// sequentially (each run still parallelizes its own units). Default is
// fail-fast; add --continue-on-error to load every run and report failures at
// the end.
//
// Exit codes tell the caller which failure class occurred: 0 ok, 2 config,
// 3 input, 4 schema, 5 database (retryable), 6 integrity.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/vishal9359/Analysis-FW/internal/config"
	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/runner"
)

// Version is the loader version, overridable at build time with
// -ldflags "-X main.Version=...".
var Version = "0.1.0"

func main() {
	os.Exit(run())
}

func run() int {
	var (
		batch           = flag.Bool("batch", false, "treat input as a parent of ProfileData-* runs (auto-detected if not set)")
		continueOnError = flag.Bool("continue-on-error", false, "batch: keep loading remaining runs after one fails (default: stop at the first failure)")
		configPath      = flag.String("config", "", "path to config.yaml (default: config/config.yaml)")
		showVersion     = flag.Bool("version", false, "print version and exit")
	)
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr,
			"analysis-fw — load a ProfileData-* directory into the configured database.\n\n"+
				"Usage:\n  analysis-fw [flags] <input_dir>\n\nFlags:\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	if *showVersion {
		fmt.Println(Version)
		return fwerr.ExitOK
	}
	if flag.NArg() != 1 {
		flag.Usage()
		return fwerr.ExitInput
	}
	inputDir := flag.Arg(0)

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		return fwerr.ExitCode(err)
	}

	log := newLogger(cfg.LogLevel, cfg.LogFormat)

	fi, statErr := os.Stat(inputDir)
	if statErr != nil || !fi.IsDir() {
		log.Error("input path is not a directory: %s", inputDir)
		return fwerr.ExitInput
	}

	ctx := context.Background()
	isBatch := *batch || len(runner.FindRuns(inputDir)) > 0
	if isBatch {
		return runBatch(ctx, inputDir, cfg, *continueOnError, log)
	}
	return runSingle(ctx, inputDir, cfg, log)
}

func runSingle(ctx context.Context, dir string, cfg *config.Config, log *logger) int {
	log.Info("loading %s -> %s (workers=%d)", dir, cfg.Store.Database, cfg.Store.Workers)
	report, err := runner.RunLoad(ctx, dir, cfg, nil)
	if err != nil {
		log.Error("%v", err)
		return fwerr.ExitCode(err)
	}
	printJSON(report.ToMap())
	if report.Status != "complete" {
		for _, u := range report.Units {
			if !u.OK() {
				log.Error("%s: %d records read but %d rows in the main table",
					u.Stem, u.Records, u.TableRows[u.Stem])
			}
		}
		return fwerr.ExitIntegrity
	}
	tables := 0
	for _, u := range report.Units {
		tables += len(u.TableRows)
	}
	log.Info("done: %d rows across %d tables", report.Rows(), tables)
	return fwerr.ExitOK
}

func runBatch(ctx context.Context, dir string, cfg *config.Config, continueOnError bool, log *logger) int {
	runs := runner.FindRuns(dir)
	log.Info("batch: %d run(s) under %s -> %s (continue_on_error=%v)",
		len(runs), dir, cfg.Store.Database, continueOnError)

	batch, err := runner.RunBatch(ctx, dir, cfg, nil, continueOnError)
	if err != nil {
		log.Error("%v", err)
		return fwerr.ExitCode(err)
	}
	m := batch.ToMap()
	printJSON(m)
	if batch.ExitCode() != fwerr.ExitOK {
		log.Error("batch finished with failures: %v", m["totals"])
	} else {
		log.Info("batch complete: %v", m["totals"])
	}
	return batch.ExitCode()
}

func printJSON(v interface{}) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot render report: %v\n", err)
		return
	}
	fmt.Println(string(b))
}
