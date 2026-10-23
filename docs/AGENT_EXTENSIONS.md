# Optional panel and tracing

The primary [longitudinal study](STUDY_PROTOCOL.md) uses the released SDBench
`baseline`. These optional features are separate: the panel changes diagnostic
policy; Langfuse exports metadata after execution.

## Experimental panel

[`experimental_panel.py`](../sdbench/agents/experimental_panel.py) is a newly
written, paper-inspired policy. It is **not original MAI-DxO**, whose executable
orchestrator and complete prompts were not found in the inspected official
release. See the [source audit](mai_dxo_source_audit.json).

The module implements the existing SDBench interface:

```python
run(case_summary, test_runner, agent_llm, collector, config) -> str
```

It uses LangGraph for the loop, SDBench's injected model callable for inference,
and the released XML action parsers and TestRunner. It needs no separate
LangChain model wrapper or five additional servers.

```text
brief presentation
  -> panel: one model call discussing five perspectives
  -> validate action
       questions / tests -> TestRunner -> observations -> panel
       malformed action  -> repair request -> panel
       final diagnosis   -> return answer
  -> if limit reached: one reserved final-answer call
```

The perspectives are Hypothesis, Test-Chooser, Challenger, Stewardship and
Checklist. All five are requested within **one response per panel step**. That
call boundary and the prompt are our implementation choices, not recovered
Microsoft internals. The graph has panel, dispatch and final-answer nodes.

The panel validates one action type per response and at most three tests. A
panel-call cap bounds malformed-response loops; the isolated client reserves
one of its allowed provider calls for a final answer. There is no calibrated
Bayesian state, numeric clinical budget, ensemble or cost-based test cancellation.
It therefore does not reproduce the paper's accuracy-cost frontier.

The collector saves the prompt hash and implementation label, visible messages,
and graph events. Role names are not emitted as separate provider calls when
no separate call occurred. The gatekeeper and judge remain outside this graph.

### Run a fictional mock example

Requires Docker. The output path must be new:

```bash
uv sync --python 3.12 --extra harbor --extra panel

uv run --extra harbor --extra panel python -m sdbench.cli.isolation prepare \
  --dataset synthetic --case-set all --case-id 0 \
  --diagnostic-agent experimental_panel \
  --max-turns 3 --max-model-calls 8 --api-mode mock \
  --output results/panel-demo

uv run --extra harbor --extra panel harbor run \
  --config results/panel-demo/job.json

uv run --extra harbor --extra panel python -m sdbench.cli.isolation summarize \
  --manifest results/panel-demo/manifest.json \
  --jobs results/panel-demo/jobs --output results/panel-demo/summary.json
```

This creates two mock conditions for one fictional case. The fixed mock score
3/5 is expected, not a clinical result. The separate live fictional panel run
scored 2/5; see [verification](VERIFICATION.md).

The policy choice is frozen in the manifest and protocol hash. The service
selects the packaged agent; the client cannot load an arbitrary module. Harbor
0.24 also has a [native LangGraph adapter](https://github.com/harbor-framework/harbor/blob/v0.24.0/src/harbor/agents/installed/langgraph.py).
This project retains its existing adapter because it supplies the clinical
case-access boundary, metering and ATIF export. Native Codex/Claude Code clinical
tools are not wired into these tasks.

## Langfuse metadata export

[`tracing.py`](../sdbench/harbor_integration/tracing.py) reads the trusted
sidecar usage ledger and verifier judge-call ledger. It exports role, attempt,
status, reported tokens, API latency and hashed technical/model references.
It excludes patient records, messages, prompts, test names and clinical scores.
The clinical conversations remain in the local SDBench report and diagnostic
ATIF trajectory.

This is a **post-run metadata export**, not a full execution waterfall, evaluator
ensemble or evaluation dataset. Span timestamps describe export time; recorded
API latency is metadata. It makes no model calls and cannot affect diagnosis.
Hashing technical identifiers is not a certification of de-identification.

```bash
# Preview locally; no Langfuse SDK or network access required.
uv run python -m sdbench.harbor_integration.tracing \
  --trial-dir <completed-harbor-trial-directory> \
  --output results/trace-preview.json
```

To send, set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` in the host environment, install the `tracing` extra,
and use a new output path:

```bash
uv run --extra tracing python -m sdbench.harbor_integration.tracing \
  --trial-dir <completed-harbor-trial-directory> \
  --output results/trace-sent.json --send
```

SDK flushing does not establish server acceptance; check the configured server.
Sending the same trial again appends observations, so send the reviewed export
once. Keep credentials outside the diagnostic container. Full clinical trace
export is not implemented and would need the source data's access restrictions.

The current environment uses Langfuse SDK 4.18.0 with local server 3.159.0; see
[SDK compatibility](https://langfuse.com/docs/observability/sdk/overview) before
upgrading. A standard LangChain/LangGraph callback would not automatically capture
our raw SDBench model callable or every retry. The trusted provider ledger remains
the accounting source. A trace UI's catalog estimate is not an Azure invoice.

## Verification

Core modules import optional dependencies lazily. Harbor 0.24 requires Python
3.12; core SDBench and role images support Python 3.11. `uv.lock` records exact
host dependencies; generated panel containers use pinned requirements.

```bash
uv sync --python 3.12 --extra harbor --extra panel --extra tracing
uv run python -m unittest discover -s tests -v
```

The panel tests execute the compiled graph with scripted responses, covering
requests, observations, turn exhaustion, malformed actions and invalid answers.
Tracing tests exercise the real SDK against an in-memory exporter. Completed
Docker and live smoke checks are recorded separately in [VERIFICATION.md](VERIFICATION.md).
