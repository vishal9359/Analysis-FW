// Package discover finds the load units in a ProfileData-* directory.
//
// A *unit* is one <layer>_<type> stem: its .proto schema plus its ordered .pb
// data parts. Naming convention:
//
//	<layer>_<type>.proto        schema (one message)
//	<layer>_<type>.pb           data, single file
//	<layer>_<type>.<NNN>.pb     data, split parts in order 000, 001, ...
//
// Config/ is ignored — run_config.log is not a data layer.
//
// Discovery is driven by the DATA files: every .pb must have its .proto beside
// it, and split parts must be a dense 000..N-1 run. Both are integrity guards —
// data with no schema, or a missing split part, would otherwise load silently
// and short.
package discover

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
)

// <stem>.pb  or  <stem>.<NNN>.pb   (NNN = split index)
var splitRe = regexp.MustCompile(`^(.+?)(?:\.(\d+))?\.pb$`)

// Unit is one <layer>_<type>: a schema and the data files that use it.
type Unit struct {
	Stem      string // e.g. "linux_block_1_stats" — becomes the table name
	LayerDir  string // e.g. "Linux/Block" — for diagnostics
	ProtoPath string
	PBParts   []string // ordered: 000, 001, ... (or a single file)
}

type group struct {
	parts []string
	idxs  []int
	dir   string
}

// splitIndex is the NNN of a split part, or -1 for a single file (which sorts
// first).
func splitIndex(name string) int {
	m := splitRe.FindStringSubmatch(name)
	if m == nil || m[2] == "" {
		return -1
	}
	n, err := strconv.Atoi(m[2])
	if err != nil {
		return -1
	}
	return n
}

// Discover returns the units under a run directory, stem-sorted.
func Discover(root string) ([]Unit, error) {
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return nil, fwerr.Input("input path is not a directory: %s", root)
	}

	// group .pb files by stem, remembering their directory
	groups := map[string]*group{}
	walkErr := filepath.Walk(root, func(path string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if fi.IsDir() {
			if fi.Name() == "Config" {
				return filepath.SkipDir
			}
			return nil
		}
		name := fi.Name()
		if !strings.HasSuffix(name, ".pb") {
			return nil
		}
		m := splitRe.FindStringSubmatch(name)
		if m == nil {
			return fwerr.Input("unexpected .pb name (not <stem>[.NNN].pb): %s", name)
		}
		stem := m[1]
		g, ok := groups[stem]
		if !ok {
			g = &group{dir: filepath.Dir(path)}
			groups[stem] = g
		}
		g.parts = append(g.parts, path)
		g.idxs = append(g.idxs, splitIndex(name))
		return nil
	})
	if walkErr != nil {
		if fe, ok := walkErr.(*fwerr.Error); ok {
			return nil, fe
		}
		return nil, fwerr.InputWrap(walkErr, "cannot walk %s", root)
	}

	if len(groups) == 0 {
		return nil, fwerr.Input("no .pb data files found under %s", root)
	}

	stems := make([]string, 0, len(groups))
	for stem := range groups {
		stems = append(stems, stem)
	}
	sort.Strings(stems)

	units := make([]Unit, 0, len(stems))
	for _, stem := range stems {
		g := groups[stem]

		// order parts by split index (single file = -1, sorts first)
		sort.Slice(g.parts, func(i, j int) bool {
			return splitIndex(filepath.Base(g.parts[i])) < splitIndex(filepath.Base(g.parts[j]))
		})

		proto := filepath.Join(g.dir, stem+".proto")
		if fi, err := os.Stat(proto); err != nil || fi.IsDir() {
			return nil, fwerr.Input(
				"%s: found data files but no schema '%s.proto' beside them", stem, stem)
		}

		// split indices must be a dense 0..N-1 run (a gap means a missing part)
		if len(g.parts) > 1 {
			got := append([]int(nil), g.idxs...)
			sort.Ints(got)
			for i, v := range got {
				if v != i {
					return nil, fwerr.Input(
						"%s: split parts are not contiguous 000..%03d (got %v)",
						stem, len(g.parts)-1, got)
				}
			}
		}

		layerDir, err := filepath.Rel(root, g.dir)
		if err != nil {
			layerDir = g.dir
		}
		units = append(units, Unit{
			Stem:      stem,
			LayerDir:  filepath.ToSlash(layerDir),
			ProtoPath: proto,
			PBParts:   g.parts,
		})
	}
	return units, nil
}
