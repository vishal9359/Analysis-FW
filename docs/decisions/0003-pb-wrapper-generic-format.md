# 0003 — The `.pb` wrapper + `generic_format`/`payload` format contract

**Status:** Accepted. This is the seam between Profile FW and Analysis FW.

## Context

Analysis FW needs a stable, self-describing on-disk contract for the profiling data
that (a) doesn't require message-name coordination between producers and (b) handles
both flat metrics and repeated sub-structures (NVMe per-queue/per-core).

## Decision

- **Each `.pb` file is one wrapper message** (`StatLogs`) holding a
  `repeated <record>` field. Protobuf's repeated encoding delimits the records, so
  there is **no length-prefix framing**. A big run is split into several `.pb` files,
  each a complete wrapper.
- **A record** (`StatLog`) has a `generic_format` field (header:
  `GenericFormat` = timestamp, hostname, component, tag, log_level) and a `payload`
  field (layer data).
- The loader identifies record / header / payload / wrapper **by structure and the
  agreed field names** (`generic_format`, `payload`) — **not** by message name. Protos
  may reuse `StatLog`/`StatLogs`/`Payload` freely.
- **Payload scalars → the main table.** **`repeated <Message>` payload fields → child
  tables** (`<stem>_<field>`), one row per sub-element, additive (1 main + N + M, never
  N×M), linked to the main row by `record_id`.

## Consequences

- New producers interoperate without name coordination.
- Timestamps are **RFC 3339 UTC** (see [../timestamp-format.md](../timestamp-format.md));
  the loader tolerates several historical formats and clamps out-of-range values.
- **Planned change (no code change needed):** Profile FW intends to move `device` from
  `payload` into `GenericFormat` for all layers. Because the loader iterates header
  fields generically, `device` simply becomes a header column. Only caveat:
  pre-existing tables that never had `device` need a one-time `ALTER TABLE ADD COLUMN`.
- Reference: [../reference/requirements/profile-log-format-details.txt](../reference/requirements/profile-log-format-details.txt),
  and the design in [../design.md](../design.md).
