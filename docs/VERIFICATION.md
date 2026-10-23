# Verification and known limits

Checks below cover [cleanup #2](https://github.com/Ali-Maq/sdbench/issues/2)
and its costing, routing and resume follow-ups.
They establish software execution, not clinical validity or equivalence to MAI-DxO.
**No real patient was evaluated.**

## Follow-up verification

| Check | Observed result |
| --- | --- |
| Python 3.12, all optional integrations | 217 tests passed, no skips |
| Python 3.11, compatible optional integrations | 210 passed; seven Harbor-only tests skipped because Harbor requires Python 3.12 |
| Python 3.11 compilation; Ruff F401/F811/F821/F841; whitespace | Passed |
| Pricing regressions | Partial/all-failed/zero totals, legacy missing coverage, report/recost denominators and pricing exceptions preserving diagnoses |
| Cache regressions | Effective routing/source identity, local FAISS rebuild/reuse, transient failure recovery, corrupt caches and interrupted/concurrent atomic writes |
| Routing/resume regressions | Sequential and concurrent argument isolation, endpoint/credential changes, string IDs, malformed/mismatched records and completed pricing failures |
| Browser inspection | Fictional partial-price report expands correctly; complete totals stay Unknown while known subtotal and unpriced tests remain visible |

Provider calls in these tests are scripted. They validate execution and failure
handling, not the medical correctness of a price, diagnosis or judge score.

## Original cleanup verification (02db899)

| Check | Observed result |
| --- | --- |
| Clean Python 3.12 environment, all retained extras | 148 tests passed, no skips |
| Python 3.11 source compilation | Passed |
| Unused imports/variables and undefined-name checks | Ruff F401/F811/F821/F841 passed |
| Wheel and source distribution | Built; restricted data and removed panel absent |
| Wheel installed outside checkout on Python 3.11 | Five bundled cases loaded; baseline import and five CLI help entrypoints passed |
| Actual Docker + Harbor 0.24 | 10/10 mock encounters completed, zero exceptions or retries |
| Frozen paired summary | Five complete pairs; 40 mock calls, zero live calls; all three roles accounted for |
| Native ATIF and local clinical report | Generated from the new run; diagnostic, gatekeeper and judge evidence retained |
| Baseline transport parity | Three scripted comparisons match stock requests, model inputs, public transcripts and final diagnoses |
| Optional Langfuse | SDK tests export parent/child observations to an in-memory collector; no server export in this cleanup |

The Docker run used the five bundled fictional cases, two mock conditions,
five investigation turns and twelve model calls per role allowed. The fixed mock
judge returns 3/5 and reward 0. These are transport fixtures, not a medical result.
Mock token usage is unknown. No clinical-workup cost or provider invoice was measured.

Use the [README quickstart](../README.md#run-a-fictional-smoke-test) to reproduce
this path. The local job is named `cleanup-baseline-20261022`; private/generated
artifacts are not committed. Task manifests retain exact source, prompt,
dependency, image and configuration hashes. GitHub Actions tests Python 3.11/3.12
and checks the wheel outside the checkout without calling models.

## Review and corrections

Claude Code reviewed public-source snapshots read-only before and after cleanup.
Direct review and scripted regressions checked findings before changes. The second
pass found no blocking regression, but identified a batch-name normalization edge
case that was corrected. Reviewer agreement is not test evidence. Patient records,
credentials and raw clinical artifacts were excluded.

The cleanup removes the experimental panel and its dependencies, obsolete
publishing assets, duplicate documentation and unused helpers. It also corrects:

- Batch prices attributed by response position instead of test name, including
  ambiguous and nonfinite prices.
- Cumulative CLI turn counts and overly broad Azure test-blob cleanup.
- Duplicate executor caching that could return a closed executor after shutdown.
- Retrying an exhausted role budget and misclassifying internal failures as bad requests.
- Analysis without a frozen schedule, which could omit entirely missing pairs.

## Four follow-up corrections

| Issue | Implemented correction |
| --- | --- |
| [#7](https://github.com/Ali-Maq/sdbench/issues/7): pricing-cache identity | Versioned effective routing/model arguments, pricing implementation and charge-master/embedding fingerprints; matcher and disk-index invalidation |
| [#8](https://github.com/Ali-Maq/sdbench/issues/8): partial clinical totals | Explicit complete/partial/unknown status, nullable complete totals, known subtotals and pricing coverage across raw results, CLI and reports |
| [#10](https://github.com/Ali-Maq/sdbench/issues/10): shared provider settings | Request-local nested routing values; cached clients follow current endpoint, API version and credentials |
| [#11](https://github.com/Ali-Maq/sdbench/issues/11): stock resume | String case IDs and explicit completion/identity checks; diagnosis text is never an error channel |

Pricing exceptions preserve completed diagnostic and judge evidence. Resume does
not repeat those calls; recost retries missing pricing separately. Old records
without declared pricing coverage or judge evidence are treated conservatively.
Report rows distinguish incomplete diagnoses from incorrect completed diagnoses.
All aggregate clinical-cost reports use completed diagnoses, retain incomplete
pricing in that population and disclose the excluded diagnostic failures.

Claude's read-only follow-up review and independent review identified the pricing
exception path, failed-cache recovery and misleading coverage/status displays.
Each correction has a focused offline regression. Reviewer conclusions are not
substitutes for execution evidence.

## Remaining validation and study work

No live Cosmos service or real pricing-provider integration was exercised in
these follow-ups. FAISS and cache recovery use local files and scripted embedding
responses. Routing tests use mocked SDK transports. Remote changes behind mutable
model aliases cannot be detected offline. Keep inputs fixed during a price call
and restart processes after code changes. Old cache/index files are ignored,
not deleted. Per-test batch-price caching remains memoization of a test estimate;
it does not replay the original batch prompt or reproduce stochastic generations.

The stock judge's permissive score parser remains upstream behavior; stock resume
checks saved score/flag consistency and nonempty output, not a second clinical
judgment. Harbor already enforces its stricter declared score format. Stock
routing/resume changes do not replace Harbor's own provider path or recovery.

Before the first patient run, implement private episode preparation and the
source-linked recorded-only contract, freeze the expert reference and disclosure
window, and verify the intended provider route. These are specified in the
[study plan](STUDY_PROTOCOL.md#what-remains-before-the-first-patient-run).

Clinical costing inside Harbor, invoice reconciliation, live paired gatekeeper
results, semantic disclosure evaluation, one-shot controls, evaluator ensembles
and patient-level cohort analysis remain unimplemented or unmeasured. Failed
encounters have raw evidence but no completed clinical HTML view. Native
Codex/Claude Code clinical tool adapters and full clinical Langfuse export are
not implemented. Original MAI-DxO remains unavailable in the inspected sources;
this extension evaluates the released baseline.

## Historical fictional runs

Earlier baseline Azure runs saved judge scores of 4/5 and 5/5 under different
development configurations. They are not a controlled comparison.

The removed experimental panel completed one live fictional encounter at **2/5,
reward 0**, with eight provider attempts and 52,005 reported tokens. Its historical
adapter label was `sdbench-isolated-baseline`; the frozen manifest and transcript
identify `experimental_panel`. The unfavorable result is retained in local
artifacts and Git history. It is neither original MAI-DxO nor a patient evaluation.

That earlier encounter's metadata reached a local Langfuse server as one parent
plus eight attempt observations. Export timestamps represented post-run export,
not an execution waterfall. Clinical-workup costs were not calculated and API
charges were not reconciled with billing. No live diagnosis was rerun during cleanup.
