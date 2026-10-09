# Analysis-FW — Simple High-Level Architecture

**Status:** Proposal for review; implementation and low-level design are pending.  
**Date:** 2026-09-20.  
**Basis:** All 50 [goals and requirements](goals-and-requirements.md).

**Start here for the architecture review.** This document consolidates the earlier
three-module, four-plane, low-level and multi-agent designs into one smaller
proposal. Those documents remain design history; they are not additional parts
that must be implemented alongside this proposal. The existing ingestion pipeline
is still documented in [architecture.md](architecture.md).

## 1. The proposed approach

Build **one analysis application with five logical components**: a small workflow
controller, shared context and knowledge, a calculation engine, one LLM Analyst,
and an independent Validator. These components can run in the same application.

The Analyst decides what to investigate and explains the results. SQL and calculation
code produce the numbers. The Validator checks the work throughout the process;
the controller enforces its decisions and releases the checked report.

This is an agentic workflow: the Analyst can request more evidence, investigate a
disagreement, and revise a finding. A single Analyst handles I/O, queues, CPUs and
SSD questions using the relevant tools and knowledge. Specialist agents can be
considered later if evaluation reveals a specific need.

### 1.1 Does this follow an agentic loop?

**Yes.** It follows the same broad tool-driven investigation pattern documented
by [DB-GPT's ReAct architecture](https://raw.githubusercontent.com/eosphoros-ai/DB-GPT/main/docs/docs/getting-started/concepts/architecture.md)
and [HolmesGPT's agentic loop](https://github.com/HolmesGPT/holmesgpt#how-it-works)
(references checked on 2026-09-20). This describes the approach; it does not select
either product as our implementation.

**Choose the next question → request a checked tool operation → inspect the result
→ revise the explanation or ask another question.**

The next investigation depends on what the tools return. The controller also
ensures that mandatory baseline calculations and data checks are completed; the
Analyst cannot skip them. Calculation requests are checked before execution, and
results are checked before becoming accepted evidence. Knowledge lookups retain
source, applicability and version information. Failed or incomplete operations
return their status and reason, so the Analyst can investigate without treating
their output as an established fact.

There is also a **review-and-correction loop**: the independent Validator can
request more evidence or reject an interpretation, sending it back for correction.
The Analyst proposing a final answer does not end validation. The controller
bounds tool calls, retries, time and cost; insufficient evidence or exhausted
limits cannot force approval. Release still requires the exact report to pass
all mandatory checks.

## 2. Architecture diagram

### 2.1 Components and responsibilities

![Five components in one application, with an investigation loop and independent validation](diagrams/simple-analysis-architecture.svg)

[Open the diagram at full size](diagrams/simple-analysis-architecture.svg).
Arrows show information and control flow. All calculation requests, results,
findings and report revisions pass the applicable validation checks.

### 2.2 Complete flow: FIO validation and LLM workload analysis

![FIO and real LLM workload inputs entering the shared checked agentic loop, independent review, and report release](diagrams/analysis-workflows.svg)

[Open the complete workflow diagram at full size](diagrams/analysis-workflows.svg).
These are two modes of the same application, using the same five components.
Here, **LLM workload** means the workload being profiled, such as model loading,
fine-tuning and checkpointing. The Analyst LLM assists both modes.

| Step | FIO-based analysis | Real LLM workload analysis |
|---|---|---|
| **Supply the run** | FIO job/command, achieved results, matching profiling data, system configuration and Grafana queries/panel settings. | Workload/model configuration, matching profiling data, events or phase timings, system/SSD context and applicable prior FIO findings or known limitations. |
| **Establish the questions** | Does observed behavior agree with what FIO requested and actually achieved? Do profiling and Grafana represent it correctly? | What happened in each phase and layer? How did the workload use the SSD, and what changed between comparable runs? |
| **Build checked evidence** | Mandatory data checks plus expected-versus-observed timeline, counts/bytes, buckets, performance and distribution comparisons, with reviewed tolerances. | Mandatory checks on this actual run plus complete run/phase metrics, cross-layer comparisons, queue/core/LBA distributions and available SSD telemetry. |
| **Investigate through the loop** | Follow discrepancies across workload execution, profiling, loading/storage, calculations and Grafana. For example, if source values agree but a panel differs, check its query, time window and unit conversions. | Follow significant patterns and test alternative explanations. For example, if checkpointing has larger downstream I/O counts, compare bytes, request sizes, split evidence and applicable limits before claiming splitting. |
| **Review and release** | Independently checked report of passed, failed and incomplete workload checks, with expected/observed values, diagnosis and limitations. | Independently checked phase/layer/SSD findings, comparisons, supported recommendations, hypotheses and evidence gaps. |

Both paths validate findings, render a candidate report, and check that exact
report before release. A correct report of a failed FIO check can be approved.
Unresolved mandatory **report-validation** checks withhold approval in either mode.
Corrections and changed inputs repeat the affected checks described in Section 4.

## 3. Each component's key jobs

| Component | Key jobs | Main output |
|---|---|---|
| **1. Workflow & Report Controller — ordinary code** | Fix the requested runs, devices, layers, time windows, phases and required topics/checks. Coordinate the other components, bound follow-up work, save evidence and decisions, and enforce validation. Render the report and release only its exact approved version. | Approved report, correction-required status, or insufficient-evidence status. |
| **2. Context & Knowledge — maintained information and retrieval** | Read the actual schema and supply applicable field meanings, units, collection behavior, relationships, workload/system context and phase information. Retrieve engineer-supplied profiling, Linux, SSD and firmware knowledge for both Analyst and Validator. Retain sources, applicability and versions. | A shared, traceable explanation of what the data means and what is known. |
| **3. Calculation & Evidence Engine — SQL and ordinary code** | Calculate counts, bytes, distributions, rates, latency, outstanding/busy activity and SSD health changes. Compare layers, phases, runs, FIO results and Grafana output. Check every run's data consistency and retain complete results with their scope, units and limitations. | Reproducible measurements, comparisons and data-check outcomes. |
| **4. Analyst — one LLM with tools** | Explain the workload and its SSD demands, request further calculations, investigate discrepancies and alternative explanations, and propose engineering recommendations. Draft findings with evidence and knowledge references; distinguish facts, interpretations and hypotheses. | Findings and a readable draft report for independent review. |
| **5. Independent Validator — code checks plus a separate LLM review context** | Independently examine inputs, calculation requests, results, findings and the rendered report. Check numbers and meaning, seek counterevidence, and reject unsupported conclusions. Record the check decisions that the controller must enforce. | Passed, failed or could-not-complete checks, with evidence and reasons. |

The existing **Profile FW → protobuf loader → ClickHouse** pipeline supplies
measurements. Other inputs include FIO jobs/results, workload events, system/device
configuration, Grafana queries and panel settings, and technical documents.
The Validator can retrieve full source evidence and knowledge independently of
the Analyst's chosen summaries.

Saved inputs, schema/knowledge versions, analysis settings and model/tool versions,
queries, full results, findings, failures, corrections and reports are shared
application records managed by the controller.
They survive a restart. Their storage design belongs in the later low-level design.

## 4. How analysis runs, and where correctness is checked

The controller applies the following sequence. The five checks are the earlier
**CP1–CP5**, expressed here in plain language; they reuse the same Validator.

| Stage | Work and required independent check |
|---|---|
| **1. Establish usable inputs** | Bind a stable data revision to the request, actual schema, definitions and applicable knowledge. Check run/device identity, collection coverage, phase timing and input compatibility. Record required topics, checks and tolerances before analysis. Unknown meanings block dependent work; usable parts can still be analyzed with explicit limits. |
| **2. Check each calculation before execution** | Confirm that the resolved query or function answers the intended question. Check filters, joins, units, denominator, counter handling and compatible observation points. Enforce read-only access and execution limits. |
| **3. Calculate and check results** | Calculate across the full selected period, including significant brief events. Save complete results, then check arithmetic, units, completeness and applicable consistency rules using independent reference methods. A preview or incomplete query cannot establish a whole-run result. |
| **4. Explain and check findings** | The Analyst links findings to checked evidence and applicable knowledge. The Validator checks support, scope, alternatives, contradictions and the strength of causal claims. More evidence or corrections return through the same calculation and review path. |
| **5. Render, check and release** | Check every factual statement and displayed value in the exact rendered report and any tables/charts. Verify required coverage and all dependent approvals. The controller releases that version only after every mandatory report-validation check passes. |

Numerical validation uses executable rules, independently reviewed reference
calculations and known-answer cases. Repeating the same wrong query is insufficient.
Separate LLM review helps check interpretation; model agreement cannot override a
failed check. The Analyst cannot change required checks or relax tolerances.

Every run is checked for missing/duplicate samples, timestamp problems, resets or
wraparound, bucket totals and agreement between related quantities. Checks respect
the collector's definitions and compatible populations. For example, cumulative
counters need appropriate interval treatment; sampled outstanding I/Os cannot
establish an unsampled peak. Failures and incomplete evidence retain their status;
missing measurements never mean zero activity.

Analysis also uses applicable prior FIO validation results and known profiling/query
limitations, stating when they are unavailable. Earlier validation never replaces
checks on the actual workload run.

Recorded workload events or engineer timings establish phases where available.
Inferred phases are labeled, with timing uncertainty and repeated or overlapping
occurrences preserved. Comparable-run analysis exposes configuration differences.
Equivalent evidence must produce consistent measurements and supported conclusions
across repeated analyses and layer/stack/SSD views.

The system may approve a report that correctly demonstrates a FIO or profiling
failure. It must withhold a report whose own mandatory validation remains unresolved.
A limited report can state supported findings and explicit gaps when its required
checks pass; dropping a claim must not silently remove a requested topic.

Changed evidence, definitions, knowledge or findings invalidate dependent approvals.
Corrections repeat affected checks and final report validation. Earlier versions and
failure reasons remain available for review. Engineer feedback follows this same path.

## 5. How the design grows with our data and knowledge

For a new table or schema change, discover what is actually stored, associate it
with the applicable field definitions, and determine which existing calculations
and checks can use it. Reuse compatible operations; add reviewed calculation code
and independent checks when a genuinely new measurement method requires them.
There is no fixed table list or new agent required per table.

New relevant evidence must improve, extend or challenge findings. Reports show
which available tables/fields were used, excluded or could not be interpreted,
and explain important changes from previous analysis. Old runs retain their own
schema and meaning. Unresolved renames, unit changes or changed semantics stop
affected calculations instead of silently reusing an outdated interpretation.

Engineers can update field descriptions and domain documents without changing
analysis code. Both analysis and validation use relevant, reviewed knowledge with
source and applicability information. Missing or conflicting knowledge leaves
dependent conclusions unresolved. Domain explanations remain distinct from measured
facts; examples guide report depth without dictating the answer.

## 6. Coverage of our requirements and sample report

| Requirement group | How this proposal covers it |
|---|---|
| **1.1–1.3: FIO inputs and timeline** | Compare requested workload, achieved FIO results, profiling observations and Grafana output from the same run; check activity, phases and pauses. Include dashboard SQL, variables, transformations and units. |
| **1.4–1.10: FIO measurement checks** | Calculation Engine and Validator cover cross-layer counts/bytes, exact size-bucket boundaries, rates/size/bandwidth/latency, outstanding/split/merge/busy activity, queues/cores/LBA, syscall types/flags and SSD health. |
| **1.11–1.13: Diagnosis and repeatability** | Analyst investigates workload execution, profiling, loading/storage, calculations and Grafana. Reports show expected/observed behavior, impact, tolerances and pass/fail/incomplete outcomes; retained versions allow repeat checks as coverage grows. |
| **2.1–2.14: Workload analysis** | Shared context, complete run/phase calculations, every-run quality checks and the Analyst support focused layers, stack/SSD explanations, comparisons, quantitative summaries, engineer feedback and consistent findings. |
| **3.1–3.11: Meaning, knowledge and flexibility** | Context & Knowledge and extensible calculations support new measurements, changed schemas, supplied expertise and visible coverage. New filesystem/deeper SSD collection under 3.3–3.4 also requires Profile FW work. |
| **4.1–4.5: SSD/firmware decisions** | Analyst connects measured workload demands to supported opportunities, applicable firmware knowledge, next experiments and fair before/after comparisons. |
| **5.1–5.7: Independent validation** | One Validator checks all five stages; controller enforces exact-version approval, revalidation, visible outcomes and evaluation against reviewed correct and incorrect examples. |

The design covers all 11 topics in the [manual fine-tuning report](finetune-report-alignment.md):
interfaces, read/write size distributions with aligned/unaligned size categories,
SSD-facing request sizes,
cross-layer multipliers, phase-specific sizes and volumes, device activity,
queue/core distribution, LBA concentration and temperature.

Size categories do not establish starting-address alignment. The strength of each
conclusion depends on the evidence. For example, mmap call
counts do not establish that all storage I/O used mmap; a count increase alone does
not prove splitting; more cores than queues does not prove lock contention; a
temperature rise during writes does not establish its cause. The system must retain
the supported observation and identify the evidence needed for a stronger conclusion.

## 7. What we borrow from existing solutions

| Reference | Applicable approach borrowed |
|---|---|
| [InsightPilot — EMNLP 2023](https://aclanthology.org/2023.emnlp-demo.31/) | Let an LLM choose analysis actions while an analysis engine computes structured results. Apply this to our measured I/O data and follow-up investigations. |
| [Panda — CIDR 2024](https://www.vldb.org/cidrdb/papers/2024/p6-singh.pdf) | Combine computed telemetry features with metric descriptions, relevant expert documents, source references and answer verification. Apply this to our profiling and SSD knowledge. |

These are reusable design patterns, not evidence that our reports will be correct.
Our requirements additionally demand independent numerical checks, validation at
each stage and enforced report release. This proposal borrows the approaches;
adoption of HolmesGPT, DB-GPT or another agent framework is not a prerequisite.
Model and library choices can be evaluated during detailed design.

## 8. What must be demonstrated before relying on reports

Architecture alone cannot guarantee correct analysis. Implementation acceptance
must demonstrate useful reports and detection of deliberately introduced errors:

- Reviewed FIO and real-workload runs, including the manual report's topics, with
  expected findings established independently and kept out of the Analyst's inputs.
- Wrong arithmetic, units, runs, phases, partial results, unsupported causes and
  contradictions caught at the appropriate stage; correct limited findings accepted.
- A new table, schema/meaning change, knowledge correction and changed run data
  producing the right revised findings and invalidating stale approvals.
- Repeatability, engineer corrections, and detection of changes to the rendered
  report after validation. Measure both missed errors and false rejections.

Only the loader exists today. Stable run registration, duplicate-load protection,
schema-change handling for analysis, calculation tools and validation still require
implementation; automatic migration of existing database tables is not implemented.
The next step is review of this high-level proposal. Low-level modules, interfaces,
storage layouts and implementation choices follow that review.
