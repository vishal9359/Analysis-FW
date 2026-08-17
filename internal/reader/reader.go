// Package reader parses a .pb file and iterates its records.
//
// A .pb file IS one wrapper message holding a `repeated <record>` list —
// protobuf's repeated encoding delimits the records, so there is no framing or
// length prefix. A big run is split into several .pb files, each a complete
// wrapper.
package reader

import (
	"fmt"
	"os"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/dynamicpb"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
	"github.com/vishal9359/Analysis-FW/internal/registry"
)

// Records parses one .pb file and returns its records in order.
//
// A parse failure means the .pb does not match its .proto (schema drift) or is
// truncated — reported as an input error with enough detail to diagnose it.
func Records(path string, schema *registry.TableSchema) (protoreflect.List, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fwerr.InputWrap(err, "cannot read %s", path)
	}

	if schema.Wrapper == nil {
		return nil, fwerr.Schema("%s: no wrapper message (a message with a repeated "+
			"record field) found in the .proto", path)
	}

	wrapper := dynamicpb.NewMessage(schema.Wrapper)
	if err := proto.Unmarshal(data, wrapper); err != nil {
		return nil, fwerr.InputWrap(err,
			"%s: could not parse as %s — the .pb does not match its .proto "+
				"(schema drift) or is truncated/corrupt. First bytes: %s",
			path, schema.Wrapper.Name(), firstBytes(data))
	}
	return wrapper.Get(schema.RepeatedField).List(), nil
}

func firstBytes(b []byte) string {
	n := 16
	if len(b) < n {
		n = len(b)
	}
	return fmt.Sprintf("% x", b[:n])
}
