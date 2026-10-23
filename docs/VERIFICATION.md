# Verification and known limits

Checks below were run on October 22, 2026 during [cleanup #2](https://github.com/Ali-Maq/sdbench/issues/2).
They establish software execution, not clinical validity or equivalence to MAI-DxO.
**No real patient was evaluated.**

## Cleaned baseline

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

## Remaining issues

| Issue | Scope and current consequence |
| --- | --- |
| [#7](https://github.com/Ali-Maq/sdbench/issues/7): pricing-cache identity | Optional stock cache omits pricing-source inputs; keep it disabled across source changes |
| [#8](https://github.com/Ali-Maq/sdbench/issues/8): partial clinical totals | Failed prices can leave a subtotal in raw results; inspect per-test errors before comparison |
| [#10](https://github.com/Ali-Maq/sdbench/issues/10): shared provider settings | Stock routing can mutate nested caller/default settings across calls |
| [#11](https://github.com/Ali-Maq/sdbench/issues/11): stock resume | Nonnumeric IDs and valid diagnoses containing “error” can be rerun |

The latter two affect the stock harness/provider path, not Harbor's separate
provider implementation and recovery. Clinical costing is not integrated into
Harbor. Failed encounters have raw evidence but no completed clinical HTML view.
Semantic leakage, recorded-only retrieval, longitudinal episode preparation,
one-shot controls and evaluator ensembles remain unimplemented or unevaluated.
See the [study plan](STUDY_PROTOCOL.md) before interpreting this as clinical readiness.

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
