# Proposed longitudinal study

**Design only. Patient evaluation is deferred until repository cleanup is complete.**
The existing adapter runs the released SDBench baseline. It does not implement
custom longitudinal episodes, a recorded-only gatekeeper, one-shot comparisons
or evaluator ensembles. Original MAI-DxO is unavailable in the [inspected sources](SOURCES.md).

## Study question

Can the released sequential diagnostic workflow resolve a defined clinical
uncertainty from a longitudinal record, and how does its gatekeeper affect
accuracy, disclosure and resource use?

The paper compares MAI-DxO with an already sequential baseline. Studying the
released baseline alone cannot validate or refute MAI-DxO's reported advantage.

The first episode type is initial diagnosis before myeloma is established.
Later progression or complication episodes need their own clinical question;
repeating the already-known disease name would not answer it. Keep episodes from
one patient in the same data split and account for their correlation in analysis.
The current paired analyzer clusters by case, not by longitudinal patient.

## Episode contract to build

| Field | Meaning |
| --- | --- |
| Opening | Brief presentation and genuinely known history at entry |
| Permitted evidence | Source-linked findings within the chosen episode |
| Timing policy | When a requested result may be disclosed |
| Reference | Expert's documented conclusion and supporting evidence |
| Required specificity | Essential qualifiers, accepted uncertainty and conflicts |

Reuse the existing specialist records. Treatment decisions can help interpret a
record but do not automatically define the diagnostic answer. Freeze the reference
wording before scoring; resolve material contradictions rather than requiring
another blanket review of every record.

Current `CaseData` supplies the same `full_text` to gatekeeper and judge. Separate
gatekeeper evidence and privileged evaluator context require a new contract.
The requested variant returns recorded evidence or “not documented”; stock
SDBench can synthesize missing findings, so preserve that as a named separate
condition. The new recorded-only policy must exclude the reference answer and
answer-bearing assessment sections from the gatekeeper's input.

For the first retrospective mode, a later result can be returned when its test
is ordered, with its recorded relative timing. This does not claim it existed
at entry. True as-of-time replay needs availability timestamps and clock rules.
Requested confirmatory evidence is not automatically leakage; unsolicited
answers, out-of-episode findings and unsupported observations need source audit.
Missingness can itself reveal the clinicians' diagnostic path and must be reported.

## Separate comparisons

| Comparison | Interpretation |
| --- | --- |
| Sequential vs opening-only one-shot | Includes the value of acquired evidence; instructions necessarily differ |
| Sequential vs matched-evidence final answer | Same collected findings; conditional on the sequential agent's test choices |
| Agent vs expert's recorded workup | Descriptive historical comparison, not randomized human-versus-AI evaluation |
| Change only the gatekeeper model | Existing paired runner; diagnostic policy, judge, prompts and limits fixed |

An all-evidence one-shot is another information condition, not an equal-information
control. An all-myeloma initial-diagnosis cohort measures feasibility/recognition;
include a constant-answer control and avoid claims about specificity without
alternative diagnoses. Later episodes can have varied targets within the same
underlying disease. Keep disease-revealing cohort metadata out of agent context.

## Scoring and reporting

The [released judge](../sdbench/prompts/check_diagnosis/v3_reasoning/prompt.yaml)
compares clinical meaning, cause, specificity and incorrect additions. Scores
4–5 pass; 4 can tolerate omitted qualifiers, including stage. Retain that score
for compatibility and predeclare stricter episode-specific endpoints where needed.
The MIMIC judge also sees requested tests and gatekeeper-reported synthetic flags.
Those flags are not verified provenance.

Deterministic checks cover schema, calls and protocol limits; source/time checks
need the new evidence contract. Clinical equivalence needs an explicit rubric.
Different judges should score frozen outputs, with their disagreements reported;
an evaluator ensemble does not create ground truth. Re-judging must write separate
results after validating the original acquisition evidence, not alter its config.

Report scheduled/completed/failed encounters, qualifier errors, unavailable results,
disclosure audit, tests, rounds, latency and all-role API usage. Separate medical
cost estimates, token-rate estimates and invoiced charges. Freeze generation and
wall-clock limits before held-out evaluation.

## Implementation order after cleanup

1. Prepare one private episode and reference from existing expert records.
2. Implement source-linked evidence retrieval and test its access rules.
3. Inspect one complete encounter; treat it as integration evidence only.
4. Add frozen-output comparators and episode-specific evaluation.
5. Expand to a defined cohort with patient-level splits and analysis.

Use SDBench's `DiagnosticAgent` and `TestRunner` interfaces and Harbor's task and
verifier contracts. A new orchestration framework is not needed for this plan.
