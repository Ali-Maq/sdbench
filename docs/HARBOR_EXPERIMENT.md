# Harbor implementation and commands

This adapter runs the released SDBench baseline. Harbor is pinned to 0.24.0 and
requires Python 3.12; role images and core SDBench support Python 3.11. See the
[README](../README.md#run-a-fictional-smoke-test) for the mock quickstart and
[verification](VERIFICATION.md) for actual execution evidence.

## Architecture

```text
Preparation: fixed config + cases + prompt/source hashes
  |
  +-- Harbor diagnosis container
  |     GET /session    <- short presentation and limits
  |     POST /tests     -> questions/tests -> observations
  |     POST /model     -> diagnostic messages -> model completion
  |     POST /diagnosis -> final answer
  |                      |
  +-- Trusted gatekeeper sidecar
  |     private case + original BatchTestRunner + metered model gateway
  |     authoritative events and provider-attempt ledgers
  |
  +-- Harbor collects sidecar evidence after diagnosis stops
        -> separate verifier: validate evidence -> released judge -> reward
        -> host: SDBench HTML report, ATIF and optional metadata export
```

The sidecar service meters diagnostic calls, but its gatekeeper LLM receives only
its own role prompt. It has no filesystem or administrative tools. All three
containers share a host kernel. The diagnostic container has no private case,
reference, provider key or outbound provider network; it receives only the
presentation and disclosed observations.

| Responsibility | Source |
| --- | --- |
| Freeze paired schedule and analyze missing attempts | [experiment.py](../sdbench/isolation/experiment.py) |
| Build role images, network and verifier configuration | [tasks.py](../sdbench/harbor_integration/tasks.py) |
| Launch client and collect native trajectory | [agent.py](../sdbench/harbor_integration/agent.py) |
| Adapt the baseline/TestRunner to HTTP | [diagnosis.py](../sdbench/isolation/diagnosis.py) |
| Own case access, finalize answers, record events | [service.py](../sdbench/isolation/service.py) |
| Supply observations using released prompts | [gatekeeper.py](../sdbench/gatekeeper.py) |
| Select and verify stored evidence for private episodes | [recorded.py](../sdbench/isolation/recorded.py) |
| Validate evidence and grade | [isolation/judge.py](../sdbench/isolation/judge.py), [judge.py](../sdbench/judge.py) |
| Provider attempts and cost accounting | [providers.py](../sdbench/isolation/providers.py), [accounting.py](../sdbench/isolation/accounting.py) |
| Native ATIF and full-role HTML | [trajectory.py](../sdbench/harbor_integration/trajectory.py), [reporting.py](../sdbench/harbor_integration/reporting.py) |

Harbor's [custom-agent](https://docs.harborframework.com/agents/custom-agents),
[multi-container](https://docs.harborframework.com/tasks/multi-container),
[separate-verifier](https://docs.harborframework.com/tasks/separate-verifier) and
[artifact](https://docs.harborframework.com/jobs/artifact-collection) interfaces
provide execution. We do not add a scheduler or results UI. Native Codex/Claude
Code clinical tools are not connected to these tasks.

## Evidence and compatibility

The authenticated diagnostic session cannot select another case, model or role.
The service writes evidence to `/private/evidence`; Harbor transfers this
sidecar-owned directory to the separate verifier. The verifier checks trial,
case/config hashes, event order, turns and accepted requests before grading.
Hashes detect inconsistencies; the trusted sidecar/collection boundary establishes
origin. Lookalike agent-authored ledgers are ignored by analysis.

Generated task inventories record runtime sources, dependencies, Dockerfiles and
the pinned base image. Tasks, images and logs can contain private case data and
session credentials; keep them in the authorized research environment, not a
public registry or repository. Default outputs are under ignored `results/`.

The baseline prompt and clinical loop are reused. Deliberate adapter differences:

- Judge output must match the declared score format; the stock parser's arbitrary
  in-range digit fallback is rejected.
- SDK retries are disabled for per-attempt accounting. Gatekeeper and judge
  retain SDBench's provider retry wrapper; diagnostic gateway calls fail on a
  provider error. Gatekeeper formatting retries remain. Actual provider attempts
  are logged, and exhausted call budgets bypass retry sleeps in every role.
- A model-call cap bounds malformed responses that do not consume test turns.
- Synthetic flags stay in evaluator evidence, outside the diagnostic HTTP response.
- Packaged prompts are pinned; ambient local overrides cannot change the experiment.

Generated tasks allow 1,800 seconds for diagnosis and 600 for the verifier. The
HTTP request limit matches the diagnosis deadline because one gatekeeper request
can contain several chunks/retries. Individual provider attempts have a 120-second
timeout. Harbor controls total episode time. Report timeouts by arm; resource
limits are part of interpreting a result. Removed experimental-panel manifests
are rejected rather than silently run as baseline.

## Paired gatekeeper experiment

Each case/repetition creates two trials under one protocol hash and a reproducibly
shuffled schedule. Case, presentation, diagnostic policy/model, judge, prompts,
provider settings and limits are fixed; only `o4-mini` versus `gpt-5` changes.

| Dataset | Gatekeeper prompt | Judge prompt |
| --- | --- | --- |
| NEJM | `v6_fixed` | `v3_reasoning` |
| MIMIC | `v13_mimic` | `v4_reasoning_with_tests` |
| Synthetic fixture | `v6_fixed` | `v3_reasoning` |

The paired CLI accepts only those datasets. It uses direct OpenAI Chat Completions
in live mode; Azure also supports the separate smoke and recorded-episode commands.
No live paired comparison has been completed. Real cases require their documented
access rights and an appropriate provider route.

Preparation makes no API calls. To prepare an authorized live comparison, use
`--api-mode live` in the [quickstart](../README.md#run-a-fictional-smoke-test), select
the dataset/case set and diagnostic model explicitly, and export `OPENAI_API_KEY`
locally before running Harbor. Review limits and spending first. Common options:

```text
--diagnosis-model MODEL    --repetitions N    --max-turns N
--max-model-calls N        --max-completion-tokens N
--reasoning-effort low|medium|high            --concurrency N
--seed N                  --rate-cards PATH
```

Use `python -m sdbench.cli.isolation prepare --help` for the complete interface.
The generated job uses `n_attempts=1`; repetitions already have paired identities,
so do not add independent Harbor attempts. Preparation refuses an existing output
directory. Use Harbor's recovery mechanisms with the original tasks.

Analysis requires the frozen schedule. Missing, failed and invalid attempts count
as incorrect in primary scheduled accuracy; completed-only accuracy is also
reported. Results remain separate by dataset. Repeated attempts are clustered by
case for the paired interval, not treated as independent patients. The whole
workflow may ask different follow-ups after different gatekeeper responses;
identical-request replay is a different experiment and is not implemented.

Semantic leakage requires explicit audit labels. An explicitly requested
confirmatory result is not automatically leakage. Container separation and
`is_synthetic=False` do not establish evidence fidelity or zero leakage.

## Live Azure smoke test

This creates one fictional encounter using the same named deployment in all
roles. It is not the paired gatekeeper experiment. Replace placeholders with your
verified resource and tenant, then use the existing Azure CLI sign-in:

```bash
uv run --extra harbor python -m sdbench.cli.isolation prepare-smoke \
  --model gpt-5.4 \
  --base-url https://<resource>.cognitiveservices.azure.com/openai/v1/ \
  --case-id 0 --max-turns 5 --max-model-calls 8 \
  --max-completion-tokens 4096 --reasoning-effort medium \
  --jobs results/harbor-dashboard/jobs --output results/azure-smoke

uv run --extra harbor python -m sdbench.cli.azure_harbor \
  --tenant <tenant-id> --config results/azure-smoke/job.json

uv run --extra harbor harbor view results/harbor-dashboard/jobs \
  --host 127.0.0.1 --port 8080-8089 --jobs
```

The launcher runs native `harbor run`, refreshes a short-lived Entra token and
removes its temporary credential directory afterward. Only gatekeeper and judge
receive read-only mounts; diagnosis receives neither provider credentials nor
Azure login state. Credentials are excluded from images, configs and ledgers.

Responses calls use `store=False` and no SDK retries. If response phases are
present, only `final_answer` text triggers actions; commentary remains in private
raw evidence. Each request resends the text history without hidden reasoning or
`previous_response_id`. Token limits include reasoning tokens. Record returned
model IDs and deployment upgrade policy; a deployment name can change versions.
The paired summarizer rejects the single-model smoke manifest.

## Viewing results and reports

Harbor shows diagnostic messages, executed requests, observations and the final
answer as ATIF. This is text/action replay, not a video or hidden reasoning trace.
The agent token display covers diagnostic calls only; all-role usage is in the
trusted sidecar and verifier ledgers. An unknown cost leaves the cost/reward plot
empty; select time, or tokens when usage was recorded. Harbor's catalog-price tooltips are not
provider invoices or the paper's clinical-workup cost curve.

After a completed trial, render the existing SDBench HTML report without model calls:

```bash
uv run python -m sdbench.harbor_integration.reporting \
  --trial-dir <completed-harbor-trial-directory> \
  --case <prepared-task-directory>/tests/private/case.json
```

The exporter checks case/evidence identity and requires the saved judge output
with consistent score/correctness fields. It writes `artifacts/clinical-report/`
once, refusing to overwrite it. Open `overview.html` in a browser; Harbor's file
viewer shows HTML source. The report includes diagnostic, gatekeeper and judge
conversations, reference and gatekeeper-reported flags. Flags are not verified
source citations. Raw ledgers remain unchanged; derived per-call copies fit the
native viewer's 1 MiB limit. This report is evaluator-only, never an agent input.

For [recorded-only episodes](RECORDED_EPISODES.md), the same overview links to
`evidence.html` and `evidence.json`: opening, allowed catalog, event/source dates,
private expert reference and the IDs actually selected for each request. The
verifier checks exact rendering against the frozen catalog. Original-source
extraction and clinical relevance still require separate review.

Failed encounters cannot produce this completed-report view. Inspect:

| Artifact | Available evidence |
| --- | --- |
| `result.json` | Harbor completion/exception status |
| `agent/diagnosis.json` | Sanitized failure status; partial public dialogue is not retained |
| `artifacts/gatekeeper-evidence/events.jsonl` | Completed requests/observations and any terminal event |
| `artifacts/gatekeeper-evidence/raw_calls.jsonl` | Recorded diagnostic/gatekeeper attempts |
| `verifier/result.json`, `verifier/raw_calls.jsonl` | Grading status and attempts, if produced |

Missing diagnoses/results remain missing; do not reconstruct a successful encounter.

## Usage and cost

`usage.jsonl` in the trusted gatekeeper artifacts and `verifier/judge_api_calls.jsonl`
cover all provider attempts. Unknown usage stays unknown; cached input and reasoning
tokens are subsets, not added twice. Conflicting duplicate call IDs are rejected.

Optional rate cards map exact model names to `effective_date`, `input_per_million`,
`output_per_million`, optional `cached_input_per_million` and `source`. Estimates
use those supplied rates; `actual_paid_usd` remains null without billing reconciliation.
Do not apply public OpenAI prices to Azure automatically. Clinical-workup costing
remains available through the [stock runner](../RUN_SDBENCH.md), not this adapter.

## Optional Langfuse workflow export

By default, the [exporter](../sdbench/harbor_integration/tracing.py) reads only trusted usage
ledgers. This metadata mode includes role, status, attempts, tokens, latency and hashed technical
references; excludes case text, messages, prompts, test names and clinical scores.
Span timestamps are export time, not an original execution waterfall. Hashing
technical references is not a de-identification certification.

```bash
# Local preview; no network calls or SDK needed.
uv run python -m sdbench.harbor_integration.tracing \
  --trial-dir <completed-harbor-trial-directory> --output results/trace-preview.json
```

To send, set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`
on the host, install `--extra tracing` and add `--send` with a new output path.
The pinned SDK major supports the tested local v3 server. Check server acceptance;
SDK flush alone is not proof. Re-export appends observations, so send a reviewed
trial once. Clinical dialogue remains in local Harbor/SDBench artifacts.

For the complete clinical audit view, explicitly select `--content full` and
provide the exact frozen judge case used by a completed recorded-only trial:

```bash
uv run --extra tracing python -m sdbench.harbor_integration.tracing \
  --trial-dir <completed-harbor-trial-directory> \
  --case <frozen-private-case.json> --content full \
  --output <private-directory>/full-trace-preview.json
```

This mode validates the case and event hashes, recorded findings, final diagnosis,
and the identity/configuration and token counts of every raw API attempt against
the trusted usage ledgers. It produces a private local preview without making a
model call. It does not alter the trial, prompts, evidence or scores.

The resulting tree is:

```text
Harbor trial                         opening, submitted diagnosis, saved score
├── diagnostic agent                 opening and submitted diagnosis
│   └── each model attempt           saved messages, visible output, usage
├── gatekeeper                       each model attempt
│   └── requested test batch
│       └── each recorded retrieval  question, exact answer, evidence/source IDs
└── judge                            frozen reference, candidate and saved score
    └── each model attempt           exact judge messages and visible output
```

The full reference is added only to the judge branch. An auditor can inspect the
whole trace, but agents receive none of this export; their existing role boundaries
are unchanged. Failed provider attempts remain visible. Hidden/encrypted model
reasoning is excluded. Scores are the saved judge's assessments, not new grades.

Full-content sending requires a literal loopback endpoint such as
`LANGFUSE_BASE_URL=http://127.0.0.1:3001`, with project keys in the host environment.
Add `--send` and a new preview output path after reviewing the preview. Keep the
server private and authenticated. Observations use the public OpenTelemetry
HTTP exporter; the saved score uses the Python SDK's typed ingestion API for
**self-hosted Langfuse v3** (tested locally with v3.159.0). The score route and
trace-level attributes need migration before upgrading to v4. Metadata mode
continues to use the SDK's OpenTelemetry path.

Inside a full trace, the agent graph appears beneath the left-hand observation
tree. Expand that panel if it is collapsed; drag its divider to enlarge the graph.
Diagnostic and gatekeeper roles are
agents, the judge is an evaluator, request batches are tools, and returned findings
are retrievers. These native types enable [Langfuse's graph view](https://langfuse.com/docs/observability/features/agent-graphs)
without a LangGraph runtime. The graph is inferred from observation nesting and
timing. Role nodes group their model calls; they do not describe a compiled
LangGraph or prove per-turn control dependencies. Use the timeline and generation
observations for exact saved messages and call order.

New provider ledgers record UTC start/end times as well as monotonic latency.
The full trace uses those intervals; tool retrievals show their recorded completion
instant because individual start times were not captured. Older calls without valid
timestamps receive explicitly labeled export-time placeholders, never invented
execution times. Parent spans enclose the displayed children. This is an export of
a completed workflow, not live streaming or screen video.

Only generation observations carry token usage, avoiding double counting parent
totals. Cached/reasoning subsets remain metadata. Model names are metadata rather
than catalogue pricing inputs; API dollars stay unknown without billing evidence.
Trace and observation IDs are deterministic within the `otel-v1` export format,
so resubmitting the same completed trial updates the same observations. Legacy
span-only exports keep their old trace identity. This separation matters on v3:
changing an existing observation's type can retain both type variants in storage,
even when trace detail deduplicates them. `legacy_export_trace_id` identifies the
prior format; it does not imply that a prior export exists. Do not count export
versions as independent clinical trials.

The receipt reports OTLP transport success
and acknowledgement of the saved score. Server processing is asynchronous: compare
the stored types, content, IDs, timestamps and usage through the local API and
verify the graph in the UI before calling the export complete.
