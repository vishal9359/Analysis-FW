# Profile FW - tracepoints & kernel functions by layer

Every attach point below lives in `kernel/ebpf_tracer.c` (`SEC(...)` markers).
"Type" = the eBPF attach mechanism.

## Block layer
| Collector | Attach type | Hook | Kernel event | What it captures |
|---|---|---|---|---|
| blk_bio_queue | `tp_btf` | `block_bio_queue` | bio submitted to queue | read/write bios, size buckets (aligned and unaligned), REQ_* flags (once per bio, at submission) |
| blk_bio_queue | `tp_btf` | `block_split` | bio split into smaller bios | read/write splits |
| blk_bio_queue | *not checked* | `block_bio_backmerge`, `block_bio_frontmerge` | bio merged onto the back / front of a queued request | read/write back and front merges |
| blk_bio_queue | *not documented yet* | - | - | time in the driver layer only, time in the device only; driver and device latency buckets |
| blk_dev_stats | *(no eBPF)* | reads `/sys/block/<dev>/stat` | - | ios, sectors, times, in-flight, busy |

> The flags, merge, time and latency entries for `blk_bio_queue` come from the team's
> answers (2026-10-15), not from reading `ebpf_tracer.c`. Details:
> [knowledge/catalog/linux_block_2_misc.yaml](../knowledge/catalog/linux_block_2_misc.yaml), Q8 and Q9.

## NVMe layer
| Collector | Attach type | Hook | Kernel event | What it captures |
|---|---|---|---|---|
| nvme_stats | `tp_btf` | `nvme_setup_cmd` | NVMe cmd submitted | read/write ios, bytes, buckets, per-queue, per-core, LBA ranges |
| nvme_stats | `tp_btf` | `nvme_complete_rq` | NVMe cmd completed | device_time + driver_time latency |
| nvme_seqrand | `tp_btf` | `nvme_setup_cmd` | NVMe cmd submitted | per-core sequential/random run detection |

## Syscall layer (`syscalls_stats`)
| Attach type | Hook | Kernel event | What it captures |
|---|---|---|---|
| `tracepoint/syscalls` | `sys_enter_*` / `sys_exit_*` for `read`, `readv`, `pread64`, `preadv`, `write`, `writev`, `pwrite64`, `pwritev` | sync read/write syscalls | per-io_type count, size, latency (enter records start, exit computes) |
| `tracepoint/syscalls` | `sys_enter_io_submit` | AIO submission | AIO read/write io_type + size |
| `kprobe` | `aio_complete_rw` | AIO completion | AIO latency |
| `tracepoint/io_uring` | `io_uring_submit_req` | io_uring SQE submitted | io_uring read/write io_type + size |
| `tracepoint/io_uring` | `io_uring_complete` | io_uring CQE completed | io_uring latency |
| `tracepoint/syscalls` | `sys_enter_mmap` | mmap() called | mmap_count |
| `tracepoint/syscalls` | `sys_enter_mremap` | mremap() called | mremap_count |

## VFS block-device boundary (`syscall_converse`)
| Attach type | Hook | Kernel event | What it captures |
|---|---|---|---|
| `kprobe` | `blkdev_read_iter` | read to a block device | buffered vs direct counts/sizes, IOCB flags |
| `kprobe` | `blkdev_write_iter` | write to a block device | buffered vs direct counts/sizes, IOCB flags |

> Note: `blkdev_*_iter` use `kprobe` (not `fentry`) because these symbols are
> not BTF-exported on kernel 6.8; a comment in the source notes `fentry` would
> be lower-overhead where available.

## SSD SMART (`ssd_smartctl_stats`)
| Attach type | Source | What it captures |
|---|---|---|
| *(no eBPF)* | `smartctl -a /dev/<dev>` subprocess | temperature, spare, %used, data units, host commands, power cycles, errors |

## Attach-type quick reference
- **`tp_btf`** — BTF-typed raw tracepoint; lowest-overhead, typed args
  (block/nvme).
- **`tracepoint/...`** — classic tracepoint (syscalls, io_uring).
- **`kprobe`** — dynamic probe on a kernel function (AIO completion, blkdev_iter).
- **no eBPF** — userspace reads sysfs / runs a tool (blk_dev_stats, ssd).