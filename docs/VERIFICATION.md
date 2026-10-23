# What has actually been verified

These checks were run locally on October 22, 2026. They establish execution and
inspection behavior for the named configurations. They do not establish clinical
validity or equivalence to the unpublished original MAI-DxO implementation.

| Check | Observed result |
| --- | --- |
| Python 3.12 unit/integration suite with Harbor, panel and tracing extras | 131 tests passed, no skips |
| Released baseline through actual Docker + Harbor | Earlier 10/10 mock encounters completed; fresh post-review run completed 2/2, zero infrastructure exceptions |
| Baseline HTTP transport comparison | Three scripted tests matched stock requests, model inputs, public transcripts and final diagnoses |
| Earlier baseline Azure smoke artifacts | Two completed fictional encounters; saved judge scores 4/5 and 5/5, under different development configurations |
| Experimental LangGraph panel through actual Docker + Harbor | 2/2 mock encounters completed, zero infrastructure exceptions; one paired fictional case |
| Experimental panel with live Azure GPT-5.4 | One fictional encounter completed; judge score **2/5**, reward **0** |
| Langfuse SDK tests | Real SDK exported parent/child observations to an in-memory OpenTelemetry collector |
| Local Langfuse server readback | Live encounter metadata appeared as one parent plus eight API-attempt observations |
| Distribution | Wheel built; panel code, pinned container requirements, and tracing exporter are included |

## Review corrections and remaining limitations

Public source was reviewed read-only with Claude Code, followed by a discussion
of its findings. Reproducible findings were checked independently; review agreement
is not test evidence. No patient files were supplied to that review.

- Fixed report labels that treated `is_synthetic=False` as verified recorded
  evidence. Regression tests cover the upstream parser's missing-tag and missing-result
  fallbacks, plus missing/invalid saved flags. The upstream parser is unchanged.
- Aligned the Harbor client's HTTP request timeout with the existing 1,800-second
  encounter deadline; a gatekeeper request can legitimately include multiple
  provider and formatting retries. Task/adapter tests check deadline consistency.
- Added scripted stock-versus-HTTP baseline tests for request limits, forced final
  diagnosis and malformed-response recovery. They do not establish live-model or
  published-score equivalence.
- Regenerated the saved fictional clinical report with corrected labels, preserving
  its previous rendering and original evidence. No diagnosis was rerun for that edit.

The new Docker smoke used the released baseline on one fictional case under two
mock conditions, with three investigation turns and eight model calls per role
allowed. Both completed; fixed mock score 3/5 and reward 0 are expected.

Failed encounters remain inspectable through trusted raw-call/event artifacts,
but cannot yet produce the completed clinical report. The paired route is direct
OpenAI; Azure is supported by the separate smoke launcher. Custom episodes,
recorded-only evidence retrieval, one-shot conditions and evaluator ensembles
remain unimplemented.

## Earlier live baseline artifacts

Reinspection of the saved `azure-gpt54-smoke` and `azure-gpt54-phase-aware`
artifacts confirmed completed live synthetic encounters, the released baseline's
system prompt, and judge scores 4/5 and 5/5 respectively. These predate the panel
run and used different development configurations. They are not independent
patient samples or a controlled baseline-versus-panel comparison. No additional
live call was made during this review.

## Live panel encounter configuration and outcome

The live smoke used bundled synthetic case `0`, the `experimental_panel` policy,
and the same GPT-5.4 deployment for diagnosis, gatekeeper, and judge. The provider
route was Azure's Responses API. Its limits were three investigation turns, eight
model calls per role, and 4,096 completion tokens per call (including reasoning).
The gatekeeper used `v6_fixed`; the judge used `v3_reasoning`.

The saved run contained four diagnostic calls, three gatekeeper calls, and one
judge call: **eight provider attempts and 52,005 total reported tokens**. All
three investigation turns were used before the forced final-answer step. The
judge marked the diagnosis incorrect, at **2/5**. This result is retained rather
than rerunning until the example succeeds. A short integration smoke is not a
comparison of diagnostic strategies, nor evidence that more turns would have
changed the result.

The saved dialogue also contains two gatekeeper requests for a more specific
test request. This is an observable interaction issue to examine before clinical
evaluation; successful transport does not imply that every request was useful.

The complete visible dialogue, reference answer, and judge explanation were
rendered with the existing SDBench HTML reporter. Harbor retained the diagnostic
ATIF trajectory and protected role artifacts. No real patient record was used.

Clinical-workup costs were not calculated. API charges were not reconciled with
Azure billing. Unknown dollars remain unknown; token counts are not an invoice.

The saved live trial retains the older Harbor adapter label
`sdbench-isolated-baseline`; its frozen manifest, diagnostic transcript, and clinical
report identify the actual policy as `experimental_panel`. The adapter is now
named `sdbench-isolated` for new runs. Historical execution artifacts were not
rewritten to change their labels.

## Mock checks

The mock backend returns fixed transport-test responses. Its judge deliberately
returns score 3 and reward 0. Both baseline and panel mock runs therefore had zero
clinical rewards despite successful execution. This is expected and is not a
measurement of medical accuracy. The baseline run was frozen before the panel
extension; the panel was tested separately afterward.

## Trace scope

The Langfuse export contains allowlisted API metadata: role, status, attempt,
reported token counts, recorded API latency, and hashed technical references.
It contains no case narrative, test names, reference answer, prompts, or model
conversation. Its span timestamps describe post-run export, not original model
execution. Clinical dialogue remains in the local Harbor/SDBench artifacts.

The actual run was exported to an existing local Langfuse 3.159.0 installation
with compatible Python SDK 4.18.0. The public API confirmed nine observations,
including eight API attempts. SDK flush alone is not treated as server acceptance.

## Reproduce the checks

Use the [Harbor quickstart](../README.md#try-harbor-without-model-api-calls) for the
baseline, [panel commands](AGENT_EXTENSIONS.md) for the experimental policy, and
[Azure smoke instructions](HARBOR_EXPERIMENT.md#live-azure-smoke-test) for a live
synthetic encounter. Live calls require your own authorized deployment and incur
provider charges. Exact versions are recorded in `uv.lock`; generated tasks also
record code, configuration, prompt, image, and dependency hashes.

Raw local artifacts, credentials, and private deployment settings are not
published. GitHub Actions runs the nonclinical suite on Python 3.11 and 3.12;
Harbor-only checks require Python 3.12. The workflow does not make model calls.

## Research work still required

- Obtain the authors' original MAI-DxO implementation and matching prompts if exact
  workflow comparison is the goal. This experimental panel does not fill that provenance gap.
- Define and review longitudinal episodes, information availability cutoffs, and
  reference diagnoses before evaluating real patient records.
- Run a preregistered paired gatekeeper comparison on authorized datasets with
  fixed diagnostic policy, judge, limits, repeated attempts, and failure accounting.
- Measure semantic disclosure and observation quality; container separation alone
  does not establish that a gatekeeper response is free from leakage.
- Add clinical-workup costing and reconcile API estimates with provider billing.
