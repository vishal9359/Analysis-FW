// Package registry compiles a producer's .proto at runtime and derives its
// table schema — generically.
//
// Each .proto defines, by convention:
//   - GenericFormat : the common header fields (timestamp, hostname, ...).
//   - a payload msg : the layer-specific fields.
//   - a record msg  : { GenericFormat generic_format = 1; <Payload> payload = 2; }
//     -- one row of data.
//   - a wrapper msg : { repeated <record> <name> = 1; }
//     -- what a .pb file actually contains (no delimiters needed; the repeated
//     field structures the records internally).
//
// The loader identifies these by STRUCTURE and the agreed field names, not by
// message name, so a new field, a renamed payload message, or a new layer needs
// no code change. The .proto is parsed in pure Go — no protoc binary is
// required at build or run time.
package registry

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"path/filepath"

	"github.com/bufbuild/protocompile"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/descriptorpb"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/store"
)

// The agreed contract with Profile FW: the record message has a singular
// message field named generic_format (the header, of type GenericFormat) and a
// singular message field named payload (the layer data). Both names are
// required; a .proto that does not follow this is rejected.
const (
	GenericField   = "generic_format"
	PayloadField   = "payload"
	GenericMessage = "GenericFormat"
)

// protobuf field kind -> neutral column type
var protoToType = map[protoreflect.Kind]store.ColumnType{
	protoreflect.StringKind:   store.TypeString,
	protoreflect.BytesKind:    store.TypeString,
	protoreflect.BoolKind:     store.TypeBool,
	protoreflect.Int32Kind:    store.TypeInt32,
	protoreflect.Sint32Kind:   store.TypeInt32,
	protoreflect.Sfixed32Kind: store.TypeInt32,
	protoreflect.Int64Kind:    store.TypeInt64,
	protoreflect.Sint64Kind:   store.TypeInt64,
	protoreflect.Sfixed64Kind: store.TypeInt64,
	protoreflect.Uint32Kind:   store.TypeUint32,
	protoreflect.Fixed32Kind:  store.TypeUint32,
	protoreflect.Uint64Kind:   store.TypeUint64,
	protoreflect.Fixed64Kind:  store.TypeUint64,
	protoreflect.FloatKind:    store.TypeFloat32,
	protoreflect.DoubleKind:   store.TypeFloat64,
	protoreflect.EnumKind:     store.TypeInt32,
}

// Loader-added bookkeeping columns (not from the proto).
var (
	leadingColumns = []store.Column{
		{Name: "run_id", Type: store.TypeString},
		{Name: "ts", Type: store.TypeDateTime},
	}
	trailingColumns = []store.Column{
		{Name: "_loaded_at", Type: store.TypeDateTime},
	}
	// Correlation key, added to the main + child tables ONLY when a payload has
	// repeated sub-messages (i.e. child tables exist). A 64-bit sequence
	// assigned per record by the loader; links a main row to its child rows.
	// (Deferred to re-architecture: a multi-node-safe scheme.)
	recordIDColumn = store.Column{Name: "record_id", Type: store.TypeUint64}
)

// ChildSchema is a table for one `repeated <Message>` field in the payload
// (e.g. per_queue). One row per sub-element per record, carrying the header and
// record_id.
type ChildSchema struct {
	Table         string                       // <stem>_<repeated_field>
	RepeatedField protoreflect.FieldDescriptor // payload field to iterate
	SubFields     []protoreflect.FieldDescriptor
	Columns       []store.Column
	OrderBy       []string
}

// TableSchema is everything needed to read one unit and write its rows.
type TableSchema struct {
	Stem          string // == main table name
	Record        protoreflect.MessageDescriptor
	Wrapper       protoreflect.MessageDescriptor
	RepeatedField protoreflect.FieldDescriptor // the repeated field inside the wrapper
	GenericField  protoreflect.FieldDescriptor // the GenericFormat field on the record
	PayloadField  protoreflect.FieldDescriptor // the payload field on the record
	TimestampFd   protoreflect.FieldDescriptor // generic_format.timestamp
	GenericFields []protoreflect.FieldDescriptor
	PayloadFields []protoreflect.FieldDescriptor // SCALAR payload fields
	Columns       []store.Column                 // main table columns, in row order
	OrderBy       []string
	HasRecordID   bool
	Children      []ChildSchema
	Descriptor    []byte // serialized FileDescriptorSet — portable, for workers
	SchemaVersion string
}

// Build compiles protoPath and derives the schema for a unit.
func Build(stem, protoPath string) (*TableSchema, error) {
	dir := filepath.Dir(protoPath)
	name := filepath.Base(protoPath)
	c := protocompile.Compiler{
		Resolver: protocompile.WithStandardImports(
			&protocompile.SourceResolver{ImportPaths: []string{dir}}),
		SourceInfoMode: protocompile.SourceInfoNone,
	}
	files, err := c.Compile(context.Background(), name)
	if err != nil {
		return nil, fwerr.SchemaWrap(err, "%s: failed to compile", name)
	}
	fd := files[0]

	// Serialize to a FileDescriptorSet so workers (and a future streaming
	// module) can rebuild the schema from bytes alone.
	fdset := &descriptorpb.FileDescriptorSet{}
	seen := map[string]bool{}
	var collect func(f protoreflect.FileDescriptor)
	collect = func(f protoreflect.FileDescriptor) {
		if seen[f.Path()] {
			return
		}
		seen[f.Path()] = true
		imports := f.Imports()
		for i := 0; i < imports.Len(); i++ {
			collect(imports.Get(i).FileDescriptor)
		}
		fdset.File = append(fdset.File, protodesc.ToFileDescriptorProto(f))
	}
	collect(fd)

	return buildFrom(stem, fd, fdset, name)
}

// BuildFromDescriptor rebuilds a schema from serialized descriptor bytes — the
// portable form used to hand a schema to a worker without re-reading the
// .proto from disk.
func BuildFromDescriptor(stem string, descriptorBytes []byte, source string) (*TableSchema, error) {
	fdset := &descriptorpb.FileDescriptorSet{}
	if err := protoUnmarshal(descriptorBytes, fdset); err != nil {
		return nil, fwerr.SchemaWrap(err, "%s: cannot parse descriptor set", source)
	}
	files, err := protodesc.NewFiles(fdset)
	if err != nil {
		return nil, fwerr.SchemaWrap(err, "%s: cannot build descriptors", source)
	}
	var primary protoreflect.FileDescriptor
	files.RangeFiles(func(f protoreflect.FileDescriptor) bool {
		if filepath.Base(f.Path()) == source {
			primary = f
			return false
		}
		primary = f // fallback: last file
		return true
	})
	if primary == nil {
		return nil, fwerr.Schema("%s: descriptor set is empty", source)
	}
	return buildFrom(stem, primary, fdset, source)
}

func buildFrom(stem string, fd protoreflect.FileDescriptor,
	fdset *descriptorpb.FileDescriptorSet, source string) (*TableSchema, error) {

	rec, gField, pField, wrap, repField, err := detect(fd, source)
	if err != nil {
		return nil, err
	}

	genericNames, genericCols, err := scalarColumns(gField.Message(), source)
	if err != nil {
		return nil, err
	}
	payloadNames, payloadCols, repeated, err := splitPayload(pField.Message(), source)
	if err != nil {
		return nil, err
	}

	tsFd := gField.Message().Fields().ByName("timestamp")
	if tsFd == nil {
		return nil, fwerr.Schema("%s: %s has no 'timestamp' field", source, GenericMessage)
	}

	hasChildren := len(repeated) > 0
	main := append([]store.Column{}, leadingColumns...)
	if hasChildren {
		main = append(main, recordIDColumn)
	}
	main = append(main, genericCols...)
	main = append(main, payloadCols...)
	main = append(main, trailingColumns...)

	var children []ChildSchema
	for _, f := range repeated {
		subNames, subCols, err := scalarColumns(f.Message(), source)
		if err != nil {
			return nil, err
		}
		if len(subNames) == 0 {
			return nil, fwerr.Schema("%s: payload.%s sub-message has no fields", source, f.Name())
		}
		cc := append([]store.Column{}, leadingColumns...)
		cc = append(cc, recordIDColumn)
		cc = append(cc, genericCols...)
		cc = append(cc, subCols...)
		cc = append(cc, trailingColumns...)
		if err := checkUnique(cc, fmt.Sprintf("%s:%s", source, f.Name())); err != nil {
			return nil, err
		}
		// convention: the sub-message's first field is its key dimension
		keyDim := string(subNames[0].Name())
		children = append(children, ChildSchema{
			Table:         fmt.Sprintf("%s_%s", stem, f.Name()),
			RepeatedField: f,
			SubFields:     subNames,
			Columns:       cc,
			OrderBy:       []string{"run_id", "hostname", keyDim, "ts"},
		})
	}

	if err := checkUnique(main, source); err != nil {
		return nil, err
	}

	raw, err := protoMarshal(fdset)
	if err != nil {
		return nil, fwerr.SchemaWrap(err, "%s: cannot serialize descriptors", source)
	}
	sum := sha256.Sum256(raw)

	return &TableSchema{
		Stem:          stem,
		Record:        rec,
		Wrapper:       wrap,
		RepeatedField: repField,
		GenericField:  gField,
		PayloadField:  pField,
		TimestampFd:   tsFd,
		GenericFields: genericNames,
		PayloadFields: payloadNames,
		Columns:       main,
		OrderBy:       []string{"run_id", "hostname", "ts"},
		HasRecordID:   hasChildren,
		Children:      children,
		Descriptor:    raw,
		SchemaVersion: "sha256:" + hex.EncodeToString(sum[:])[:16],
	}, nil
}

// detect finds the record message and, if present, the wrapper message.
//
// The record message is identified by the agreed field NAMES: it has a singular
// message field named generic_format (header) and a singular message field
// named payload (data). Both are required.
func detect(fd protoreflect.FileDescriptor, source string) (
	rec protoreflect.MessageDescriptor,
	gField, pField protoreflect.FieldDescriptor,
	wrap protoreflect.MessageDescriptor,
	repField protoreflect.FieldDescriptor,
	err error) {

	msgs := fd.Messages()
	for i := 0; i < msgs.Len(); i++ {
		desc := msgs.Get(i)
		g := desc.Fields().ByName(GenericField)
		if g == nil {
			continue // not a record message
		}
		// this message carries generic_format, so it must be a valid record
		if g.Kind() != protoreflect.MessageKind || g.IsList() {
			return nil, nil, nil, nil, nil, fwerr.Schema(
				"%s: %s.%s must be a singular message field", source, desc.Name(), GenericField)
		}
		if string(g.Message().Name()) != GenericMessage {
			return nil, nil, nil, nil, nil, fwerr.Schema(
				"%s: %s.%s must be of type %s, got %s",
				source, desc.Name(), GenericField, GenericMessage, g.Message().Name())
		}
		p := desc.Fields().ByName(PayloadField)
		if p == nil {
			return nil, nil, nil, nil, nil, fwerr.Schema(
				"%s: record message %s has '%s' but no '%s' field. The contract "+
					"requires both a '%s' and a '%s' message field.",
				source, desc.Name(), GenericField, PayloadField, GenericField, PayloadField)
		}
		if p.Kind() != protoreflect.MessageKind || p.IsList() {
			return nil, nil, nil, nil, nil, fwerr.Schema(
				"%s: %s.%s must be a singular message field", source, desc.Name(), PayloadField)
		}
		if rec != nil {
			return nil, nil, nil, nil, nil, fwerr.Schema(
				"%s: more than one record message (both %s and %s have a '%s' field); expected one",
				source, rec.Name(), desc.Name(), GenericField)
		}
		rec, gField, pField = desc, g, p
	}

	if rec == nil {
		return nil, nil, nil, nil, nil, fwerr.Schema(
			"%s: no record message found. The contract requires a message with a "+
				"'%s' field (header) and a '%s' field (data).", source, GenericField, PayloadField)
	}

	// wrapper = a message with a `repeated <record>` field
	for i := 0; i < msgs.Len() && wrap == nil; i++ {
		desc := msgs.Get(i)
		for j := 0; j < desc.Fields().Len(); j++ {
			f := desc.Fields().Get(j)
			if f.IsList() && f.Kind() == protoreflect.MessageKind &&
				f.Message().FullName() == rec.FullName() {
				wrap, repField = desc, f
				break
			}
		}
	}
	return rec, gField, pField, wrap, repField, nil
}

// scalarColumns maps a message's scalar fields to columns. Rejects repeated and
// nested fields — used for the header (GenericFormat) and for a repeated
// sub-message's own fields, both of which must be flat scalars.
func scalarColumns(msg protoreflect.MessageDescriptor, source string) (
	[]protoreflect.FieldDescriptor, []store.Column, error) {

	var fds []protoreflect.FieldDescriptor
	var cols []store.Column
	for i := 0; i < msg.Fields().Len(); i++ {
		f := msg.Fields().Get(i)
		if f.IsList() || f.IsMap() {
			return nil, nil, fwerr.Schema(
				"%s: %s.%s is repeated (this message's fields must be scalars)",
				source, msg.Name(), f.Name())
		}
		if f.Kind() == protoreflect.MessageKind || f.Kind() == protoreflect.GroupKind {
			return nil, nil, fwerr.Schema(
				"%s: %s.%s is a nested message (this message's fields must be scalars)",
				source, msg.Name(), f.Name())
		}
		ct, ok := protoToType[f.Kind()]
		if !ok {
			return nil, nil, fwerr.Schema("%s: %s.%s has unsupported protobuf type %s",
				source, msg.Name(), f.Name(), f.Kind())
		}
		fds = append(fds, f)
		cols = append(cols, store.Column{Name: string(f.Name()), Type: ct})
	}
	return fds, cols, nil
}

// splitPayload separates a payload into scalar fields (-> main table) and
// repeated sub-messages (-> child tables).
func splitPayload(payload protoreflect.MessageDescriptor, source string) (
	[]protoreflect.FieldDescriptor, []store.Column, []protoreflect.FieldDescriptor, error) {

	var scalarFds []protoreflect.FieldDescriptor
	var scalarCols []store.Column
	var repeated []protoreflect.FieldDescriptor

	for i := 0; i < payload.Fields().Len(); i++ {
		f := payload.Fields().Get(i)
		if f.IsList() {
			if f.Kind() != protoreflect.MessageKind {
				return nil, nil, nil, fwerr.Schema(
					"%s: payload.%s is a repeated scalar; only repeated messages "+
						"(which become child tables) are supported", source, f.Name())
			}
			repeated = append(repeated, f) // -> its own child table
			continue
		}
		if f.IsMap() || f.Kind() == protoreflect.MessageKind || f.Kind() == protoreflect.GroupKind {
			return nil, nil, nil, fwerr.Schema(
				"%s: payload.%s is a singular nested message; payload fields must be "+
					"scalars, or repeated messages for child tables", source, f.Name())
		}
		ct, ok := protoToType[f.Kind()]
		if !ok {
			return nil, nil, nil, fwerr.Schema("%s: payload.%s has unsupported protobuf type %s",
				source, f.Name(), f.Kind())
		}
		scalarFds = append(scalarFds, f)
		scalarCols = append(scalarCols, store.Column{Name: string(f.Name()), Type: ct})
	}
	return scalarFds, scalarCols, repeated, nil
}

func checkUnique(cols []store.Column, source string) error {
	seen := map[string]bool{}
	for _, c := range cols {
		if seen[c.Name] {
			return fwerr.Schema("%s: duplicate column '%s' "+
				"(a generic and a payload/sub field share a name)", source, c.Name)
		}
		seen[c.Name] = true
	}
	return nil
}
