# Goals and Requirements — the analysis system

**Status: draft for review.** This file lists *what* we want to build, in plain
language. It does not say *how*.

The work is split into phases:

- **Phase 1 (now)** — ask for a graph in plain language and get it in Grafana.
  Agreed on 2026-10-09.
- **Phase 2 and later** — automated analysis reports, starting with automated FIO
  validation. These are the goals written before the Phase 1 decision, kept
  unchanged [further down](#phase-2-and-later--automated-analysis-reports).

Phase 1 also builds a foundation that Phase 2 needs: the written meaning of every
table and column (Phase 2 Goals 1, 2 and 10) is created in Phase 1 and reused
later.

Scope: the analysis side only. This file does not cover changes to Profile FW,
the live streaming work in [roadmap.md](roadmap.md), or replacing Grafana.

---

# Phase 1 — Plain-language requests to Grafana graphs

## What Phase 1 delivers

An engineer describes what they want to see, and the system understands the
request, finds the data, writes the SQL, runs it, and creates the graph in
Grafana, together with a short explanation. No query or graph has to exist in
advance.

Example requests:

- "Show me all read-write I/O counts."
- "Show me all read-write split I/Os."
- "Show latency for filesystem and block layers, but not SSD."
- "Compare read and write IOPS between the block and NVMe layers."
- "Show the bandwidth trend for the last 30 minutes."

## Decisions already made

- **Meanings may be predefined; queries and graphs are not.** Written
  descriptions of tables, columns and relationships are allowed and expected.
- **Requests come from a file**, `request.yaml`, and the system runs from the
  command line. A UI page comes later.
- **The request names the run.** Choosing the run in a UI comes later.
- **One device per run** today, so a request never needs to name a device.
- **Unclear requests:** the system picks a sensible meaning, shows it, and asks
  yes or no on the command line. Yes runs it; no lets the user rephrase.
- **Each graph comes with a short explanation of what it plots**, not why the
  values look the way they do.
- **Confirmed request-and-query pairs may be kept as examples** to guide later
  requests.
- **The LLM is the company's deployed, OpenAI-compatible LLM API**, reachable
  from the office box, with roughly 128k tokens of context. Whether it supports
  tool (function) calling is not known yet, so **the design must not depend on
  it**. Its configuration is provided when implementation starts.
- **Grafana stays the drawing layer:** the latest Grafana container, with the
  ClickHouse plugin already installed, reached through its REST API or its MCP
  server. **Graphs go into one Grafana folder per run.** The trial may first try
  WrenAI's own chart generation (GenBI, local preview only) if the LLM API
  supports tool calling; Grafana remains the Phase 1 target. See
  [phase1-tool-research.md](phase1-tool-research.md), section 4.
- **Licences:** Apache-2.0 and MIT are acceptable. AGPL has not been approved.
- **Existing open-source or free tools are preferred** over building from
  scratch. See [phase1-tool-research.md](phase1-tool-research.md).
- **Not in Phase 1:** the UI page, automated FIO validation (moved to Phase 2),
  written analysis reports, and comparing two runs.

## Goal A — Turn a plain-language request into a graph

**A1** — A user must be able to describe what they want to see in plain language, without naming tables, columns or SQL.

**A2** — For now, the request is read from a `request.yaml` file, and the system is run from the command line.

**A3** — The request names the run to use; the run's single device is used. Times in a request, such as "the last 30 minutes", are taken within that run, not from the current clock — runs are loaded after they finish.

**A4** — Before running anything, the system must state in plain words what it will show: which measurement, which layers, which run and time range, and which kind of graph. It must then ask the user to confirm, yes or no.

**A5** — When a request can be read more than one way, the system must pick a sensible meaning and say which one it picked, as part of A4. It must never choose silently.

**A6** — If the user answers no, the user can rephrase the request and the system starts again.

**A7** — The system must find the data it needs by itself, write the SQL, run it, and create the graph in Grafana. A request must work even if no query or graph like it has ever been made.

**A8** — The system must choose the kind of graph from the request and from the shape of the result: time series, line, bar, comparison, table, histogram, or another suitable type. The user may ask for a particular kind.

**A9** — Grafana draws the graphs. The system supplies the query and the graph settings, creates the graph in a Grafana folder named after the run, and gives the user a link to it.

**A10** — Each graph must come with a short explanation of what it plots: the measurement, the layers, the run and time range, the units, and how the values were calculated. It does not explain why the values look the way they do; that belongs to Phase 2.

**A11** — If part of a request needs data that is not collected — for example the filesystem layer today — the system must say so clearly, and still answer the rest of the request.

## Goal B — Understand the data, not just the column names

Column names do not say how to use the data. `read_ios`, for example, is a
running total: adding up its samples gives a number hundreds of times too large.

**B1** — There must be a written description of every table, column and relationship: what it means, its unit, whether it is a running total or a reading at a moment, and how to calculate with it. Engineers write and correct these descriptions without changing code.

**B2** — The system must use these descriptions, not only the raw table and column names, to decide which data answers a request.

**B3** — Running totals must be turned correctly into amounts per interval or per run. Readings at a moment must never be subtracted. Units must be converted to what the user expects.

**B4** — The system must keep working when the database schema changes: new tables, new columns, changed columns. New data must become usable as soon as its description is written.

**B5** — Confirmed request-and-query pairs may be kept as examples to guide later requests. They are hints only: every request still gets its own new query.

**B6** — The descriptions and examples must be kept in our own files in this repository, in our own format, so they can move to another tool if the chosen tool changes or stops being maintained.

## Goal C — Graphs and explanations must be right

A wrong graph is easy to believe. These requirements are what stop one reaching
an engineer.

**C1** — Before a query runs, it must be checked against the rules of Goal B: no adding up of running totals, no subtracting of moment readings, units converted, limited to the requested run, read-only.

**C2** — After a query runs, the result must be checked for values that cannot be true, such as negative rates. An empty result must be explained, not shown as a blank graph.

**C3** — The explanation must only say what the graph's data supports. Every number in it must come from the query result.

**C4** — The SQL behind every graph must be visible, so an engineer can check it.

**C5** — Phase 1 is accepted when the system passes a test set of **30 requests**: the 17 kinds of existing Grafana graph, described in plain language (the current hand-built panels are the answer key); the five example requests above; and eight more, including requests that must be refused or only partly answered — for example latency for the filesystem layer. A request **passes** when the meaning shown at A4 matches what was asked, and the graph is correct: the same numbers as the answer key (within rounding), the right run, layers and units, a suitable kind of graph, and an explanation that says only what the graph shows.

**C6** — The pass mark (set on 2026-10-09; it can be raised later):

- at least **27 of the 30** requests pass, each asked once, without rephrasing;
- **all five** example requests pass;
- **no wrong graph is ever shown as if it were right.** Every failure must be visible: a warning, a clear "cannot answer", or a meaning the user can reject at the yes/no step. This rule has no exceptions;
- asking the same request twice gives the same numbers.

## Phase 1 open points

All earlier questions are answered. What remains are small actions:

1. **A Grafana service-account token** for the system, instead of the admin password. Created once, by an admin, in Grafana under *Administration → Users and access → Service accounts*, with the Editor role.
2. **Check whether the LLM API supports tool calling**, as the first step once its configuration is available. Nothing depends on the answer, but it decides how the requests to the LLM are written.
3. **Export the existing Grafana dashboards as JSON** (*Share → Export*). One file per dashboard gives both the answer key for C5 and the SQL behind every panel.
4. *Suggested:* **pin the Grafana version** in the container instead of `latest`, so the dashboard format does not change underneath the system after an update.

---

# Phase 2 and later — automated analysis reports

These goals were written before the Phase 1 decision and are kept unchanged.
Phase 2 starts with automated FIO validation (Goal 4). Their requirement numbers
are referred to by [analysis-architecture.md](analysis-architecture.md).

### How these goals fit together

- **Goals 1 and 2** are the foundation: the system must know where the numbers
  come from, and must know the storage and SSD domain. Nothing else works well
  without both.
- **Goals 3, 4 and 5** are the validation track: check the data, analyse an FIO
  run, and investigate when something looks wrong.
- **Goals 6, 7, 8 and 9** are the analysis track: understand a real workload, at
  several depths, deep enough to guide SSD and firmware work.
- **Goal 10** keeps all of it working as the database and the profiling grow.
- **Goals 11 and 12** are about being right: what a trustworthy report means,
  and the independent check that enforces it before anything is delivered.

---

### Goal 1 — Give the analysis system full knowledge of the profiling framework

The system will see numbers. It must also know what those numbers mean and where
they came from, or it cannot explain anything reliably.

**1.1** — There must be a written description of every storage layer that is profiled.

**1.2** — For each layer, the system must know where the data comes from: which kernel functions, tracepoints, or files are read.

**1.3** — For each collected field, the system must know what it means and its unit.

**1.4** — For each field, the system must know whether it is a running total since boot, a value at that moment, or a measurement over an interval.

**1.5** — The system must know the exact point inside each layer where a measurement is taken, so it can explain what that value includes and what it leaves out.

**1.6** — The system must know what each profiling point can and cannot observe, including sampling, filtering and known limitations.

**1.7** — The system must know which layers are not profiled yet, so it never treats missing data as "no activity".

**1.8** — This knowledge must live in the repository and be updated whenever profiling changes.

**1.9** — Adding a description for a new layer or field must not require changing analysis code.

---

### Goal 2 — Feed domain knowledge into the system

A language model may not know enough about storage, NVMe, SSD internals or our
own hardware. Some of what it does know may be out of date or wrong for our
devices. We must be able to teach it, and to correct it.

**2.1** — There must be a place in the repository to store domain knowledge, kept separately from the profiling descriptions in Goal 1.

**2.2** — It must cover how the Linux I/O stack behaves: caching, readahead, merging, splitting, queueing, scheduling, deferred writeback, and mmap or page-fault I/O that never appears as a read or write call.

**2.3** — It must record which I/O paths are invisible at which layer, so the analysis can reason correctly about a layer showing no activity while a lower layer is busy.

**2.4** — It must cover SSD internals that matter for analysis, such as the flash translation layer, garbage collection, wear levelling, write amplification, over-provisioning and cache behaviour.

**2.5** — It must cover the specific devices we test: capacity, interface, queue count, and published limits.

**2.6** — It must cover the host system where it affects I/O: CPU core count, NVMe queue count, and how the two are mapped to each other.

**2.7** — It must be able to hold firmware and SSD policy information where that is available to us.

**2.8** — It must hold expected or typical value ranges, so the analysis can tell normal behaviour from unusual behaviour.

**2.9** — It must hold our own team knowledge, including findings from earlier runs and things we have already learned the hard way.

**2.10** — Engineers must be able to add or correct domain knowledge without changing code, in whatever form is convenient: documents, written explanations, rules, or worked examples.

**2.11** — Where stored domain knowledge disagrees with what the model assumes, the stored knowledge must win.

**2.12** — Knowledge must only be applied where it actually fits the workload, measurement, device or firmware being examined.

**2.13** — Domain knowledge must be kept clearly separate from measured data, so a report never presents an assumption as a measurement.

**2.14** — It must be possible to see which piece of domain knowledge led to a conclusion, and where it came from.

**2.15** — Domain knowledge that turns out to be wrong must be correctable, and the correction must apply to later analyses.

**2.16** — Both the analysis and the independent checker in Goal 12 must be able to use this knowledge.

---

### Goal 3 — Automatically check that the collected data is internally consistent

Some checks do not need to know anything about the workload. If one of these
fails, something is definitely broken. These checks apply to every run, not only
FIO runs.

**3.1** — For each layer and direction, the aligned and unaligned I/O size buckets must add up to the total I/O count for that layer.

**3.2** — Bucket boundaries must be applied exactly as the profiler defines them. An unaligned range such as 4 KB to 8 KB means larger than 4 KB and smaller than 8 KB; exactly 4 KB and exactly 8 KB belong to their aligned buckets.

**3.3** — Buckets must be checked for gaps and overlaps, so no I/O size is counted twice or missed entirely.

**3.4** — Per-queue counts, per-CPU-core counts and per-LBA-range counts must each add up to the layer total.

**3.5** — Bandwidth must agree with IOPS multiplied by average I/O size.

**3.6** — Per-second values must add up to the totals reported for the whole run.

**3.7** — Counters that can only increase must never go backwards.

**3.8** — Timestamps must be continuous and cover the whole run, with no unexplained gaps.

**3.9** — Any comparison must use matching time ranges, the same device, and the same units before the numbers are compared.

**3.10** — Every numeric check must have a stated tolerance, and the tolerance used must be shown in the result. A check must never quietly pass or fail on a hidden margin.

**3.11** — Before a check reports a problem, it must confirm the field actually means what the check assumes, using Goal 1. A wrong assumption about a field must not be reported as a data fault.

**3.12** — These checks must not depend on knowing which workload was run.

**3.13** — Each check must give a clear pass or fail, together with the numbers that caused it.

**3.14** — It must be easy to add new checks as new fields and layers appear.

---

### Goal 4 — Automatically analyse and validate an FIO run

FIO is the known workload we use to check the whole pipeline before trusting it
on a workload we do not understand. Today this is done by looking at graphs by
hand.

**4.1** — The system must accept the FIO command or job file, the FIO JSON output, the profiled data, and the information derived from the graphs.

**4.2** — All inputs must come from the same run. Missing or mismatched inputs must be reported, not worked around.

**4.3** — Until Run and Deploy FW supplies these automatically, the system must accept file paths given by hand.

**4.4** — It must read the FIO configuration and describe in plain language what workload was asked for, including pattern, sizes, I/O method, queue depth, job count, target device, duration and pauses.

**4.5** — It must describe what the profiled data actually shows at each layer.

**4.6** — It must keep four things apart: what the job file asked for, what FIO reports it achieved, what each profiling point observed, and what the graphs display.

**4.7** — It must compare what was asked for with what was observed, and explain the differences it can explain.

**4.8** — It must treat differences between layers as something to explain, not automatically as errors. The kernel legitimately splits, merges, caches and pre-reads I/O.

**4.9** — It must not assume that FIO settings predict what the lower layers will do. What happens below the top of the stack is what we are measuring, not something we can assume in advance.

**4.10** — Timeline checks must allow for the sampling interval, and for activity that legitimately continues past a phase boundary, such as delayed writeback after a write phase ends.

**4.11** — Distribution across NVMe queues and CPU cores must only be expected to be even where the workload and system configuration actually justify it.

**4.12** — For SSD health values, it must separate what had already accumulated before the run from what changed during the run. Lifetime counters must not be read as run activity.

**4.13** — It must clearly separate three kinds of statement: facts taken from the data, explanations it is confident about, and things it cannot explain.

**4.14** — It must give an overall verdict on whether the data looks trustworthy enough to move on to a real workload, and show which checks passed, which failed, and which could not be completed.

**4.15** — Each problem reported must state the expected behaviour, the observed behaviour, the measurements behind it, and why it matters.

**4.16** — The result must be a text report.

**4.17** — It must work for any FIO job, with any parameters — not only the current sequential and random read/write pattern.

**4.18** — Each result must record the run, the workload, and the versions of the components involved where known, so a later run can be compared against it.

**4.19** — It must clearly reduce the time now spent checking graphs by hand.

---

### Goal 5 — Investigate problems when something looks wrong

Knowing that something is wrong is not enough. Someone still has to find out
where. This is a separate activity from normal analysis.

**5.1** — When a check fails, or the analysis reports something it cannot explain, the system must be able to investigate it as a separate step.

**5.2** — The investigation must consider every stage that could be at fault: the workload run itself, the profiling code, the data files, the loader, the database, the queries, and the graphs.

**5.3** — It must collect evidence for and against each possible cause.

**5.4** — It must name the most likely stage and explain its reasoning.

**5.5** — It must separate a confirmed problem from a possible cause.

**5.6** — It must say when the evidence is not enough to decide, and state what further check would settle it.

**5.7** — It must never guess a cause just to give an answer.

**5.8** — Investigation must stay separate from routine analysis, so normal runs stay fast.

---

### Goal 6 — Automatically analyse a real workload

This is the actual purpose: understand workloads we do not already understand,
starting with LLM model loading and LLM fine-tuning.

**6.1** — The system must produce a written analysis of a profiled run without a person reading the graphs first.

**6.2** — It must work when nothing is known in advance about how that workload behaves.

**6.3** — It must use whatever workload context is available — model, parameters, run settings, system details — and must say what missing context limits the analysis.

**6.4** — It must take account of the Goal 3 checks and any FIO validation for that run. If those failed, or are not available, the analysis must say so and qualify its conclusions accordingly.

**6.5** — It must find the phases of a run by itself: where behaviour changes, where activity stops, and where the read and write balance shifts. The phases are not given in advance.

**6.6** — It must characterise each phase, and name it where the evidence or the supplied context supports a name, such as model load or checkpointing.

**6.7** — It must report the main measurements per phase, not only as a total for the whole run. One average across phases that behave differently hides the finding.

**6.8** — It must compare the phases within a run and say how they differ.

**6.9** — It must describe I/O sizes, access patterns, where on the device the I/O lands, and timing behaviour.

**6.10** — Distributions must be reported as shares of the total, not only as raw counts, so the dominant sizes, queues and address ranges are obvious.

**6.11** — A conclusion drawn from the absence of activity must first establish that the measurement was being collected and was able to observe the thing that is missing. Absence of data is not evidence of absence of activity.

**6.12** — It must use the existing hand-written analyses as examples of what is useful, not as a fixed template.

**6.13** — It must be free to organise the report differently, and to report findings the hand-written analyses never covered.

**6.14** — It must handle a full-length run across all layers without losing important detail.

**6.15** — The output must be a text report a person can read and act on, and must stay as compact as a good hand-written analysis. Supporting evidence must be available on request rather than filling the report.

**6.16** — Findings must point to the run, measurements, time periods or graphs behind them, so an engineer can check them.

**6.17** — An engineer must be able to mark an interpretation as wrong and supply extra context for a revised analysis.

**6.18** — It must clearly state what it could not work out, and why.

---

### Goal 7 — Compare runs against each other

Understanding one run is useful. Comparing runs is how we learn what a change
actually did.

**7.1** — The system must compare two or more runs of the same workload.

**7.2** — It must compare runs of different workloads.

**7.3** — It must compare the same workload run with different settings, models or parameters.

**7.4** — It must say what changed, by how much, and which differences matter.

**7.5** — It must make clear which configuration differences could explain the change.

**7.6** — It must say when two runs are not fairly comparable, and why.

**7.7** — It must support comparing a chosen time window instead of the whole run.

---

### Goal 8 — Analyse at several depths

Engineers ask different questions at different levels. Each level must be
available on its own.

**8.1** — Analyse each profiled layer on its own.

**8.2** — Compare two or more layers together, to show how I/O changes as it moves down the stack.

**8.3** — Quantify that change: by what factor the I/O count grows or shrinks between two layers, and how the size distribution shifts. Where the data supports it, attribute the change to splitting, merging, caching or readahead.

**8.4** — Give an overall view using every layer that is available.

**8.5** — Give a view dedicated to the SSD: access patterns, read and write mix, request sizes, address ranges, queue activity, latency, bandwidth and health.

**8.6** — Give a higher-level view of overall SSD behaviour and the SSD policies involved, where the measurements and policy knowledge allow it.

**8.7** — A user must be able to ask for one level without getting all of them.

**8.8** — The levels must agree with each other. Two levels must not contradict.

---

### Goal 9 — Produce insight that is useful for SSD and firmware work

This is the long-term reason the framework exists.

**9.1** — The analysis must be written for SSD, firmware, hardware and software engineers.

**9.2** — It must explain how the workload affects the SSD, not only what the host did.

**9.3** — It must summarise the workload demands that matter to firmware: request sizes, read and write mix, sequential or random access, bursts, concurrency and latency needs.

**9.4** — It must point out behaviour that could benefit from investigation or improvement, such as rising latency, limited throughput, or uneven use of queues and cores.

**9.5** — It must weigh host-layer behaviour before concluding that a problem belongs to the SSD or its firmware.

**9.6** — It must support comparing AI workloads with non-AI workloads.

**9.7** — It must separate a confirmed finding from a hypothesis. Where a hypothesis needs proof, it must say what further measurement or controlled experiment would settle it.

**9.8** — It must give enough evidence to justify a firmware or design change — or say clearly that the current data is not enough to justify one.

**9.9** — Every conclusion must be tied back to specific measured data.

**9.10** — It must not recommend a change based on data that is missing or incomplete.

---

### Goal 10 — Adapt to new tables, new columns and changed schemas

The database schema is derived from the profiling protos and changes whenever
profiling changes. New tables and new columns appear regularly. The analysis must
absorb them, and must get better because of them.

**10.1** — A new table appearing in the database must not break the analysis.

**10.2** — A new column added to an existing table must not break the analysis.

**10.3** — Added, renamed, removed or retyped fields must be recognised and handled, without code changes where possible, and with a clear message where not.

**10.4** — New tables and new columns must actually be used by the analysis, not merely tolerated. New data must improve the result, not sit unused.

**10.5** — New data must be able to revise earlier conclusions, not only add to them. Where new evidence challenges a previous finding, that must be visible.

**10.6** — Before new data is used in a conclusion, the system must know what it means, from Goal 1 and Goal 2. Unexplained new data must be reported as needing a description, not guessed at.

**10.7** — The system must interpret each run using the schema and field meanings that applied when that run was collected, so older runs stay correctly readable as the schema moves on.

**10.8** — It must never silently reuse an out-of-date query, calculation or field meaning. Where a change cannot be interpreted reliably, the affected analysis must stop and say what is needed.

**10.9** — There must be a way to see which tables and columns the analysis actually used, and which it ignored, so silently unused data is visible.

**10.10** — Adding a whole new profiled layer must not require rewriting the analysis system.

**10.11** — The filesystem layer must be supported once profiling for it exists.

**10.12** — Additional SSD parameters must be supported once they are collected.

**10.13** — The system must work correctly when only some tables or layers are present, and every analysis must state which layers and measurements were available for that run.

**10.14** — It must identify the gaps that limited its conclusions, and what extra data or context would improve them.

**10.15** — A table or column that disappears or is renamed must be reported clearly, not ignored in silence.

**10.16** — The set of tables, graphs and checks must be treated as a growing list, never a fixed one.

---

### Goal 11 — Define what a trustworthy analysis means

An analysis that sounds confident but is wrong is worse than no analysis at all.
This goal states the rules. Goal 12 enforces them.

**11.1** — Every statement in a report must be traceable to specific data.

**11.2** — The system must not state anything the data does not support.

**11.3** — It must clearly separate measured facts, domain knowledge, and its own interpretation.

**11.4** — A possible cause must never be presented as an established fact merely because it sounds plausible.

**11.5** — An explanation drawn from domain knowledge is a legitimate finding when it is labelled as one. Much of what is useful to a firmware engineer is inference, not measurement. The requirement is honesty about the status of a statement, not avoidance of inference.

**11.6** — It must say what it does not know, and what data would be needed to know it.

**11.7** — It must not claim a complete cross-layer understanding while layers are still missing.

**11.8** — Running the analysis again on the same data must give consistent conclusions.

**11.9** — Numbers quoted in a report must match the stored data exactly.

**11.10** — A report must not contradict itself.

---

### Goal 12 — Check every analysis before it is delivered

We must never hand an engineer an incorrect analysis. A separate component must
review the report before the user ever sees it. This applies to FIO validation
reports, workload analyses, comparisons, and firmware recommendations alike.

**12.1** — A separate component must check every analysis report before it reaches the user.

**12.2** — The checker must be independent of whatever produced the report, so it does not repeat the same mistake.

**12.3** — The producing component saying its own answer is correct is not approval. Its confidence carries no weight in this decision.

**12.4** — The checker must verify reported values, calculations, units, time ranges and comparisons against the source measurements and the schema that applies to them.

**12.5** — It must confirm that the supporting data belongs to the correct run.

**12.6** — It must verify that every conclusion is actually supported by the data it refers to.

**12.7** — It must find statements that go further than the data can show.

**12.8** — The checker enforces correct labelling, not silence. An interpretation or hypothesis that is labelled as one, and that is consistent with the evidence and the domain knowledge, must be allowed through. Stripping well-founded engineering inference out of a report is a failure of the checker, not a success — the most valuable findings are often the ones no instrument measured directly.

**12.9** — It must find contradictions inside the report.

**12.10** — It must check claims against the domain knowledge in Goal 2, and flag where they disagree.

**12.11** — It must check that missing data has been acknowledged rather than assumed away.

**12.12** — It must be able to reject a report or send it back for correction, not only add comments.

**12.13** — A report that fails the check must never be delivered as though it had passed.

**12.14** — Where a reliable analysis is not possible, the user must get a clear statement of the limitation and what evidence is missing — not the unchecked conclusions.

**12.15** — A corrected or revised report must be checked again before release.

**12.16** — Approval applies only to the exact report and inputs that were checked. If the data, schema, queries or domain knowledge change, the affected findings must be reviewed again.

**12.17** — The user must be told that the report was checked, and what was flagged. An approved report must still carry its remaining limits and label its hypotheses as hypotheses.

**12.18** — The checker itself must be testable: it must be shown to catch reports that are deliberately wrong, and must be shown not to strip out correctly labelled inference.

---

### Open points for review

These affect the requirements, so they are worth settling before design starts.

1. Should the consistency checks (Goal 3) run automatically after every load, or only when asked?
2. Does the report need a machine-readable form as well as text, so results can be tracked over time?
3. Is Goal 7 (comparing runs) wanted now, or later?
4. Should Goal 5 (investigation) be built at the same time as Goal 4, or after it?
5. How much of Goal 1 already exists, and how much has to be written from scratch?
6. Who owns the domain knowledge in Goal 2 — is it written once, or does every engineer add to it?
7. If the checker in Goal 12 rejects a report, should the system retry on its own, or stop and tell the user?
8. If a run fails the Goal 3 checks, should a workload analysis still be produced with a warning (6.4), or refused?
9. Who sets the tolerances in 3.10, and are they fixed per check or per device?
10. Do we need runs with known problems to test Goal 3 and Goal 12 against? We have none today, and without them we cannot prove either one catches anything.
