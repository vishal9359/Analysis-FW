**Analysis FW — Goals and Requirements**

**Status:** Draft for review.  
**Date:** 2026-09-11.  
**Updated:** 2026-09-17.

This document describes what the framework should achieve in simple English.
Goals 1 and 2 are the immediate priorities. Goal 3 supports these goals through
flexible data handling and domain knowledge, with profiling coverage growing over
time. Goal 4 describes the broader engineering direction. Goal 5 requires separate
validation of every analysis report from the first release. The goals and
requirements will be reviewed and modified before implementation, design, or
architecture is decided.

The framework should help engineers understand workloads and use that understanding
to improve SSDs, with the main focus on SSD firmware. AI workloads are the current
priority, and support should grow to other workloads that generate I/O.

# Goal 1: Automate FIO workload validation

Reduce the manual effort needed to check whether profiling, stored data, analysis
queries, and Grafana graphs correctly represent a known FIO workload.

## Requirement 1.1: Understand the FIO workload

The system should understand the supplied FIO script or command, including the
read/write pattern, I/O sizes, I/O method, queue depth, number of jobs, target device,
duration, and pauses. It should support different parameter combinations. The
five-minute workload phases with one-minute pauses are one example, rather than
a required test pattern.

## Requirement 1.2: Use the results and data from the same run

The system should use the FIO script, FIO results, profiled data, and Grafana query
or graph results from the same run. It should distinguish what the script requested,
what FIO actually achieved, what each profiling point observed, and what the graphs
display. Missing or mismatched inputs should be reported.

## Requirement 1.3: Check the workload timeline

The system should check whether activity, read/write direction, phase changes, and
pauses appear at the expected times in the relevant graphs. It should account for
sampling intervals and explain delayed or continuing activity when the workload
or storage behavior supports that explanation.

## Requirement 1.4: Compare I/O counts and transferred data across layers

The system should compare read/write counts and transferred data at the syscall,
block, NVMe, and SSD layers wherever those measurements are available. Comparisons
should use matching time ranges, devices, and units. It should explain expected
differences caused by splitting, merging, buffering, or profiling locations, and
flag differences that remain unexplained.

## Requirement 1.5: Check I/O size buckets

For each applicable layer and read/write direction, the system should check whether
the aligned and unaligned size buckets together account for the corresponding
total I/O count. An unaligned range such as 4 KB to 8 KB excludes both endpoints;
exactly 4 KB and exactly 8 KB belong in their aligned buckets. Missing size coverage
or overlapping buckets should be identified.

## Requirement 1.6: Check that performance graphs agree

The system should check IOPS, average I/O size, bandwidth, and average latency
against the workload and the underlying measurements. For the same operations and
time interval, bandwidth should agree with IOPS multiplied by average I/O size,
and average size should agree with the transferred bytes and I/O count. Latency
should be interpreted according to what each layer actually measures.

## Requirement 1.7: Check outstanding, split, merged, and busy activity

The system should check whether outstanding I/Os, split/merged counts, and the
device-busy graph are consistent with the workload. These measurements should help
explain queue behavior and differences between layers. Any claimed inconsistency
should consider the actual meaning of the collected field.

## Requirement 1.8: Check where I/Os occur and how they are distributed

The system should assess read/write activity across LBA address ranges, NVMe queues,
and CPU cores. It should compare the observed distribution with the workload and
system configuration, and explain unusual concentration or missing activity where
the available information allows. An even distribution should only be expected
when the workload and configuration justify it.

## Requirement 1.9: Check syscall types and flags

The system should check whether the observed syscall I/O types and flags agree
with the FIO I/O method and options. It should consider which functions the profiler
actually observes when interpreting missing or additional operations.

## Requirement 1.10: Review SSD health and operating measurements

The system should review available SSD measurements such as temperature, spare
capacity, percentage used, power cycles, unsafe shutdowns, and media errors.
It should distinguish values accumulated before the run from changes during the
run, and highlight changes relevant to the workload or data validation.

## Requirement 1.11: Help locate validation problems

When results disagree, the system should identify whether the evidence points to
workload execution, profiling, data loading or storage, analysis calculations, or
Grafana queries and presentation. It should distinguish a confirmed problem from
a possible cause, consider evidence for and against suggested causes, and state
what further check would help resolve uncertainty.

## Requirement 1.12: Provide a clear validation result

The system should provide an overall result in a readable text report and show
which checks passed, which found a problem, and which could not be completed.
Each issue should state the expected behavior, observed behavior, supporting
measurements, and practical impact. Any acceptable difference or tolerance used in
a check should be explained.

## Requirement 1.13: Make validation repeatable as the framework changes

Engineers should be able to repeat the validation after changes to profiling code,
data loading, queries, or dashboards. Results should identify the workload, run,
and relevant versions when available, so findings can be reproduced and compared.
New measurements and graphs should be included as validation coverage grows beyond
the current 17 graph categories.

# Goal 2: Automate analysis of real workloads

Use an LLM to explain the behavior of actual workloads from the available profiling
data. Start with LLM model loading and fine-tuning, then expand to more workloads.

## Requirement 2.1: Understand the workload being analyzed

The system should use the available workload description, model details,
parameters, run settings, and system information to explain the purpose of the
run. It should identify missing context that affects the analysis.
Named phases such as model loading and checkpointing should use recorded workload
events or engineer-supplied timing where available. A phase inferred from the I/O
pattern must be identified as an inference. Repeated or overlapping phases should
remain distinguishable.

## Requirement 2.2: Consider the quality of the available data

The analysis should take account of relevant FIO validation results and known
profiling or query limitations, together with the consistency checks on the actual
run described in Requirement 2.10. A successful FIO validation does not establish
the quality of a later workload's data. The analysis should state which measurements
are usable and which findings are uncertain because of data-quality concerns.
If validation evidence is unavailable, that should be made clear.

## Requirement 2.3: Analyze each available layer

The system should explain the observed behavior within each profiled layer,
including I/O counts, sizes, rates, latency, and other measurements that are
available for that layer. It should identify meaningful changes over the run and
relate them to workload phases when those phases are known.
The analysis should cover the full selected period, including significant brief
events. Omitted data or summaries that limit the conclusions must be made clear.

## Requirement 2.4: Analyze multiple layers together

The system should compare selected layers and describe the overall behavior across
all available layers. It should explain supported relationships between changes
in I/O counts, sizes, timing, and latency. Missing layers or missing information
needed to connect observations should be stated clearly.

## Requirement 2.5: Provide an SSD-focused explanation

The system should explain how the workload uses the SSD, including access patterns,
read/write mix, request sizes, address ranges, queue activity, latency, bandwidth,
and health measurements where available. SSD policy behavior should be discussed
when the necessary measurements and policy context are available.

## Requirement 2.6: Produce a connected explanation of the workload

The analysis should connect related observations into an understandable account
of what happened during the run. It should highlight the findings most useful to
SSD engineers and distinguish measured facts, possible explanations, and questions
that need further investigation.

## Requirement 2.7: Learn from existing manual analyses

The system should use the team's manually written analyses as examples of useful
observations and expected depth. It should be able to use a different structure,
add supported insights, and improve the explanation beyond those examples.

## Requirement 2.8: Support different workloads and comparisons

The system should support different models, workload types, parameter combinations,
and run configurations. Engineers should be able to compare selected runs to
understand changes in I/O behavior, with relevant configuration differences made
clear. Comparisons should describe the size of the changes and state when different
conditions, coverage, or measurement meanings prevent a fair comparison. The scope
should be able to expand from AI workloads to other I/O workloads.

## Requirement 2.9: Make findings easy to review

Important findings should refer to the supporting run, measurements, time periods,
or graphs so an engineer can check them. Engineers should be able to identify
incorrect interpretations and provide additional context for a revised analysis.
The output should be a readable text report that uses clear language and states
the limits of each conclusion.

## Requirement 2.10: Check data consistency for every workload

Every run used in analysis must undergo the applicable data checks, even when its
expected workload behavior is unknown. These should cover bucket totals, agreement
between related metrics, and whether counts grouped by queue, core, or address
range account for the corresponding layer total where those groups cover each
operation once. Counts over intervals should agree with totals over the same period
when the measurements support that check.
Checks must first confirm that the compared measurements cover the same operations
and use compatible definitions, with any tolerance stated in the result.

The checks must also identify unexplained counter decreases, missing or duplicate
samples, and timestamp problems. They must account for documented resets, counter
wraparound, sampling or event timing, and collection start/stop times. Each check
must state whether it passed, failed, or could not be completed, and show the
supporting values.
Findings affected by unresolved issues remain subject to Goal 5's release rules.

## Requirement 2.11: Allow focused analysis and comparison

Engineers should be able to request analysis of one layer, selected layers, the
available stack, or the SSD perspective. They should also be able to select a time
window or workload phase for analysis and comparison. The selected scope and its
limits must be clear, and conclusions at different levels must remain consistent
when they describe the same evidence.

## Requirement 2.12: Produce consistent results from the same evidence

Repeating an analysis with the same data, scope, schema, domain knowledge, and
analysis settings should produce consistent measurements and supported conclusions.
The wording may vary. A material change in a finding should identify the changed
evidence, interpretation, or analysis version that explains it; unexplained
contradictions must be resolved before the report is approved.

## Requirement 2.13: Quantify workload and phase characteristics

The report should describe the observed I/O interfaces and quantify the main I/O
sizes, read/write mix, transferred data, outstanding I/Os, and activity across
queues, cores, and LBA ranges for the selected run or phase. Percentages must
identify what is counted, their denominator, layer, direction, time range, and
treatment of other or missing categories. Counts and
byte shares must remain distinguishable, with units and rounding made clear.

The report must distinguish a common request size from an average request size,
an observed sampled maximum from a proven bound, and I/O-count share from time
utilization. The same approach should support new measurements and workload phases
as the framework grows, without fixing the output to one example report.

## Requirement 2.14: Explain changes in I/Os between layers

The report should quantify changes in I/O counts, sizes, and transferred bytes
between compatible observation points, separately for reads and writes. It should
calculate count multipliers and explain when splitting, merging, or another
mechanism is supported by the evidence. Matching aggregate totals alone must not
be presented as proof of how each individual I/O moved through the stack.

# Goal 3: Expand profiling coverage, data flexibility, and domain knowledge

Improve what the profiling framework collects and what the analysis system knows
about those measurements. Analysis-FW must adapt as tables, schemas, and domain
knowledge change. Profiling coverage requires work across both frameworks; flexible
analysis and use of supplied knowledge are required for the current capabilities.

## Requirement 3.1: Explain the meaning of collected fields

For each measurement used in analysis, the system should have access to its meaning,
unit, source layer, and collection method. The context should explain whether a
value is a running total, a current value, or a measurement over an interval, and
how it should be interpreted.
Definitions must distinguish size-bucket categories from address alignment, NVMe
host-interface measurements from SSD internal measurements, and logical LBA ranges
from physical flash locations. Unit conversions must preserve the meaning of the
original byte counts and address ranges.

## Requirement 3.2: Provide profiling implementation context

The analysis system should have access to the profiling framework's relevant
architecture and behavior, including traced functions, tracepoints, collection
locations, sampling, filtering, timestamps, and known limitations. This context
should explain what each profiling point can and cannot observe.

## Requirement 3.3: Add filesystem-layer coverage

The profiling framework should add the filesystem measurements needed to understand
its role in workload I/O. The analysis should use those measurements to explain
filesystem activity and its relationship with neighboring layers. The required
measurements and their meanings should be reviewed as this coverage is defined.

## Requirement 3.4: Add deeper SSD measurements and context

The profiling framework should expand SSD parameter collection to support deeper
analysis of device behavior. Relevant firmware and SSD policy context should be
provided where available. Missing measurements that prevent an important engineering
question from being answered should be identified.

## Requirement 3.5: Extend analysis when new data becomes available

New layers, tables, fields, and graphs must become usable in workload analysis and
applicable FIO validation checks once their meaning is understood. The system must
use relevant new information to improve, extend, or revise its findings, including
earlier conclusions that the new evidence challenges. Engineers should be able to
see how the additional information affects the analysis.

## Requirement 3.6: State the coverage and remaining gaps

Each analysis should state which layers and SSD measurements were available for
the run. It should identify gaps that limit cross-layer explanations or SSD
conclusions and explain what additional data or context would improve the analysis.
The system should support useful analysis from the available subset of tables,
within those limits. Missing or uncollected data must not be treated as zero activity.

## Requirement 3.7: Accept new tables without a fixed table list

Analysis-FW must be flexible enough to use new profiling tables as they are added.
Once the table's purpose, fields, and relationships are supplied, the system must
include its relevant measurements in analysis. Adding a table should not require
redesigning the analysis workflow or restricting analysis to the original tables.

## Requirement 3.8: Handle changes to existing table schemas

Analysis-FW must recognize and adapt to added, renamed, or removed fields and
changes in field types, units, structure, or meaning. It must use the schema and
field definitions that apply to the data being analyzed, including older runs.
When a change cannot be interpreted reliably, the affected analysis must stop
and explain what information is needed. It must not silently reuse an outdated
query, calculation, or field interpretation.

## Requirement 3.9: Allow engineers to supply domain knowledge

Engineers must be able to provide and update domain knowledge used by Analysis-FW
without changing analysis code.
This may include technical documents, expert explanations, measurement rules,
profiling details, workload behavior, SSD policies, firmware information, and
validated examples. Relevant knowledge should include device specifications,
expected value ranges with their applicable conditions, Linux I/O behavior, and
SSD mechanisms such as garbage collection, caching, and write amplification.
Both analysis generation and the separate validation component
must be able to use this knowledge, so their reasoning can extend beyond the LLM's
built-in knowledge.

## Requirement 3.10: Use relevant and traceable domain knowledge

The system must identify the source of supplied knowledge and use it only where
it applies to the workload, measurements, hardware, or firmware being examined.
Engineers must be able to correct or replace outdated knowledge. Missing or
conflicting knowledge must be made clear, and findings that depend on it must
remain unresolved until there is enough support for a conclusion. Important
findings should identify the domain knowledge used to explain them. Reviewed,
applicable knowledge supplied by engineers should take precedence over unsupported
model assumptions. Conflicts with measured evidence must be investigated, and
domain knowledge must remain clearly distinguished from measurements.

## Requirement 3.11: Show which available data was used

Engineers must be able to see which available tables and fields contributed to an
analysis, and which were excluded or could not be interpreted. The system should
explain exclusions that affect the requested analysis, including irrelevant data,
missing field descriptions, and incompatible schemas. This should make newly added
data that is not yet being used visible.

# Goal 4: Help engineers identify SSD and firmware improvements

Turn reliable workload analysis into evidence that supports SSD engineering
decisions, with firmware behavior and performance as the main focus.

## Requirement 4.1: Identify important workload demands on the SSD

The system should summarize the workload patterns that matter for SSD behavior,
such as request sizes, read/write mix, sequential or random access, bursts,
concurrency, and latency needs. It should explain which observations are most
relevant to firmware engineers and why.

## Requirement 4.2: Identify supported improvement opportunities

The system should highlight behavior that may benefit from investigation or
improvement, such as latency increases, limited throughput, or uneven resource use.
It should consider host-layer behavior when assessing whether the evidence points
to an SSD or firmware issue.

## Requirement 4.3: Provide evidence for engineering recommendations

Recommendations should explain the observed issue, supporting evidence, possible
cause, and expected benefit of investigating it. They should distinguish a
confirmed finding from a hypothesis that requires more measurements or a controlled
experiment. Firmware-specific recommendations should use the available firmware
and policy context.

## Requirement 4.4: Help evaluate proposed changes

Engineers should be able to compare suitable runs before and after a firmware,
SSD configuration, or other relevant change. The analysis should describe observed
improvements, regressions, and trade-offs, and identify workload or system
differences that affect the comparison.

## Requirement 4.5: Support broader SSD development decisions

As profiling coverage and evidence grow, the system should help engineers assess
the need for firmware optimization, new firmware capabilities, and SSD software
or hardware improvements. Its output should give engineers a clear basis for
deciding what to investigate and validate next.

# Goal 5: Independently validate every analysis report before release

Prevent incorrect or unsupported analysis from reaching engineers as an approved
report. This goal applies to FIO validation reports, workload analyses, comparisons,
and SSD or firmware recommendations.

## Requirement 5.1: Use a separate validation component

Every generated analysis report must be checked by a component separate from the
component that produced it. The validator must assess the report independently
using the relevant source data and domain knowledge. The generating component's
own confidence or statement that its answer is correct is not sufficient approval.

Validation must also happen during analysis: check inputs and their meaning,
proposed calculations, returned results, and individual findings before accepting
them as support for later conclusions. Follow-up investigations and corrections
must receive the same checks. The analysis component must not be able to skip or
weaken required checks to obtain approval.

## Requirement 5.2: Check evidence and calculations

The validator must check reported values, calculations, units, time ranges,
comparisons, and references against the source measurements and applicable schema.
It must check that supporting data belongs to the correct run and that related
findings agree. Calculated values must be checked using their stated calculations;
unit conversions and displayed rounding must be accounted for. Its review must
examine the underlying evidence, including FIO results where relevant.

A query completing successfully is not enough: it must answer the intended
question, use the correct measurement scope, and return sufficiently complete
evidence. Failed or incomplete results must retain their status through later
analysis rather than being treated as checked facts.

## Requirement 5.3: Check interpretations and conclusions

The validator must check whether explanations and recommendations follow from the
evidence and relevant domain knowledge. It must identify contradictions, unsupported
claims, missing context, and conclusions that go beyond the profiling coverage.
Possible causes and hypotheses must be clearly identified; they must not be
presented as established facts merely because they sound plausible.
Claims such as "all I/Os use this interface," "the device is underutilized," or
"queue sharing caused lock contention" must have evidence for their scope and
meaning. A resource-count mismatch or two measurements changing together is not
sufficient by itself to establish a cause. If that evidence is missing, the report
must qualify the claim or state what further measurement is needed.

## Requirement 5.4: Block reports that have not passed validation

An analysis report must only be released as approved after all required validation
checks pass. A report with incorrect findings, unsupported claims, or unresolved
required checks must be withheld until those issues are corrected or the affected
claims are removed. If reliable analysis is not possible, the system must provide
a clear status explaining the limitation and needed evidence, without presenting
the unverified conclusions as a completed analysis.

## Requirement 5.5: Validate again after changes

Corrected or revised reports must pass validation again before release. Changes
to relevant data, schemas, queries, or domain knowledge must trigger a fresh review
of affected findings before they are used in a new or updated report. Validation
approval must apply to the specific report and supporting inputs that were checked.

When supporting evidence changes, the system must identify dependent results and
conclusions, repeat their affected checks, and validate the complete revised report
before release. It must preserve earlier failures and corrections for review.

## Requirement 5.6: Make the validation outcome visible

Engineers must be able to see which checks were performed, the evidence used,
issues found, and the reason a report was approved or withheld. Approved reports
must still state remaining analysis limits and clearly label supported hypotheses.
Passing validation must not be presented as proof of facts that the available
measurements cannot establish.

## Requirement 5.7: Demonstrate that validation detects incorrect analysis

The validation component must be evaluated using reviewed examples with known
correct findings and deliberately incorrect reports. The examples should cover
wrong calculations, mixed runs, incorrect units, unsupported explanations,
contradictory conclusions, and missing evidence. The evaluation must show which
errors were caught or missed and whether valid findings were incorrectly rejected.
These checks should be repeated when validation behavior changes.

Evaluation must include errors introduced at intermediate phases, and verify that
they cannot become accepted evidence or reappear unchecked in a later finding,
chart, or revised report. It must also include accurate reports of failed FIO or
profiling checks: reporting a problem correctly is a valid analysis outcome.

The [manual fine-tuning report alignment review](finetune-report-alignment.md)
provides an evolving example of the required report depth and validation cases.
Its reported values and explanations are not fixed expected answers for future runs.
