# Profile FW - how it works

Profiles the Linux IO stack on the SUT and writes protobuf `.pb` traces.
It is the **producer**; Analysis FW ingests these `.pb` files into ClickHouse.

## The flow (kernel → .pb file)

```
                         ┌───────────────────────────────────────────────┐
IO events ──────►        │ KERNEL SPACE                                  │
(bio, nvme,             │ eBPF program (kernel/ebpf_tracer.o)           │
 syscalls)               │ tracepoints / fentry / kprobes attach to     │
                         │ block_bio_queue, nvme_setup_cmd,              │
                         │ sys_enter/exit_read…, blkdev_read_iter…       │
                         │              │                                │
                         │              │ increments counters in         │
                         │              ▼                                │
                         │ BPF MAPS  (PERCPU_ARRAY / HASH)               │
                         │ cumulative running totals, keyed by           │
                         │ io_type / cpu / queue / lba-range / bucket    │
                         │ filtered to one device via config_map         │
                         └───────────────────┬───────────────────────────┘
                                             │
                                      map.Lookup() every -i sec
                                             │
                                             ▼
                         ┌───────────────────────────────────────────────┐
                         │ USER SPACE  (Go collector, cilium/ebpf)       │
                         │ ticker(-i) fires until -d elapses             │
                         │ each tick: read map, sum PERCPU copies,       │
                         │ snapshot → one StatLog{GenericFormat,         │
                         │                    Payload}                    │
                         │ (no delta done here — raw cumulative)         │
                         └───────────────────┬───────────────────────────┘
                                             │
                                         proto.Marshal
                                             │
                                             ▼
output/ProfileData-<tag>-<ts>/Linux/<subdir>/<name>.pb  (+ copy of .proto)
```

The 2 non-eBPF collectors read the same way but from a different source:
- `blk_dev_stats` → parses `/sys/block/<dev>/stat`
- `ssd_smartctl_stats` → runs `smartctl -a /dev/<dev>` and parses the text

## The launcher (`profile_fw`)

`sudo ./bin/profile_fw -tag base -i 1 -d 60 -dev nvme3 -all`

1. Validates `-dev` (bare controller name, e.g. `nvme3`).
2. Creates `output/ProfileData-<tag>-<timestamp>/Linux/{Block,NVMe,Syscall,SSD}/`.
3. Fan-launches the selected child binaries in parallel goroutines (`exec`).
4. Waits for all, then writes `protoc.txt` (decode commands) + copies each
`.proto`.

## Key points

- **One shared eBPF object** backs 5 collectors; each is a separate Go binary
  (build tags avoid `main()` collisions).
- **Device filtering** happens in the kernel: the collector pushes a 32-byte
  device name into `config_map`; eBPF handlers compare it against
  `gendisk::disk_name`. (sysfs/smartctl collectors just open that one device.)
- **Sampling = snapshot, not reset.** Maps keep accumulating; every tick copies
  the current running total. **Delta / per-second rates are the consumer's job.**
- **PERCPU maps** are summed across CPUs at read time (per-core/per-queue kept
  split where the proto has `per_core` / `per_queue` sub-messages).
- **Timestamp format**: `2006-01-02T15:04:05.000` + `Z` + local offset
  (e.g. `2026-07-31T10:24:29.853305:30`).

## The 7 outputs

| Collector | Source | `.pb` file | Root message | Component |
|---|---|---|---|---|
| blk_dev_stats | `/sys/block/*/stat` | `linux_block_1_stats.pb` | `blk_device_stats.StatLogs` | 101 |
| blk_bio_queue | eBPF (bio queue/split) | `linux_block_2_misc.pb` | `blk_bio_queue_stats.StatLogs` | 101 |
| nvme_stats | eBPF (nvme setup/complete) | `linux_nvme_1_stats.pb` | `nvme_stats.StatLogs` | 102 |
| nvme_seqrand | eBPF (nvme setup) | `linux_nvme_2_lbarandomness.pb` | `nvme_seqrand.StatLogs` | 104 |
| ssd_smartctl_stats | `smartctl -a` | `linux_ssd_1_stats.pb` | `ssd_smart_stats.StatLogs` | 103 |
| syscalls_stats | eBPF (syscalls/aio/uring) | `linux_syscall_1_stats.pb` | `iopattern.StatLogs` | 1 |
| syscall_converse | eBPF (blkdev_*_iter) | `linux_syscall_2_ioflags.pb` | `iopattern.StatLogs` | 2 |

## Envelope (every `.pb` is the same shape)

```
StatLogs
└── repeated StatLog              ← one per sample tick
    ├── GenericFormat             { timestamp, hostname, component, tag, log_level }
    └── Payload                   scalars + optional sub-messages:
                                  • scalar           → column
                                  • singular message→ flattened prefixed columns
                                  • repeated message→ child table
                                    (per_queue, per_core, io_patterns, lba_ranges)
```