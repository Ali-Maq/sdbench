# Paired gatekeeper experiments in Harbor

This adapter tests whether changing the gatekeeper from `o4-mini` to `gpt-5`
changes the measured performance of a fixed diagnostic agent. It uses the
released SDBench baseline, not Microsoft's MAI-DxO orchestration system.

The initial implementation runs with **three role containers on a shared
kernel**, not three independent VMs. All default examples use deterministic
mock responses. Mock scores test execution and accounting, not clinical ability.

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
| Generic token/cost summaries | Harbor exposes `AgentContext` / `model_usage` / ATIF; these fields are not populated by this adapter yet | Protected per-role attempt ledger for gatekeeper and judge as well as diagnosis |
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
plan are settled. The worker requires an exported `OPENAI_API_KEY`; do not put
keys in manifests, prompts, images, or chat. Only gatekeeper and judge receive the
provider credential. Both arms retain the same diagnostic and judge models,
prompts, API route, limits, and model arguments. Only the gatekeeper model differs.

The default shared `max_completion_tokens` is 4096. It is configurable and stored
in the manifest. Validate model availability and common supported settings before
the full run. The smoke tests do not verify paid API availability or clinical
correctness.

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
