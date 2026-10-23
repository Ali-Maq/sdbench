# Paired gatekeeper experiments in Harbor

This adapter tests whether changing the gatekeeper from `o4-mini` to `gpt-5`
changes the measured performance of a fixed diagnostic agent. It uses the
released SDBench baseline, not Microsoft's MAI-DxO orchestration system.

The initial implementation runs with **three role containers on a shared
kernel**, not three independent VMs. The paired examples use deterministic
mock responses; the separate Azure smoke command explicitly uses live calls.
Mock scores test execution and accounting, not clinical ability.

## What is working, and what is still missing

The live Azure run is an **infrastructure demonstration with a fictional patient**.
Its diagnosis, gatekeeper responses, and judge assessment are genuine model
outputs, but a successful synthetic case is not a clinical benchmark result.
The reference diagnosis is authored as part of that fictional case.

The three components have different purposes:

- **SDBench** supplies cases, the information gatekeeper, diagnostic scoring,
  medical-workup costing, and a baseline diagnostic agent.
- **MAI-DxO** is the diagnostic strategy described in the paper: one model plays
  Dr. Hypothesis, Dr. Test-Chooser, Dr. Challenger, Dr. Stewardship, and
  Dr. Checklist. Those roles belong inside the diagnostic policy. They do not
  require separate MCP servers or five additional sandboxes. The inspected
  upstream release contains the baseline, not an executable MAI-DxO implementation.
- **Harbor** supplies the execution lifecycle, environments, separate verifier,
  artifacts, scheduling, and common agent/trajectory interfaces. SDBench already
  has its own parallel, resumable harness; parallelism alone is not the reason
  to add Harbor.

The community-facing objective is a reusable clinical task interface for Harbor
agents, coupled with a controlled study of how the gatekeeper affects measured
accuracy and information disclosure. The present adapter is an initial part of
that objective, not its completion:

| Capability | Current state |
| --- | --- |
| Existing baseline, gatekeeper, and judge in three role containers | Implemented; shared host kernel |
| Genuine Azure calls and per-attempt usage records | Verified on bundled synthetic cases |
| Native diagnostic trajectory | Implemented; baseline XML actions are exported to ATIF |
| MAI-DxO specialist-panel reasoning | Not implemented; any reconstruction must be labeled as such |
| Native Codex / Claude Code clinical tools | Not wired into these tasks |
| Real NEJM / MIMIC evaluation | Requires authorized dataset access and applicable provider data handling |
| Clinical-workup costing | Existing SDBench modules available; not run by this adapter |
| Actual API dollar costs | Unknown until applicable rates/billing are supplied and reconciled |
| Live paired o4-mini versus GPT-5 results | Not run; the Azure smoke uses GPT-5.4 for all roles |

Harbor 0.24's native Codex and Claude Code integrations support MCP. Reusing them
requires exposing the existing clinical operations through MCP (or an explicit
CLI tool) and configuring the native agent's provider connection. Our bespoke
`/model` completion endpoint is not a Responses-compatible Codex gateway, and
an Azure GPT deployment is not a Claude Code backend. The current custom agent
always launches the released SDBench baseline.

Container separation prevents direct access to the reference files; it does not
prove that a gatekeeper never reveals diagnostic hints in its text. A leakage
study must separately audit requested versus disclosed information, unsupported
findings, premature diagnostic disclosure, and adversarial requests. Legitimate
confirmatory test results must not be classified as leakage merely because they
identify the disease.

## Real-case access and evaluation scope

The repository ships five fully synthetic development cases. It does not ship
unrestricted real SDBench cases. The current study reports 304 NEJM cases; their
dataset requires a separate [NEJM license](../data/processed_case_data/nejm/README.md).
The [MIMIC-IV-Ext-SDBench release](https://physionet.org/content/mimic-iv-ext-sdbench/1.0.0/)
contains 488 real patients: 163 training, 254 validation, and 71 clinician-reviewed
test cases. The published evaluation uses the latter 325, not all 488.

PhysioNet requires credentialing, the specified training, and the project data
use agreement. Its [LLM-use guidance](https://physionet.org/news/post/llm-responsible-use/)
also requires verification of the provider's data handling before sending records
to a cloud API. Azure authentication and `store=False` do not by themselves
establish those conditions. Downloaded records, generated task images, and full
audit reports must remain within the permitted research environment.

Use training cases to debug the integration. Freeze the diagnostic policy,
judge, prompts, case set, provider routing, retry policy, and limits before a
paired gatekeeper comparison. Report NEJM and MIMIC separately before any
explicitly defined pooled result. Report paired differences across repeated
encounters, failures, disclosure audits, estimated clinical-workup costs, and
all-role API usage as distinct outcomes. A single successful encounter is an
integration check, not evidence of diagnostic superiority.

## Reading Harbor's native viewer

- **Pareto** defaults to API cost versus reward. A trial with unknown `cost_usd`
  has no point on that axis. Tokens or time can be selected instead. This is not
  the paper's clinical-workup cost plot.
- **Outcomes** represents each trial as a small square. One completed case
  produces one square, not a clinical encounter report.
- **Trajectory** shows the diagnostic agent's messages and actions. A text-only
  encounter has no screen video: no browser or computer-use episode was recorded.
  Neither hidden model reasoning nor unimplemented MAI-DxO role debates can be
  reconstructed from this trace.
- **Verifier / Artifacts** retain privileged audit evidence. The native file
  viewer has a 1 MiB text limit; a large raw-call ledger can fail to open despite
  existing on disk. Do not interpret that display failure as a missing run.

The existing SDBench HTML renderer is the clinical report interface to reuse:
it already shows diagnostic, gatekeeper, and grader conversations, the reference
diagnosis, and test provenance. Reports containing privileged case evidence
belong on the evaluator side and must never be mounted into the diagnostic agent.

After a trial finishes, export its saved evidence without making new model calls:

```bash
uv run --python 3.11 python -m sdbench.harbor_integration.reporting \
  --trial-dir <completed-harbor-trial-directory> \
  --case <prepared-task-directory>/tests/private/case.json
```

This verifies the case and evidence hashes and writes
`artifacts/clinical-report/overview.html`, mapped results, and an audit manifest.
The export preserves the original ledgers and creates per-call copies small
enough for Harbor's file viewer. Open the HTML in a browser to use the existing
Agent Dialog, Grader Dialog, Gatekeeper Dialog, and Test Runner Log controls;
Harbor's file browser displays HTML source rather than rendering the page.
The notice distinguishes fictional cases, incomplete attempts, unknown API
dollars, and clinical costs that have not been computed. The summary percentage
describes only the saved encounter(s), not population performance.

## Existing framework contracts come first

The implementation was checked against the released SDBench source at
`304ce9625a046cea97d64e3b1ba5d19217b5095c` and the installed **Harbor 0.24.0**
schemas and lifecycle. Harbor requires Python 3.12; SDBench and the role images
remain compatible with Python 3.11.

| Responsibility | Existing implementation reused | Adapter addition |
| --- | --- | --- |
| Diagnostic reasoning | `sdbench.agents.baseline.run` and `DiagnosticAgent` protocol | Remote `TestRunner` and completion callable |
| Request filtering, turns, chunking, gatekeeper responses | `BatchTestRunner`, `order_tests`, packaged prompt YAML | Authenticated per-trial HTTP boundary |
| Diagnosis scoring | `check_diagnosis`, packaged judge prompt, 1–5 scale and ≥4 threshold | Finalized-evidence checks and strict output-format validation |
| Task execution | Harbor `BaseInstalledAgent`, `JobConfig`, Docker environment | Launch the existing diagnostic loop inside `main` |
| Scheduling, concurrency, retries, job identity/resume | Harbor jobs, plans and task-content locks | A prepared paired schedule; no custom worker scheduler |
| Separate judge and file transfer | Harbor separate verifier and sidecar artifacts | Dedicated judge image and explicit evidence selection |
| Trajectory and token display | Harbor native viewer, ATIF, `AgentContext` | Export public diagnostic messages/actions and provider-reported usage; protected ledgers also cover gatekeeper and judge |
| Medical test costing | Existing SDBench costing modules | Not executed by this initial adapter |

Relevant Harbor specifications: [custom agents](https://docs.harborframework.com/agents/custom-agents),
[multi-container tasks](https://docs.harborframework.com/tasks/multi-container),
[separate verifier](https://docs.harborframework.com/tasks/separate-verifier),
[artifacts](https://docs.harborframework.com/jobs/artifact-collection),
[jobs](https://docs.harborframework.com/jobs/run-a-job).
Installed 0.24.0 code/schema is authoritative when newer web documentation differs.

SDBench's `TestRunner` and injected `llm` interfaces already provide the extension
points we need. We do not replace their reasoning loop or author new clinical
prompts. The full in-process SDBench harness cannot be reused unchanged for the
three-role boundary because it holds the private case and collector beside the
diagnostic agent.

The remaining custom work is specific to this experiment: role-scoped transport,
protected evidence, fixed condition assignment, pairing by case/repetition, and
accounting for missing/failed attempts. Harbor's ordinary mean-reward metric does
not carry case identities needed for a paired case-level bootstrap.

### Deliberate deviations from the original harness

- The adapter refuses ambiguous judge output instead of accepting the original
  parser's fallback to any score-range digit. This is the same rule in both arms,
  but means this is a controlled new experiment, not exact reproduction of all
  published scores.
- Live metering currently uses the existing OpenAI SDK directly, with SDK retries
  disabled and a fixed role-bound model. This is intentionally narrower than
  SDBench's OpenRouter/Azure/local routing: it makes each SDK attempt visible to
  the ledger. Existing SDBench gatekeeper formatting retries and its injected-LLM
  retry wrapper remain in use and each provider attempt is recorded. Provider
  routing must be held constant across both arms.
- The model-call limit also bounds malformed baseline responses that do not
  consume a test turn. It is part of the frozen experiment configuration.
- Synthetic provenance remains in private evidence for the judge; it is not
  exposed through the diagnostic agent's HTTP response.
- The three packaged prompt pairs are pinned experiment settings, checked against
  SDBench's `science` configuration in tests. This avoids executing ambient local
  config overrides during manifest creation. Synthetic smoke cases use the NEJM
  prompt pair, explicitly, rather than claiming a published synthetic benchmark.

## Run the mock experiment

Docker must be running. From the repository root:

```bash
uv sync --python 3.12 --extra harbor
uv run --extra harbor python -m unittest discover -s tests -v

uv run --extra harbor python -m sdbench.cli.isolation prepare \
  --dataset synthetic --case-set train \
  --max-turns 5 --max-model-calls 12 \
  --api-mode mock --output results/harbor-smoke

uv run --extra harbor harbor run --config results/harbor-smoke/job.json

uv run --extra harbor python -m sdbench.cli.isolation summarize \
  --manifest results/harbor-smoke/manifest.json \
  --jobs results/harbor-smoke/jobs \
  --output results/harbor-smoke/summary.json
```

Five bundled synthetic cases × two gatekeeper conditions × one repetition gives
ten encounters. Both conditions use fake responses in mock mode. The fake judge
returns score 3 deliberately, so a healthy smoke run has **ten completed
encounters, no infrastructure failures, and zero clinical rewards**.

Preparation makes no API calls and refuses to overwrite an existing destination.
Use a new output directory for a new preparation. Each repetition gets an explicit
paired identity, so the generated Harbor job sets `n_attempts=1`; do not add Harbor
attempts on top of `--repetitions` without updating the pairing protocol. Use
Harbor's own job recovery/diff mechanisms with the original generated tasks.

The core suite can also run without Harbor:

```bash
uv run --python 3.11 python -m unittest discover -s tests -v
uv run --python 3.11 python -m compileall -q sdbench
```

The two Harbor-specific schema/adapter checks are skipped if Harbor is absent.

## Live Azure smoke test in Harbor's own dashboard

`prepare-smoke` makes one bounded encounter using a bundled synthetic case and
the same explicitly selected deployment for diagnosis, gatekeeper and judge.
This is **not** the paired `o4-mini`/`gpt-5` experiment. It does not recreate
MAI-DxO or establish clinical performance.

Use your verified Azure resource endpoint and deployment name. For example,
replace `<resource>` and `<tenant-id>` below with your resource and tenant:

```bash
uv run --extra harbor python -m sdbench.cli.isolation prepare-smoke \
  --model gpt-5.4 \
  --base-url https://<resource>.cognitiveservices.azure.com/openai/v1/ \
  --case-id 0 --max-turns 5 --max-model-calls 8 \
  --max-completion-tokens 4096 --reasoning-effort medium \
  --jobs results/harbor-dashboard/jobs --output results/azure-gpt54-smoke

uv run --extra harbor python -m sdbench.cli.azure_harbor \
  --tenant <tenant-id> --config results/azure-gpt54-smoke/job.json

uv run --extra harbor harbor view results/harbor-dashboard/jobs \
  --host 127.0.0.1 --port 8080-8089 --jobs
```

The launcher delegates the entire job to `harbor run`. It uses your existing
`az login` to acquire a short-lived Entra token, refreshes it before expiry, and
removes its private temporary directory when the run ends. Docker Compose mounts
that directory read-only into gatekeeper and judge. Diagnosis receives neither
the provider credential nor your Azure login cache. Credentials are not placed
in task configs, images, process arguments, or run artifacts.

The Azure adapter uses the existing OpenAI SDK's Responses API with `store=False`
and SDK retries disabled. It maps the returned text into SDBench's existing
completion interface and preserves the original response and usage in the
protected ledger. When Responses supplies message phases, only `final_answer`
text is actionable; intermediate `commentary` is retained in raw evidence.
Retained assistant answers are replayed with `phase: final_answer`, so progress
messages cannot accidentally trigger duplicate clinical requests. The exact
Responses input is also logged. Each call resends SDBench's text history; no hidden reasoning
state or `previous_response_id` is carried between calls. Responses token caps
include reasoning tokens as well as visible output.

Open the job and then its trial in the viewer. The native trajectory shows the
diagnostic agent's conversation, test requests, observations and final diagnosis.
The protected `artifacts/gatekeeper-evidence/raw_calls.jsonl` and
`verifier/raw_calls.jsonl` retain gatekeeper and judge prompts and responses for
local inspection. The verifier result contains the grade and judging explanation.
This is a text/action trace, not a screen recording or access to hidden model
reasoning. The diagnostic agent never receives those privileged artifacts.

Harbor's agent token display covers diagnostic calls; all-role provider usage is
in `artifacts/gatekeeper-evidence/usage.jsonl` and
`verifier/judge_api_calls.jsonl`. Requested deployment names and returned model
identities are both retained. Azure dollar cost remains unknown without an
applicable rate card/billing reconciliation. Do not apply public OpenAI prices
to Azure charges automatically. Harbor 0.24's native token-chart tooltips may
independently show generic LiteLLM catalog estimates even when our saved cost
fields are unset; those are not Azure invoice amounts. Also capture the Azure
deployment's current model version and upgrade policy: a named deployment can
auto-upgrade even when its current version has a dated identifier. The paired `summarize` command intentionally
rejects this single-model smoke manifest.

## Role boundaries and evidence

Each task's `main` container contains source code, a session credential, and the
diagnosis client. It has no full case, reference diagnosis, clinical prompt YAML,
or provider key. The initial presentation comes from `GET /session`.

The gatekeeper sidecar owns `BatchTestRunner` and the private case. Its authenticated
API accepts only `/tests`, `/model` and `/diagnosis`, plus the read-only session.
The diagnostic credential cannot choose another case, model, provider, or role.
The service also hosts the trusted diagnostic-model metering endpoint; the
gatekeeper LLM has no server administration or filesystem tools.

The diagnosis and gatekeeper communicate on an internal Docker network. Only
the gatekeeper gets an additional outbound network in live mode. The mock judge
uses Docker's native `network_mode: none`; this works on Docker Desktop even
where Harbor's optional network-policy capability check rejects `no-network`.

After diagnosis ends, Harbor collects the **sidecar-owned** `/private/evidence`
directory. A separate verifier image receives it at the same absolute path.
The judge checks trial, case, configuration, event order, turn consumption, and
original-versus-accepted requests before making a judging call. Hashes detect
inconsistency; they are not authentication for arbitrary agent-authored files.
Trust comes from the sidecar source and Harbor's collection boundary.

The analysis accepts results only from verifier directories and API ledgers only
from the designated sidecar artifact or verifier locations. Agent artifacts with
similar filenames are ignored. Conflicting copies of a call ID are rejected.

Runtime source inventories, dependency pins, Dockerfile hashes, and the pinned
Python image are recorded in each task's `isolation.json`. Generated tasks, images,
and logs contain private case data and temporary credentials. Keep them local;
do not upload them to GitHub, a public registry, or Harbor Hub. Default paths are
under ignored `results/`.

## Live evaluation and interpretation

Prepare with `--api-mode live` only when the model choice, case access, and spending
plan are settled. The paired direct-OpenAI worker requires an exported `OPENAI_API_KEY`; do not put
keys in manifests, prompts, images, or chat. Only gatekeeper and judge receive the
provider credential. Both arms retain the same diagnostic and judge models,
prompts, API route, limits, and model arguments. Only the gatekeeper model differs.

The default shared `max_completion_tokens` is 4096. It is configurable and stored
in the manifest. Validate model availability and common supported settings before
the full run. Mock smoke tests do not verify paid API availability or clinical
correctness. A successful live smoke run verifies that particular configured
deployment and transport; it does not establish clinical correctness in general.

The optional `--rate-cards PATH` accepts a JSON object keyed by exact model name.
Each entry has `effective_date` (YYYY-MM-DD), `input_per_million`,
`output_per_million`, optional `cached_input_per_million`, and optional `source`.
No prices are guessed. Reported cached-input and reasoning tokens are subsets,
not extra tokens added again. Failed calls without usage retain unknown cost.
Token-rate estimates are not invoice-confirmed charges; `actual_paid_usd` remains
null until a separate provider-billing reconciliation is implemented.

Results retain failed and missing scheduled episodes in the primary denominator,
also show completed-only accuracy, and calculate paired differences at the case
level. NEJM and MIMIC are reported separately. Missing audit labels remain unknown;
the software does not invent a semantic leakage score.

## Remaining scope

- Blinded clinical disclosure/observation-quality review and fixed-request replay.
- Provider billing reconciliation and a reviewed live spending limit.
- Clinically sourced held-out cases with the required dataset access.
- Independent VM/microVM boundaries if untrusted executable agents require them.
- MAI-DxO or another diagnostic strategy only when its actual implementation is
  available or an explicitly labeled reproduction is requested.

This adapter is a foundation for those experiments, not evidence that one
gatekeeper is clinically better or that all information leakage has been removed.
