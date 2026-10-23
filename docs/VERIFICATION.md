# Verification and known limits

Checks below cover [cleanup #2](https://github.com/Ali-Maq/sdbench/issues/2)
and its costing, routing and resume follow-ups.
They establish software execution, not clinical validity or equivalence to MAI-DxO.
The public checks below use fictional cases. Private evaluation records and
their clinical outcomes are not redistributed in this repository.

## Full local Langfuse export (2026-10-23)

[Issue #14](https://github.com/Ali-Maq/sdbench/issues/14) adds an opt-in audit
export of completed recorded-only trials. Diagnostic and judge prompts are
unchanged. Provider ledgers now also record UTC start/end timestamps.

| Check | Observed result |
| --- | --- |
| Python 3.12, all extras | 262 tests passed |
| Python 3.11, compatible extras | 254 passed; eight expected Harbor-only skips |
| Focused Langfuse checks | 17 tests passed, including altered case/usage rejection, role isolation, exact evidence, failed attempts, unknown usage, timing and ingestion acknowledgements |
| Native Harbor fictional run | Completed without exceptions; scripted judge 3/5 and reward 0, as expected for this transport fixture |
| Local Langfuse v3.159.0 | Server API readback and browser show 11 observations: root, three roles, four model attempts, one request batch and two retrievals; saved score 3/5 |
| Content and timing | Opening, saved diagnosis, reference and retrieved text match the saved artifacts; provider intervals use recorded UTC timestamps; retrievals show completion instants |

The initial server integration rejected six semantic observation types even
though the SDK accepted their typed objects. The corrected v3 adapter represents
roles and tools as spans, retaining their purpose in names and metadata. Stable
observation IDs repaired the partial fictional trace without duplicate nodes.
Unit tests now enforce the server's supported types. No clinical data was used
to debug that compatibility issue.

This is a completed-run export, not live streaming. No new judge is run during
export. Model names remain metadata to avoid automatic catalogue price estimates;
actual API charges remain unknown. See the [workflow and commands](HARBOR_EXPERIMENT.md#optional-langfuse-workflow-export).

## Evidence-aware retrieval verification (2026-10-23)

`recorded-id-selector-v2` supplies the selector with allowed excerpt text and
relative dates, so it can check what a record actually says. The diagnostic
agent, judge and deterministic observation rendering are unchanged. The
[fictional retrieval fixture](../tests/fixtures/recorded_retrieval_cases.json)
contains five catalogs, 40 evidence entries and 20 requests with independently
specified expected ID sets. It covers unrelated ROS headings, explicit negatives,
partial compound requests, hsCRP versus ESR, full CBC/differential and SPEP/IFE
panels, narrow analytes, placeholders, zero values and absent or similar-name tests.

| Check | Observed result |
| --- | --- |
| Python 3.12, all extras | 253 tests passed, no skips |
| Python 3.11, compatible extras | 245 passed; eight expected Harbor-only skips |
| Scripted retrieval boundary tests | Four tests pass: actual excerpts/dates reach the selector, reference and expected answers do not; chosen IDs render exact sources, including partial answers and missingness |
| Live selector, final prompt | 60/60 expected-ID-set matches: 20 requests repeated three times, batched into 15 provider calls |
| Live settings | Azure Responses API; deployment `sdbench-gpt-5.4-dz`; reasoning effort `low`; 4,096 maximum completion tokens; selector only, no diagnostic or judge calls |
| Final live usage | 17,595 input + 2,227 output = 19,822 tokens; API dollar charges unknown |
| Final native Harbor run, fictional episode | Completed with the same v2 source; three investigation rounds, 33 requests, four recorded responses and 29 `not documented` responses; exact-source validation passed and the judge scored the submitted diagnosis 5/5 |
| Final Harbor usage | Eight calls across all roles; 9,643 input + 1,745 output = 11,388 tokens; API dollar charges unknown |

The first live attempt passed 54/60 checks: it omitted the structured IFE
placeholder when the separate interpretation was available. That attempt is
preserved. The prompt was clarified to retain requested placeholder records
without treating them as negative results, then the same fixture passed 60/60.
This is a development regression set used to refine the prompt, **not a held-out
accuracy estimate**. Three repetitions are not 60 independent clinical cases.

Reproduce the offline checks from the repository root:

```bash
uv sync --python 3.12 --all-extras
uv run --no-sync python -m unittest discover -s tests -p 'test_recorded_retrieval.py' -v
uv run --no-sync python -m unittest discover -s tests -v
```

Those tests script the returned IDs and validate the interface; they cannot
establish model retrieval accuracy. The live check instead passed each catalog
through `RecordedTestRunner` with `MeteredBackend`, submitted its requests in one
batch, and compared returned ID sets with the fixture after the call. Expected
IDs never entered model input. The one-off live driver and its raw ledgers are
local audit artifacts, not a published CLI. The final `live-selector-check`
settings and results record source hash
`85c61ebde7c2ce6daeff5340198e23caf95c552b93fe0aca6f5ae239ac58c03d`
and fixture SHA-256
`3ec81b4c940532ddbdc3d8a7e0bd5049fce5fca80115789490f0f9db96346b34`.
The retained earlier attempt is `live-selector-attempt1`.
The final native job is `fictional-harbor-final`, using the same frozen source
hash. Its score checks execution on an authored fixture, not clinical accuracy.

These checks do not certify retrieval completeness on another chart or quantify
diagnostic accuracy. An ID-only partial answer still does not enumerate each
missing component. No patient outcome is reported in this public verification.

## Recorded-only workflow verification (2026-10-23)

[Issue #12](https://github.com/Ali-Maq/sdbench/issues/12) adds the recorded-only
episode contract, selector, role projections, pre-judge source checks and protected
evidence report. The released diagnostic baseline and semantic judge are reused.

| Check | Observed result |
| --- | --- |
| Python 3.12, all extras | 249 tests passed, no skips |
| Python 3.11, compatible extras | 241 passed; eight Harbor-only tests skipped |
| Boundary/failure tests | Separate role payloads; reference-marker isolation; exact excerpts; absent findings; unknown, duplicate, wrong-kind and malformed selections; source/text/date tampering rejected before judge calls |
| Baseline compatibility | Existing request limits, mixed-turn rules, HTTP transport and stock runner regressions pass |
| Final fictional mock Docker run | One completed encounter; four mock calls, zero live calls; stored CBC and `not documented` for absent culture; scripted 3/5 score |
| Live fictional Azure run | One completed encounter, three investigation rounds, eight provider calls; four recorded responses and 32 `not documented` responses across 36 requests; reference diagnosis reached, judge 5/5 |
| Live usage | 8,338 input + 1,567 output = 9,905 tokens across all roles; API invoice/estimated dollars unknown |
| Live model identity | All roles configured as `gpt-5.4`; provider returned `gpt-5.4`, not a dated version identifier |
| Artifacts/UI | Both ATIF trajectories validate; native Harbor rollout and full-role HTML/source appendix inspected; original ledgers preserved |
| Packaging | Fresh core-only Python 3.11 wheel install outside checkout; five fictional cases, five CLI helps, recorded preparation and dependency check pass; private/restricted data excluded |
| Static checks | Selected Ruff checks and whitespace validation pass |

The final mock job is `recorded-reviewed-20261023`, with frozen source hash
`a3ed7c86924dcc6bcd6897f01ef27f2aea7744053a02e34c7491e4573e412fc3`.
The live fictional job is `recorded-azure-fictional-20261023`, frozen at
`7f0d137b3c9e590271a24c3d3bf1bb46038fe57a703d28ab9cd2bafc836b6f3b`.
The live run preceded final report-only fixes; its preserved evidence was
validated and exported with the corrected reporter. Clinical role logic did not
change between those runs. No paid diagnosis was repeated just to regenerate HTML.

Independent review and Claude's read-only public-source review found report
metadata escaping and provenance-display problems. These were fixed and tested:
recorded episodes do not infer patient origin from the dataset label, and reports
show configured versus provider-returned model identities, missing identities and
multiple identities. No patient records or credentials were included in review.

These checks establish execution and exact membership in a prepared evidence
catalog. They do not establish clinical validity, faithful original extraction,
complete retrieval, zero semantic leakage or a representative accuracy estimate.
The live fixture is authored fiction with four permitted evidence items. A
selector can miss a relevant item; `not documented` is not proof of absence in
the underlying chart. Known source conflicts, permitted processing and deployment
versions must be addressed for each real episode. API and clinical-workup costs
remain separate, unmeasured monetary quantities here.

## Follow-up verification

| Check | Observed result |
| --- | --- |
| Python 3.12, all optional integrations | 226 tests passed, no skips |
| Python 3.11, compatible optional integrations | 219 passed; seven Harbor-only tests skipped because Harbor requires Python 3.12 |
| Python 3.11 compilation; Ruff F401/F811/F821/F841; whitespace | Passed |
| Pricing regressions | Partial/all-failed/zero totals, legacy missing coverage, report/recost denominators and pricing exceptions preserving diagnoses; actual-workup-only reporting |
| Cache regressions | Effective routing/source identity, local FAISS rebuild/reuse, transient failure recovery, corrupt caches and interrupted/concurrent atomic writes |
| Routing/resume regressions | Sequential and concurrent argument isolation, endpoint/credential changes, string IDs, malformed/mismatched records and completed pricing failures |
| Browser inspection | Fictional partial-price report expands correctly; complete totals stay Unknown while known subtotal and unpriced tests remain visible |
| Final Docker + Harbor 0.24 run | 10/10 mock encounters, five complete pairs, no failures; 40 mock calls and zero live benchmark calls |
| Final artifacts | All ten native trajectories validate; full-role clinical report exports and passes completion checks |
| Final wheel and source distribution | Built and inspected; fresh core-only wheel installation loads five bundled cases, passes six CLI help checks and dependency validation |

Provider calls in these tests are scripted. They validate execution and failure
handling, not the medical correctness of a price, diagnosis or judge score.
The final job is `followups-reviewed-20261022`. Its frozen runtime source hash
`9eeb1afe6ae22eb7eceda4455894bd962b765924a1e0bef6e4cf5b3880ead89c` matches
the verified checkout. Generated evidence stays outside source control.

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
Recost excludes incomplete diagnoses and replaces result files atomically, preserving
existing diagnosis/judge evidence if writing is interrupted. Malformed CPT selection
replies remain uncached failures; explicit no-match responses retain LLM fallback.
Reports retain historical-workup costs even when agent costing is disabled.
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

The private episode command and recorded-only contract are now implemented.
Before the first patient run, verify source extraction, freeze the expert
reference/disclosure window, and confirm permitted processing and deployment
versions. These are specified in the
[study plan](STUDY_PROTOCOL.md#what-remains-before-the-first-patient-run).

Clinical costing inside Harbor, invoice reconciliation, live paired gatekeeper
results, semantic disclosure evaluation, one-shot controls, evaluator ensembles
and patient-level cohort analysis remain unimplemented or unmeasured. Failed
encounters have raw evidence but no completed clinical HTML view. Native
Codex/Claude Code clinical tool adapters are not implemented. Full clinical
Langfuse export is now available as a separate, opt-in local audit view; it does
not stream execution or add another evaluator. Original MAI-DxO remains unavailable in the inspected sources;
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
