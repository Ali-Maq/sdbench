# Proposed longitudinal study

**Study design; no patient evaluation has been run.** The adapter now supports
one frozen recorded-only episode through the released SDBench baseline. The
[implemented contract](RECORDED_EPISODES.md) separates permitted evidence from the
expert reference. One-shot comparisons and evaluator ensembles remain planned.
Original MAI-DxO is unavailable in the [inspected sources](SOURCES.md).

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

## Episode contract

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

Stock `CaseData` supplies the same `full_text` to gatekeeper and judge. The new
`recorded_episode` extension projects a different file for the gatekeeper: only
the opening and permitted evidence. Its selector returns IDs; application code
returns the stored excerpts or “not documented.” Stock generated observations
remain a separate condition. The preparer must exclude answer-bearing assessment
sections from the evidence catalog; deleting a reference field cannot sanitize
the clinical text itself.

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

Deterministic checks cover schema, calls, protocol limits, permitted event dates
and exact evidence rendering. They verify the frozen catalog, not the correctness
of source extraction or the selector's clinical relevance. Clinical equivalence
needs an explicit rubric.
Different judges should score frozen outputs, with their disagreements reported;
an evaluator ensemble does not create ground truth. Re-judging must write separate
results after validating the original acquisition evidence, not alter its config.

Report scheduled/completed/failed encounters, qualifier errors, unavailable results,
disclosure audit, tests, rounds, latency and all-role API usage. Separate medical
cost estimates, token-rate estimates and invoiced charges. Freeze generation and
wall-clock limits before held-out evaluation.

## What remains before the first patient run

The recorded-only execution path is implemented and tested with fictional inputs.
Before a real patient evaluation:

1. Freeze one initial-diagnosis episode: short opening, evidence window, disclosure
   rules and the expert's reference conclusion. Reuse the existing specialist
   records; do not infer the diagnostic target from the treatment alone.
2. Verify the selected excerpts against the original records and review the
   minimized payloads for permitted processing. Record timing gaps and conflicts.
3. Validate the private case with `prepare-recorded`. Its role projection and
   verifier enforce the tested evidence contract; they do not certify privacy or
   clinical completeness. Keep generated tasks and reports private.
4. Verify the intended deployment is approved for these records, then freeze
   model versions, provider settings and limits. A fictional cloud smoke test
   establishes connectivity, not permission to process patient records.

Clinical costing inside Harbor, matched-evidence/one-shot controls, semantic
disclosure auditing, episode-specific evaluator comparisons and patient-level
cohort analysis are later study work. Live paired gatekeeper results and actual
API bills remain unmeasured. Original MAI-DxO code/prompts/configuration are still
needed to evaluate that system; they are not needed to test the released baseline.

## Implementation order

1. Prepare one private episode and reference from existing expert records.
2. Implement source-linked evidence retrieval and test its access rules.
3. Inspect one complete encounter; treat it as integration evidence only.
4. Add frozen-output comparators and episode-specific evaluation.
5. Expand to a defined cohort with patient-level splits and analysis.

Use SDBench's `DiagnosticAgent` and `TestRunner` interfaces and Harbor's task and
verifier contracts. A new orchestration framework is not needed for this plan.
