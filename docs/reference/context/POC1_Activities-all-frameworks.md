# POC 1 — Activities by Framework (one engineer per framework)

**Duration:** ~4 weeks
**Hardware:** Weeks 1–2 any Linux box with local NVMe; Weeks 3–4 DGX Spark (Presto drive). The Use-case FW engineer may start on Spark earlier for AI use-case compatibility work.
**Principle:** Each engineer works independently with simple stand-ins, against agreed contracts (Section 4). Integration happens once, at the end (Section 5). No engineer's progress may block another's.

## POC 1 common goal

One config-driven run on DGX Spark where:
```
Use-case FW starts the run → Profile FW tools collect per-IO + system data
→ Analysis FW pipeline ships/ingests it → live + post-run views in Grafana
```
…and each framework exits POC 1 with measured facts (rates, volumes, overheads, tool gaps) driving its POC 2 design.

---

## 1. Profiling FW — Activities

**POC 1 mission:** answer *what can we collect, with which tools, at what cost* — per layer of the IO stack, on both a generic Linux box and DGX Spark.

| # | Activity | Deliverable |
|---|----------|-------------|
| P1 | eBPF tooling evaluation: bpftrace vs BCC vs libbpf/CO-RE vs reusing existing tools (biosnoop, biolatency). Criteria: overhead, field access, ARM/kernel portability, maintainability, output control | Tooling comparison note + recommendation for production collectors |
| P2 | Instrumentation point mapping: verify available tracepoints/kprobes per layer — syscalls (incl. io_uring), VFS, page cache/writeback, ext4/jbd2, block/blk-mq (`block_rq_insert/issue/complete`), NVMe driver (`nvme_setup_cmd/nvme_complete_rq`) — on the dev box kernel | Layer → hook → available fields matrix |
| P3 | Parameter definition: map each P1 "system behavior" parameter from Frameworks.txt (seq/random, QD, latency split, IO flags, cached vs direct, bursts, LBA hotness, per-process stats, NVMe queue stats) to the concrete fields/hooks from P2 that produce it | Parameter → data-source traceability table |
| P4 | Prototype captures: run collectors per layer against the shared fio matrix (U3); measure event rate vs IOPS, trace output bytes/sec, and tool overhead (fio throughput/latency with vs without tracing) | Per-layer data characterization report |
| P5 | Loss accounting: design ring-buffer sizing + drop counters so every collector reports events dropped; demonstrate detection under deliberate overload | Drop-counter spec + demo |
| P6 | SSD host-visible data: prototype periodic sampling of SMART, NVMe log pages, telemetry via nvme-cli/sysfs; define fields and sampling intervals | SSD-domain collection prototype + field list |
| P7 | DGX Spark compatibility: verify BTF/CO-RE support, tracepoint availability, BCC/bpftrace on ARM + NVIDIA kernel; hand eBPF kernel-config requirements (e.g., `CONFIG_DEBUG_INFO_BTF`) to the kernel expert; if eBPF doesn't fit, identify alternates | **Spark eBPF compatibility report** (go/no-go + gaps) |
| P8 | *(joint with Analysis FW)* Agree the collector output contract: event format, common envelope fields, clock source (single monotonic clock), file/stream conventions | Signed-off output contract v0 (see Section 4) |
| P9 | POC 1 findings: what to collect in POC 2, with which tooling, at what expected overhead | Findings report + POC 2 recommendations |

---

## 2. Analysis FW — Activities (incl. data collection off the SUT)

**POC 1 mission:** prove the thin end-to-end data pipeline — *SUT events → shipper → ingest → ClickHouse → queries → Grafana (live + post-run)* — and measure where it breaks.

**Non-blocking rule:** until Profile FW prototypes mature, self-generate input using existing tools (biosnoop, custom bpftrace) driven by the shared fio matrix — swap sources later behind the same contract.

| # | Activity | Deliverable |
|---|----------|-------------|
| AN1 | Canonical per-IO event schema v0: common envelope (run_id, sut_id, layer, timestamp_ns, pid/comm, cpu) + per-layer payload (LBA, size, rw, flags, qid/cid, latency components). *(joint with P8)* | Schema spec v0 |
| AN2 | Run/experiment metadata model: run_id, SUT descriptor, workload config hash, tool versions, start/end, notes — everything needed to compare runs. *(consumes run_id from Use-case FW, see U4)* | Metadata schema, auto-populated per run |
| AN3 | Stand up single-node ClickHouse; implement schemas; measure compression ratio + insert throughput on real captured data; test TTL tiering (short retention raw, long retention aggregates) | Running DB + measured numbers |
| AN4 | In-DB aggregates: per-second rollups (IOPS, MB/s, p50/p95/p99, QD) and latency histograms via materialized views from raw events | Aggregate tables |
| AN5 | On-SUT collector wrapper v0: one command to start/stop a named set of tools for a run_id, structured output (NDJSON/CSV, rotation). *This is the interface Use-case FW calls (see Section 4)* | `collector` v0 |
| AN6 | Shipper v0: batch/stream rotated output to central node over HTTP; survive network interruption (retain + retry) | `shipper` v0 |
| AN7 | Ingest service v0: parse, batch-insert to ClickHouse, tag with run_id/sut_id, dead-letter bad records | `ingestd` v0 |
| AN8 | Pipeline measurement: end-to-end lag (event on SUT → visible in Grafana), shipping overhead on workload, behavior when DB stops mid-run | Pipeline measurement notes |
| AN9 | Query catalog v0 (versioned SQL): latency percentiles per layer; host-vs-device latency split; IOPS/throughput timeline; IO size distribution; seq-vs-random; QD over time; per-process attribution; burst detection | 8–10 queries mapped to Frameworks.txt P1 parameters |
| AN10 | Grafana dashboards: (1) live run monitor, (2) post-run deep dive by run_id, (3) run comparison (two run_ids) | 3 dashboards |
| AN11 | Ad-hoc SQL path documented (clickhouse-client, Grafana Explore); feedback session with ≥1 SSD firmware engineer on queries/dashboards | How-to + user feedback notes |
| AN12 | Stress & limits: max-IOPS run to find first bottleneck (eBPF drops / shipper / DB); project storage for 1-week run × 12 SUTs; failure drills (network cut, DB restart, full buffer disk) | Saturation report + **data volume projection memo** |
| AN13 | POC 1 findings + POC 2 backlog: raw-forever vs aggregate-first verdict, ClickHouse confirmation, multi-node layout sketch, stream-vs-post-run default | Findings doc + backlog |

---

## 3. Use-case Run & Deploy FW — Activities

**POC 1 mission:** prove a config-driven run pipeline — *deploy → configure → trigger profiling → run workload → stop profiling → validate → summarize* — and de-risk AI use-cases on DGX Spark.

| # | Activity | Deliverable |
|---|----------|-------------|
| U1 | SUT deployment automation v0: script installing all dependencies (fio, BCC/bpftrace, nvme-cli, collector components) on a fresh Linux SUT; idempotent re-runs | `deploy` v0 + setup checklist |
| U2 | Test-suite config file v0 (YAML): SUT address, workload selection + params, profiling on/off + tool set, run repetitions, output locations; parser with parameter validation and clear errors | Config schema v0 + parser |
| U3 | *(joint with Analysis FW)* Shared fio workload matrix: seq/rand × read/write/mixed × QD (1/8/32) × block size (4K/128K/1M) × buffered vs O_DIRECT; versioned job files — the reference workloads all three engineers use | fio job file set |
| U4 | Run pipeline v0: generate run_id → apply SUT settings → call collector start (AN5 interface) → execute workload → collector stop → gather workload logs → emit run summary. Framework log with timestamps and pipeline stages per Frameworks.txt | `runner` v0 + framework log format |
| U5 | AI use-case compatibility on DGX Spark: manually run NGC model-load / fine-tune / inference workloads; document setup steps, container runtime findings (docker/enroot/GDS usage), what is automatable vs not | **Spark AI use-case compatibility report** (feeds July Q3 plan item) |
| U6 | Basic SUT/SSD configuration hooks: apply a small set of kernel/SSD parameters from config before a run (e.g., IO scheduler, CPU governor; nvme set-features) as proof of the mechanism | Config-apply module v0 |
| U7 | Post-test validation v0: workload exit status, dmesg/error scan, SSD health delta (SMART before/after); run summary with pass/fail + execution time | Validation + summary module v0 |
| U8 | POC 1 findings: pipeline design learnings, Spark/NGC automation gaps, config schema evolution needs | Findings report + POC 2 input |

---

## 4. Contracts between engineers (agree in Week 1, freeze for the POC)

| Contract | Between | Content |
|----------|---------|---------|
| **run_id + log hierarchy** | all three | run_id format, who generates it (Use-case FW), directory layout for run artifacts on SUT and central node |
| **Collector control interface** | Use-case FW ↔ Analysis FW | `collector start/stop --run-id … --tools …` CLI v0 (becomes REST in a later phase per Frameworks.txt) |
| **Collector output contract** | Profile FW ↔ Analysis FW | Event format, envelope fields, per-layer payloads, clock source, drop-counter reporting, file naming/rotation |
| **Workload matrix** | all three | The U3 fio job set — same workloads for tool characterization (P4), pipeline testing (AN8/AN12), and runner testing (U4) |
| **Run metadata handoff** | Use-case FW → Analysis FW | Runner emits a metadata record (config hash, SUT descriptor, timestamps, status) that ingest stores with the run |

Weekly 30-minute sync across the three engineers: contract changes only allowed here.

---

## 5. Week-by-week view

| Week | Profiling FW | Analysis FW | Use-case Run & Deploy FW |
|------|--------------|-------------|--------------------------|
| 1 | P1, P2 start; agree P8/contracts | AN1–AN2; ClickHouse up (AN3 start); agree contracts | U1, U2 start, U3 (shared matrix); agree contracts |
| 2 | P2–P4 on dev box | AN3–AN7 — **end-to-end slice working on dev box** | U4 runner v0 driving fio via collector interface |
| 3 | P5, P6; P7 on Spark | AN8, AN9–AN10; port pipeline to Spark | U5 on Spark; U6 |
| 4 | P7 wrap, P9 | AN11–AN13 | U7, U8 |
| End | **Integrated demo on Spark:** one YAML config → runner deploys/configures → collectors trace a fio run and one AI workload → data lands in ClickHouse → live + post-run Grafana dashboards → run summary with pass/fail | | |

**Milestones to protect:**
1. End of Week 1 — contracts in Section 4 agreed.
2. End of Week 2 — Analysis FW end-to-end slice works on the dev box (with self-generated data).
3. End of Week 4 — integrated demo on Spark.

---

## 6. Per-framework risks

| Area | Risk | Mitigation |
|------|------|------------|
| Profiling | Spark kernel lacks BTF/tracepoints | P7 is a report not a blocker; requirements to kernel expert early; fallback tool search is Profile FW's mandate |
| Profiling | Silent eBPF event loss corrupts all downstream analysis | P5 drop counters mandatory in every collector from day one |
| Analysis | Raw per-IO volume unsustainable (TB/experiment at 12 SUTs) | AN12 volume projection memo decides raw-forever vs aggregate-first before POC 2 |
| Analysis | Timestamp incoherence across layers/tools | Single monotonic clock source fixed in the P8 contract, Week 1 |
| Use-case | NGC workloads resist automation (containers, GDS, licenses) | U5 is deliberately manual-first: learn, document, automate in POC 2 |
| All | Cross-engineer blocking | Stand-ins per framework (existing tools for Analysis, fio for Use-case, dev box for Profiling); contracts frozen except at weekly sync |

---

## 7. Decisions POC 1 must answer (input to POC 2)

1. Raw-per-IO-forever vs aggregate-first with on-demand raw capture? *(AN12)*
2. ClickHouse confirmed? Multi-node layout for 6–12 SUTs? *(AN3, AN12)*
3. Production eBPF approach: bpftrace vs BCC vs libbpf/CO-RE? *(P1, P4, P7 — joint Profile/Analysis decision)*
4. Stream-during-run vs post-run shipping as default? *(AN8, AN12)*
5. Does eBPF fully work on Spark, or does Profile FW need alternate tooling? *(P7)*
6. What does AI-workload IO break in fio-shaped assumptions (mmap/page-fault IO, GDS)? *(U5 + AN pipeline observations)*
7. Config schema: what did v0 fail to express? *(U8)*
