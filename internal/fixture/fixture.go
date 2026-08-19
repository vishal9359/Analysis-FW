// Package fixture generates a ProfileData-* tree from real .proto files, with
// full payloads.
//
// It reuses the loader's own schema detection (internal/registry), so a fixture
// can never drift from what the loader expects.
//
// The data is a realistic time series: each record gets an increasing
// per-second timestamp, and the integer fields are monotonically-increasing
// cumulative counters — so per-second delta queries (IOPS, bandwidth, latency)
// produce sensible values, not zeros.
package fixture

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/dynamicpb"

	"github.com/vishal9359/Analysis-FW/internal/registry"
)

// RunName is the fixture's run directory name (also its run_id).
const RunName = "ProfileData-fixture-20260727-180221"

// BaseTime is the first record's timestamp; each record advances one second.
var BaseTime = time.Date(2026, 7, 27, 18, 2, 21, 0, time.UTC)

const (
	Hostname = "spark-e97e"
	Tag      = "test1"
)

// Spec is how many records to write for a unit, across how many .pb files.
type Spec struct {
	Count  int
	Splits int
}

// DefaultCounts mirrors the shape of a real run: one large block unit split
// across two files, smaller units elsewhere.
var DefaultCounts = map[string]Spec{
	"linux_block_1_stats": {1000, 2},
	"linux_block_2_misc":  {400, 1},
	"linux_nvme_1_stats":  {300, 1},
}

// DefaultSpec applies to any unit not named in DefaultCounts.
var DefaultSpec = Spec{200, 1}

// layerDirs maps a token in the file stem to its directory in the run tree.
var layerDirs = map[string]string{
	"block": "Linux/Block", "nvme": "Linux/NVMe", "syscall": "Linux/Syscall",
	"memory": "Linux/Memory", "filesystem": "Linux/Filesystem",
	"platform": "Platform", "ssd": "SSD",
}

// counterStep is the per-second increment for known /proc/diskstats counters,
// chosen so the derived metrics are realistic: ~2 ms/read, ~3 ms/write,
// ~8 sectors (4 KiB) per IO.
var counterStep = map[string]int64{
	"read_ios": 200, "write_ios": 600,
	"sectors_read": 1600, "sectors_written": 4800,
	"read_time_ms": 400, "write_time_ms": 1800,
}

// Result reports what was written for one unit.
type Result struct {
	Stem          string
	Dir           string
	Records       int
	Files         int
	PayloadFields int
}

// Generate writes a full ProfileData-* tree under outputRoot from every .proto
// in protoDir, and returns the run directory plus a per-unit summary.
func Generate(protoDir, outputRoot string, counts map[string]Spec) (string, []Result, error) {
	entries, err := os.ReadDir(protoDir)
	if err != nil {
		return "", nil, fmt.Errorf("cannot read %s: %w", protoDir, err)
	}
	var protos []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".proto") {
			protos = append(protos, filepath.Join(protoDir, e.Name()))
		}
	}
	if len(protos) == 0 {
		return "", nil, fmt.Errorf("no .proto files in %s", protoDir)
	}
	sort.Strings(protos)

	runDir := filepath.Join(outputRoot, RunName)
	if err := os.MkdirAll(filepath.Join(runDir, "Config"), 0o755); err != nil {
		return "", nil, err
	}
	f, err := os.Create(filepath.Join(runDir, "Config", "run_config.log"))
	if err != nil {
		return "", nil, err
	}
	f.Close()

	if counts == nil {
		counts = DefaultCounts
	}

	var results []Result
	for _, protoPath := range protos {
		stem := strings.TrimSuffix(filepath.Base(protoPath), ".proto")
		schema, err := registry.Build(stem, protoPath)
		if err != nil {
			return "", nil, err
		}
		spec, ok := counts[stem]
		if !ok {
			spec = DefaultSpec
		}

		dir := filepath.Join(runDir, filepath.FromSlash(layerDirFor(stem)))
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return "", nil, err
		}
		src, err := os.ReadFile(protoPath)
		if err != nil {
			return "", nil, err
		}
		if err := os.WriteFile(filepath.Join(dir, stem+".proto"), src, 0o644); err != nil {
			return "", nil, err
		}

		per := (spec.Count + spec.Splits - 1) / spec.Splits
		for idx := 0; idx < spec.Splits; idx++ {
			lo := idx * per
			hi := lo + per
			if hi > spec.Count {
				hi = spec.Count
			}
			if lo >= hi {
				continue
			}
			wrapper, err := makeWrapper(schema, hi-lo, lo) // continuous series
			if err != nil {
				return "", nil, err
			}
			blob, err := proto.Marshal(wrapper)
			if err != nil {
				return "", nil, err
			}
			name := stem + ".pb"
			if spec.Splits > 1 {
				name = fmt.Sprintf("%s.%03d.pb", stem, idx)
			}
			if err := os.WriteFile(filepath.Join(dir, name), blob, 0o644); err != nil {
				return "", nil, err
			}
		}
		results = append(results, Result{
			Stem: stem, Dir: layerDirFor(stem), Records: spec.Count,
			Files: spec.Splits, PayloadFields: len(schema.PayloadFields),
		})
	}
	return runDir, results, nil
}

func layerDirFor(stem string) string {
	for _, tok := range strings.Split(stem, "_") {
		if d, ok := layerDirs[tok]; ok {
			return d
		}
	}
	return "Linux/Other"
}

// makeWrapper builds one wrapper holding count records, numbered
// gi = start .. start+count-1, so split files continue one global time series.
func makeWrapper(schema *registry.TableSchema, count, start int) (proto.Message, error) {
	if schema.Wrapper == nil {
		return nil, fmt.Errorf("%s: no wrapper message in the .proto", schema.Stem)
	}
	wrapper := dynamicpb.NewMessage(schema.Wrapper)
	list := wrapper.Mutable(schema.RepeatedField).List()

	for j := 0; j < count; j++ {
		gi := start + j
		rec := list.NewElement().Message()

		g := rec.Mutable(schema.GenericField).Message()
		setString(g, "timestamp", isoTime(gi))
		setString(g, "hostname", Hostname)
		setString(g, "tag", Tag)
		setNumber(g, "component", 1)
		setNumber(g, "log_level", 3)

		p := rec.Mutable(schema.PayloadField).Message()
		for _, path := range schema.PayloadFields {
			setScalarAt(p, path, gi)
		}
		for _, c := range schema.Children {
			sub := p.Mutable(c.RepeatedField).List()
			for k := 0; k < subCount(string(c.RepeatedField.Name()), gi); k++ {
				el := sub.NewElement().Message()
				for idx, path := range c.SubFields {
					if idx == 0 {
						// key dimension (queue_id / cpu_id / io_type)
						m, fd := walkTo(el, path)
						setNumberFd(m, fd, int64(k))
					} else {
						setScalarAt(el, path, gi+k)
					}
				}
				sub.Append(protoreflect.ValueOfMessage(el))
			}
		}
		list.Append(protoreflect.ValueOfMessage(rec))
	}
	return wrapper, nil
}

// isoTime is an RFC 3339 UTC timestamp, one second per record.
func isoTime(gi int) string {
	return BaseTime.Add(time.Duration(gi) * time.Second).Format("2006-01-02T15:04:05.000Z")
}

// counterValue is a monotonically-increasing cumulative counter: linear plus a
// gentle ramp so per-second deltas rise over the run. The ramp cancels in
// latency ratios (read_time/read_ios), so latency stays ~constant while
// IOPS/bandwidth climb.
func counterValue(name string, number int32, gi int) int64 {
	step, ok := counterStep[name]
	if !ok {
		step = int64(number+1) * 50
	}
	g := int64(gi)
	return step*g + step*g*g/2000
}

func subCount(fieldName string, gi int) int {
	switch {
	case strings.Contains(fieldName, "queue"):
		return 3 + gi%4 // 3..6 queues
	case strings.Contains(fieldName, "core"), strings.Contains(fieldName, "cpu"):
		return 8 + gi%5 // 8..12 cores
	default:
		return 2 + gi%3
	}
}

// walkTo descends a FieldPath, creating intermediate singular sub-messages, and
// returns the message plus the leaf field to set.
func walkTo(m protoreflect.Message, path registry.FieldPath) (protoreflect.Message, protoreflect.FieldDescriptor) {
	for i := 0; i < len(path)-1; i++ {
		m = m.Mutable(path[i]).Message()
	}
	return m, path[len(path)-1]
}

// setScalarAt fills the scalar a FieldPath points at, creating any 1:1 nested
// group along the way.
func setScalarAt(m protoreflect.Message, path registry.FieldPath, gi int) {
	target, fd := walkTo(m, path)
	setScalar(target, fd, gi)
}

func setScalar(m protoreflect.Message, fd protoreflect.FieldDescriptor, gi int) {
	switch fd.Kind() {
	case protoreflect.StringKind:
		v := fmt.Sprintf("s%d", gi)
		if string(fd.Name()) == "device" {
			v = "nvme0n1"
		}
		m.Set(fd, protoreflect.ValueOfString(v))
	case protoreflect.BytesKind:
		m.Set(fd, protoreflect.ValueOfBytes([]byte(fmt.Sprintf("s%d", gi))))
	case protoreflect.BoolKind:
		m.Set(fd, protoreflect.ValueOfBool(gi%2 == 0))
	case protoreflect.FloatKind:
		m.Set(fd, protoreflect.ValueOfFloat32(float32(gi%100)+0.5))
	case protoreflect.DoubleKind:
		m.Set(fd, protoreflect.ValueOfFloat64(float64(gi%100)+0.5))
	case protoreflect.EnumKind:
		m.Set(fd, protoreflect.ValueOfEnum(0))
	default:
		setNumber(m, string(fd.Name()), counterValue(string(fd.Name()), int32(fd.Number()), gi))
	}
}

func setString(m protoreflect.Message, name, v string) {
	fd := m.Descriptor().Fields().ByName(protoreflect.Name(name))
	if fd == nil || fd.Kind() != protoreflect.StringKind {
		return
	}
	m.Set(fd, protoreflect.ValueOfString(v))
}

// setNumber writes v into a numeric field, matching the field's exact kind.
func setNumber(m protoreflect.Message, name string, v int64) {
	fd := m.Descriptor().Fields().ByName(protoreflect.Name(name))
	if fd == nil {
		return
	}
	setNumberFd(m, fd, v)
}

func setNumberFd(m protoreflect.Message, fd protoreflect.FieldDescriptor, v int64) {
	switch fd.Kind() {
	case protoreflect.Int32Kind, protoreflect.Sint32Kind, protoreflect.Sfixed32Kind:
		m.Set(fd, protoreflect.ValueOfInt32(int32(v)))
	case protoreflect.Int64Kind, protoreflect.Sint64Kind, protoreflect.Sfixed64Kind:
		m.Set(fd, protoreflect.ValueOfInt64(v))
	case protoreflect.Uint32Kind, protoreflect.Fixed32Kind:
		m.Set(fd, protoreflect.ValueOfUint32(uint32(v)))
	case protoreflect.Uint64Kind, protoreflect.Fixed64Kind:
		m.Set(fd, protoreflect.ValueOfUint64(uint64(v)))
	case protoreflect.FloatKind:
		m.Set(fd, protoreflect.ValueOfFloat32(float32(v)))
	case protoreflect.DoubleKind:
		m.Set(fd, protoreflect.ValueOfFloat64(float64(v)))
	}
}
