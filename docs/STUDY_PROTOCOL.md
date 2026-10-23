# Study plan: diagnostic episodes from longitudinal records

**Status: design, not a completed patient evaluation or preregistered protocol.**
The current software runs the released baseline and a gatekeeper-model comparison
on SDBench datasets. This document separates that implementation from the proposed
longitudinal study. It contains no patient-level data.

## Question and scope

Can the released SDBench sequential diagnostic workflow reach the documented
clinical conclusion for a defined episode, and how do its information requests,
unsupported findings and costs depend on the gatekeeper?

The first episode type is **initial diagnosis before myeloma is established**.
Later progression and complication episodes can follow, each with its own clinical
question and reference. An entire patient history is not one diagnosis task.
The study uses existing expert records as reference evidence and for a descriptive
historical workup comparison. It does not require the expert to recreate those records.

The paper's MAI-DxO improvement is relative to an already-sequential baseline
([supplementary methods, baseline evaluations and Table S9](https://www.science.org/doi/suppl/10.1126/science.aeh7930/suppl_file/science.aeh7930_sm.pdf)).
Our released-baseline study tests transfer to a new setting and gatekeeper
robustness; it cannot verify or refute the unavailable original MAI-DxO policy's
gain. The [optional panel](AGENT_EXTENSIONS.md) is outside this primary study.

## What an episode must specify

| Item | Purpose |
| --- | --- |
| Clinical question | What uncertainty should this encounter resolve? |
| Starting presentation | Brief symptoms/context, limited to what is known at entry |
| Known history | Earlier diagnoses and treatments genuinely known at that point |
| Allowed evidence window | Which source findings can answer investigation requests |
| Reference conclusion | Expert's documented answer to this episode's question |
| Supporting sources | Notes/reports supporting that conclusion, with relevant timing |
| Required specificity | Which qualifiers are essential, optional, conflicting or unknown |
| Missing-result rule | What happens when the requested finding is absent |

Reuse the signed specialist assessment and supporting reports. A treatment choice
can support interpretation of the record, but is not automatically the diagnostic
answer: the task may ask about cause, stage, disease status or a complication.
Flag concrete contradictions rather than demanding another blanket review.
Do not convert an uncertain subtype into a required exact-match answer. Freeze
the reference wording and essential qualifiers before scoring: a disease-only
reference and a disease-plus-complication reference can yield different scores.

For an initial-diagnosis task, the reference may be the disease and its defining
manifestations. For a later episode, known myeloma belongs in the opening history;
the answer must address the new clinical uncertainty. Stage, progression and the
cause of a new symptom are distinct targets, not interchangeable labels.

An initial-diagnosis cohort containing only myeloma patients supports a feasibility
or conditional-recognition study. It cannot establish specificity against other
conditions. A constant “multiple myeloma” response is a necessary negative control
for such a cohort, even though the judge is semantic. Later episodes can have
varied reference diagnoses without requiring every patient to have a different
underlying cancer. Cohort names and answer-bearing metadata must stay out of the
agent's context. Availability of non-myeloma initial workups remains unresolved.

## Evidence timing and the gatekeeper

The current [`CaseData`](../sdbench/case_data.py) holds a summary, full narrative
and final diagnosis. It does not enforce result-availability timestamps. The
stock gatekeeper and judge share the same `full_text` field. Separating permitted
gatekeeper evidence from privileged evaluator context requires a new episode
contract; the existing containers alone do not create that data separation.
The gatekeeper can synthesize absent findings:
see the [NEJM prompt](../sdbench/prompts/gatekeeper_synthetic_tests/v6_fixed/prompt.yaml)
and [MIMIC prompt](../sdbench/prompts/gatekeeper_synthetic_tests/v13_mimic/prompt.yaml).
The MIMIC prompt explicitly interpolates the final diagnosis and reconstructs
missing/pre-treatment findings. The NEJM template does not interpolate the
separate diagnosis field, although its full narrative can contain the answer.
Missing-result handling also differs between questions and test orders.

The requested longitudinal variant is **recorded evidence only**: return a
source-supported observation, or “not documented.” Preserve the released
baseline loop, but label this gatekeeper policy as an extension. It is not
implemented by merely placing that instruction in the initial patient summary.
The new policy must receive only the allowed evidence, excluding the separate
reference answer and answer-bearing assessment/treatment sections. The prompt
family and evidence-extraction rules remain to be frozen; stock prompts are not
silently edited. Missingness itself can reveal which investigations clinicians
performed, so record and analyze unavailable-result rates.

The proposed first mode is episode-bounded retrospective retrieval: a later
recorded result may be disclosed when its investigation is requested, carrying
its original relative timing. That does not assert it existed at the first visit.
A true as-of-time replay would additionally require reliable availability times
and a defined clock-advance rule; those are not implemented.

Define leakage relative to this policy. An explicitly requested confirmatory
biopsy result is not automatically a leak. An unsolicited final diagnosis,
unrequested future treatment response, out-of-episode finding or unsupported
result is a different problem. The report's `is_synthetic` flag is model/parser
metadata, not an independently verified citation to source evidence.

## Three separate comparisons

| Comparison | Hold fixed | Interpretation and implementation status |
| --- | --- | --- |
| Sequential versus one-shot | Model version, episode, scoring rule and generation settings | Proposed; compares investigation with immediate diagnosis, not pure reasoning under equal information |
| Model workup versus expert's recorded workup | Episode boundaries and comparable test-cost method | Proposed descriptive historical comparison; clinicians had different information and constraints |
| `o4-mini` versus `gpt-5` gatekeeper | Diagnostic policy/model, judge, case, prompts, provider and limits | Paired runner exists for NEJM/MIMIC/synthetic using direct OpenAI; Azure is smoke-only, and custom episodes/live comparison remain unimplemented |

For the first comparison, retain three clearly named conditions if resources allow:

1. **Opening-only one-shot:** the same initial presentation, no observations acquired.
2. **Sequential baseline:** requests observations through the gatekeeper.
3. **Matched-evidence final answer:** one diagnosis call using exactly the observations
   collected by the sequential run. This is a conditional replay because the
   sequential policy chose those observations; it is not an independently designed workup.

One-shot also needs a declared instruction to answer without investigation;
the released baseline prompt instead invites tests. Record that prompt difference
rather than claiming identical instructions. The current paired configuration
requires at least one investigation turn and has no one-shot policy.

An all-allowed-evidence one-shot can be an additional reference condition, with
its larger information access reported. Do not pool it with opening-only runs.
Do not use the historical expert outcome as a randomized human-versus-AI result.
Freeze each comparison's primary endpoint and resource rules before evaluating
held-out patients, including the [wall-clock limits](HARBOR_EXPERIMENT.md#runtime-limits);
“run every model against every judge” is not a study design.

## What the judge measures

The released [judge code](../sdbench/judge.py) sends the candidate answer,
reference and full case to an LLM. Its [rubric](../sdbench/prompts/check_diagnosis/v3_reasoning/prompt.yaml)
assesses disease, cause, anatomy, specificity and incorrect additions. The MIMIC
[prompt](../sdbench/prompts/check_diagnosis/v4_reasoning_with_tests/prompt.yaml)
also includes ordered tests and their gatekeeper-reported synthetic flags.
Those flags can affect judging and are not independent provenance checks.
This is **not keyword matching**.

The released outcome is score 1–5, with 4–5 counted as correct. A score of 4 can
accept omitted qualifiers; the rubric explicitly allows a cancer diagnosis
without the reference stage in one example. Consequently, a passing SDBench
score cannot by itself establish correct staging, progression assessment or
management. Keep this score as a compatibility measure.

For each new episode type, predeclare an additional primary clinical endpoint
that answers its actual question, including essential qualifiers and acceptable
uncertainty. Preserve the structured reference separately from candidate outputs. A future
re-judging entry point must validate original frozen acquisition evidence, then
write separate evaluation results; changing the original hashed trial configuration
to swap its judge would invalidate that evidence.
For the initial-diagnosis pilot, disease-level correctness is the starting endpoint;
staging or longitudinal status requires a separately specified evaluation.

| Evaluation | Appropriate method |
| --- | --- |
| Call counts and protocol/schema violations | Deterministic code checks; existing logs support these |
| Permitted source IDs and time boundaries | Proposed deterministic checks; require the new evidence contract |
| Clinical equivalence and completeness against the episode reference | Released judge plus a declared episode-specific rubric when needed |
| Unsupported or unrequested disclosure | Source-linked audit; LLM assistance does not establish source truth |
| Judge sensitivity | Different judge models score the same frozen outputs with fixed rubrics; report disagreement |

Do not average judge scores into “truth.” Select one primary rule, blind evaluators
to model/arm where feasible, retain invalid outputs, and resolve material clinical
ambiguities using the existing supporting evidence or targeted expert review.
A deterministic string matcher can recognize an approved synonym; it cannot
replace clinical evaluation. There is no “deterministic LLM” guarantee.

## Minimal architecture to implement

```text
Private longitudinal record
  -> episode preparation [NEW: source selection, timing, reference]
       -> brief presentation --------------------> diagnostic agent
       -> permitted evidence -> gatekeeper <-----> diagnostic agent
       -> reference + sources -------------------> separate evaluator

Harbor [EXISTING]: schedule -> containers -> frozen artifacts / usage / ATIF
Frozen answers [NEW]: one-shot comparisons + episode metrics + judge sensitivity
Local report [EXISTING]: visible dialogue, reference, judge and request log
Langfuse [EXISTING, OPTIONAL]: post-run API metadata only
```

Reuse `CaseData`, `DiagnosticAgent`, `TestRunner` and Harbor's task/verifier
interfaces. Add episode preparation and evaluation contracts where needed;
do not replace the released baseline with a new agent framework. LangChain
is not required to run deterministic checks or multiple judge calls. LangGraph
currently belongs only to the optional panel, and Langfuse currently observes
API metadata; neither implements this study's evaluation layer.

## Reporting and next implementation steps

Keep all episodes from a patient in one development/evaluation split. Analyze
repeated runs and episodes as correlated observations; confidence intervals must
cluster by patient. The current paired analyzer clusters by case, which suffices
only when each case is an independent patient. Do not claim patient-level analysis
for a multi-episode cohort until that mapping exists.

Report correctness, essential qualifier errors, missing results, disclosure audit,
requested tests, rounds, failures, latency and all-role API usage. Keep estimated
medical-workup cost, token-rate API estimates and actual invoiced charges separate.
The adapter has not integrated clinical costing or billing reconciliation.

Implementation order:

1. Prepare one private episode with a short opening, source-linked evidence and
   reference drawn from existing expert records; list unresolved fields explicitly.
2. Implement and test the declared evidence policy, including missing results and
   out-of-episode requests. Preserve stock behavior as a named separate condition.
3. Run that episode end to end and inspect every disclosed finding and score.
   One patient establishes integration feasibility, not clinical performance.
4. Add the frozen-output comparators and evaluator sensitivity analysis.
5. Expand to a defined cohort with patient-level splits and a prespecified analysis.

Original MAI-DxO can become another diagnostic policy if its authors release
matching code and prompts. It is not a prerequisite for evaluating the released
baseline, and the current experimental panel is not a substitute for it.
