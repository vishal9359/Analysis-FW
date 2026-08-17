package registry

import "google.golang.org/protobuf/proto"

// Thin wrappers so registry.go reads without importing proto directly for two
// calls; keeps the descriptor round-trip in one place.

func protoMarshal(m proto.Message) ([]byte, error) { return proto.Marshal(m) }

func protoUnmarshal(b []byte, m proto.Message) error { return proto.Unmarshal(b, m) }
