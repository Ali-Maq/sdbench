# Run the released SDBench harness

This guide covers the original in-process runner. For isolated execution, use
the [Harbor guide](docs/HARBOR_EXPERIMENT.md). Model calls incur provider charges.

```bash
uv sync
```

Provide credentials through your environment or a local `.env` file; the CLI loads `.env` automatically. Credentials belong in the local environment, never in prompts or committed files.

```bash
# Inspect available cases and splits.
uv run python -m sdbench.cli.case_data synthetic --list-only

# Run one fictional case with the baseline.
uv run python -m sdbench.cli.agent synthetic 0 --agent baseline --model <model>

# Run the synthetic training set.
uv run python -m sdbench.cli.harness \
  --dataset synthetic --case-set train --agent baseline --model <model>

# Resume a saved harness run.
uv run python -m sdbench.cli.harness --resume <results-directory>

# Full flag reference.
uv run python -m sdbench.cli.harness --help
```

Use `--case-id <id> [<id> ...]` or `--num-cases N` to select a subset, and `--max-workers N` to control parallelism.

Resume accepts string case IDs. It skips a saved attempt only when the filename,
stored case ID and repetition agree, and the record has a nonempty diagnosis and
judge output with consistent score/correctness fields. New records also declare
`result_status`. Older records without the required evidence are rerun; words such
as “error” in a diagnosis do not indicate failure. A pricing failure alone does
not invalidate a completed diagnosis. Use `recost` to retry its missing prices.

### Model providers & environment variables

The stock harness routes calls through [`models.py`](sdbench/models.py). The Harbor adapter has a narrower, metered provider path; stock routing settings do not automatically apply to Harbor.

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | Credentials for the default OpenRouter route |
| `SDBENCH_FORCE_AZURE=1` | Route stock-harness calls through Azure OpenAI |
| `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION` | Azure credentials and API settings |
| `SDBENCH_OPENROUTER_ZDR_ONLY=0` | Permit non-zero-data-retention providers when the dataset permits |
| `SDBENCH_LOCAL_BASE_URL`, `SDBENCH_LOCAL_API_KEY`, `SDBENCH_LOCAL_HEADERS` | Endpoint, optional key, and JSON headers for models named `local-...` |
| `SDBENCH_DATA_PATH` | Data root; defaults to `./data` |
| `SDBENCH_LOCAL_DIR` | Local prompt/agent/config override root; defaults to the current directory. Set to `disable` to use packaged definitions only |
| `OPENAI_API_KEY` | Stock CPT-costing embedding fallback when OpenRouter is unset; also the separate Harbor adapter's direct OpenAI credential |

For local inference, the full model name, including `local-`, is sent to the server. The default local API key is the placeholder `sk-local`; `SDBENCH_LOCAL_HEADERS` accepts JSON headers and overrides that authorization header when supplied.

```bash
export SDBENCH_LOCAL_BASE_URL=http://localhost:8000/v1
uv run python -m sdbench.cli.harness --model local-my-model
```

For Azure, `--model` normally identifies the deployment name; a few legacy aliases are mapped in `models.py`:

```bash
export SDBENCH_FORCE_AZURE=1
uv run python -m sdbench.cli.harness --model <azure-deployment>
```

`--model-args` accepts a JSON object merged into diagnostic-model calls:

```bash
uv run python -m sdbench.cli.harness --model <model> \
  --model-args '{"reasoning_effort": "high"}'
```

Routing overrides `force_azure`, `openrouter_zdr_only`, `local_model_base_url`, `local_model_api_key`, and `local_model_headers` are intercepted rather than sent as generation arguments. When importing SDBench as a library, load environment variables yourself; only the CLI loads `.env` automatically. Library callers should also choose an explicit local-override directory or set `SDBENCH_LOCAL_DIR=disable`.

### Published baseline configuration

Named configurations in [`config.py`](sdbench/config.py) pin the gatekeeper and judge:

| Configuration | Use |
| --- | --- |
| `default` | Current defaults; NEJM-style gatekeeper for synthetic and other non-MIMIC datasets |
| `science` | Published baseline settings for `nejm` and `mimic`; not an MAI-DxO implementation |

To evaluate a baseline with the published configuration, obtain the corresponding dataset and use its `validationandtest` split:

```bash
uv run python -m sdbench.cli.harness \
  --dataset <nejm-or-mimic> --case-set validationandtest \
  --agent baseline --sdbench-config science --model <model>
```

Matching a configuration alone does not reproduce a result: the dataset/split, model version, provider settings, and costing method also matter.

### Stock-harness clinical costing

The stock harness defaults to LLM-estimated test costs using `gpt-5-mini` (overridable with `--costing-model`). Use `--no-compute-costs` to skip costing. For the paper's grounded method, configure the public hospital charge master as described in [costing data setup](data/costing_data/README.md), then run:

```bash
uv run --extra cpt_costing python -m sdbench.cli.recost <results-directory>
```

This writes costs back to the case results and reports CPT-grounded matches versus LLM fallbacks. Review that coverage before comparing costs. LLM-only estimates and grounded estimates are different measurements.

Each estimate declares `complete`, `partial` or `unknown` pricing, with priced and
unpriced test counts. Complete totals are `null` when coverage is incomplete;
known subtotals remain separate. Reports recheck legacy results from per-test
details and the declared expected count. A cohort with missing prices has no
complete mean/total; complete-case subset statistics carry their own denominator.
This measures one price per unique test name after lowercasing and trimming outer
whitespace. Repeated requests are counted but are not repeated-procedure bills.

The optional cache now includes effective model arguments/routing, pricing code,
and, for grounded costing, charge-master content and embedding settings. Old keys
miss automatically. Changed sources also invalidate in-memory matchers and disk
indexes. Old index files are left in place. Keep sources and settings fixed during
an in-flight request and pin deployments where possible: a remote model change
behind an unchanged alias cannot be detected by a local fingerprint. Restart the
process after code changes. Transient matcher/search failures are not cached as
successful prices, and damaged local embedding/index files are rebuilt.

```bash
# Recompute already priced cases.
uv run --extra cpt_costing python -m sdbench.cli.recost <results-directory> --force

# Inspect existing costing coverage without recomputing.
uv run --extra cpt_costing python -m sdbench.cli.recost <results-directory> --analyze-only

# Use LLM-only costing.
uv run python -m sdbench.cli.recost <results-directory> --costing-method llm_only
```

Grounded costing can also run inline with `--costing-method unified_cpt_llm`. The optional `azure` extra supports Blob output and a CosmosDB costing cache. Install extras with `uv sync --extra <name>` or `uv sync --all-extras`; consult CLI `--help` for cache and managed-identity settings.

### Stock-harness reports

Each run saves case JSON, HTML, a summary, and `run_config.json` under `results/`. Regenerate reports from saved outputs without rerunning diagnosis:

```bash
uv run python -m sdbench.cli.reporting <results-directory> --ci --json report.json
```

The reporter also accepts multiple result directories from the same dataset. It groups by `(agent, model)` and reports accuracy and each available costing method. The stock reporter excludes errored/incomplete cases from accuracy and flags them. The paired Harbor analysis instead counts missing/failed scheduled attempts as incorrect in its primary accuracy, while also reporting completed-only accuracy. State the denominator when comparing results.


## Before a real evaluation

Confirm the dataset/split, model/provider, named configuration and any overrides.
Use authorized data and keep records, credentials and output inside the permitted
research environment. Capture the effective settings and use `--resume` for
unfinished runs rather than silently changing the protocol.

For a cost comparison, configure the documented charge-master source and check
coverage before evaluation. The adapter does not yet run clinical costing.
Report scheduled, completed and failed cases, pricing coverage and unknown API
charges separately. A fictional smoke test is an execution check only.
