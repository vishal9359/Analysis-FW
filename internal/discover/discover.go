// Package discover walks a run directory and pairs each <stem>.proto with its
// <stem>.pb data file(s).
//
// A layer folder holds one or more types, each a .proto plus its data:
//
//	<layer>_<type>.proto        schema
//	<layer>_<type>.pb           data (single file)
//	<layer>_<type>.<NNN>.pb     data (split parts, in order 000, 001, ...)
//
// Config/ is ignored.
package discover

import (
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
)

// Unit is one <layer>_<type>: a schema and the data files that use it.
type Unit struct {
	Stem      string
	ProtoPath string
	PBParts   []string // ordered: single .pb, or split parts 000, 001, ...
}

// Discover returns the units under a run directory, stem-sorted.
func Discover(root string) ([]Unit, error) {
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return nil, fwerr.Input("input path is not a directory: %s", root)
	}

	protos := map[string]string{} // stem -> .proto path
	pbs := map[string][]string{}  // stem -> .pb paths

	err = filepath.Walk(root, func(path string, fi os.FileInfo, err error) error {
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
		switch {
		case strings.HasSuffix(name, ".proto"):
			protos[strings.TrimSuffix(name, ".proto")] = path
		case strings.HasSuffix(name, ".pb"):
			// "<stem>.pb" or "<stem>.<NNN>.pb"
			base := strings.TrimSuffix(name, ".pb")
			stem := base
			if i := strings.LastIndex(base, "."); i > 0 {
				if suffix := base[i+1:]; isAllDigits(suffix) {
					stem = base[:i]
				}
			}
			pbs[stem] = append(pbs[stem], path)
		}
		return nil
	})
	if err != nil {
		return nil, fwerr.InputWrap(err, "cannot walk %s", root)
	}

	var units []Unit
	for stem, proto := range protos {
		parts := pbs[stem]
		if len(parts) == 0 {
			continue // a .proto with no data is not a unit
		}
		sort.Strings(parts) // 000, 001, ... sorts correctly
		units = append(units, Unit{Stem: stem, ProtoPath: proto, PBParts: parts})
	}
	sort.Slice(units, func(i, j int) bool { return units[i].Stem < units[j].Stem })

	if len(units) == 0 {
		return nil, fwerr.Input("no <stem>.proto + <stem>.pb pairs found under %s", root)
	}
	return units, nil
}

func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
