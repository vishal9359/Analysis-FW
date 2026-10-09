# Superseded — see analysis-architecture.md

This proposal has been merged into [analysis-architecture.md](analysis-architecture.md), which is now the single
architecture document to review.

Taken from it: the **Workflow & Report Controller** as a named component, the
**five-stage checkpoints** (notably checking each calculation *before* it runs),
**render-time verification of the exact report**, the **one-Analyst** position,
measuring **false rejections** as well as missed errors, the InsightPilot and
Panda references, and three domain cautions it got right: size buckets do not
establish address alignment, a sampled gauge cannot establish an unsampled peak,
and phases repeat and overlap.

Not carried over: its coverage table, which maps to a superseded 5-goal / 50-
requirement numbering rather than the current 12 goals and 153 requirements in
[goals-and-requirements.md](goals-and-requirements.md).

The original text is kept at [_history/simple-analysis-architecture.md](_history/simple-analysis-architecture.md).
