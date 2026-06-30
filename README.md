# SDBench: Sequential Diagnosis Benchmark

SDBench is a benchmark for evaluating sequential diagnosis systems in medical settings. This benchmark provides a framework for assessing how AI systems can progressively gather information, formulate hypotheses, and arrive at diagnoses through iterative decision-making processes.

This benchmark is described in [Sequential Diagnosis with Language Models](https://www.science.org/doi/10.1126/science.aeh7930), published in _Science_.

A website for the benchmark is hosted at https://microsoft.github.io/sdbench

## How it works

For each case, the harness ([`harness/harness.py`](sdbench/harness/harness.py)) runs a sequential loop between three roles:

1. The **agent** (the system under test) receives a short `case_summary` that does not reveal the diagnosis, and decides — turn by turn — to order tests, ask questions, or commit a diagnosis.
2. The **gatekeeper** ([`gatekeeper.py`](sdbench/gatekeeper.py)) answers each test order / question from the case's full text, returning the real result when the case contains it or a *synthetic* result when it does not.
3. When the agent commits a diagnosis, the **judge** ([`judge.py`](sdbench/judge.py)) scores it against the ground-truth diagnosis on a 1–5 Likert scale (4–5 counts as a correct diagnosis).

The gatekeeper and judge models/prompts are fixed by the chosen [configuration](#configurations); the agent's model is what you are benchmarking. The [costing](sdbench/costing/) module estimates the monetary cost of the tests an agent orders.

## Setup

### Install

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management.

```bash
uv sync
```

Then create a `.env` file holding the API keys for whichever model provider(s) you use (see below).
The CLIs load it automatically via `python-dotenv`.

Optional features live behind dependency extras (install one with `uv sync --extra <name>`, or
everything with `uv sync --all-extras`):

- `cpt_costing` — CPT-code grounded costing (`--costing-method unified_cpt_llm`).
- `azure` — Azure integrations: Blob Storage output upload and the CosmosDB costing cache.

### Model providers & environment variables

Models are routed by [`models.py`](sdbench/models.py). By default requests go through **OpenRouter**; you can
force Azure OpenAI or a local inference endpoint instead.

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter (default routing). |
| `OPENAI_API_KEY` | Only used by the `cpt_costing` extra: a fallback for building test-name embeddings when `OPENROUTER_API_KEY` is unset. |
| `SDBENCH_FORCE_AZURE=1` | Route all calls through Azure OpenAI. |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_VERSION` | Azure OpenAI credentials. |
| `SDBENCH_OPENROUTER_ZDR_ONLY=0` | Allow non–zero-data-retention OpenRouter providers if your dataset permits. |
| `SDBENCH_LOCAL_BASE_URL` / `SDBENCH_LOCAL_HEADERS` | Base URL / headers for a local (e.g. SSH-tunneled) inference server; models named `local-...` route here. |
| `SDBENCH_DATA_PATH` | Override the data directory (default: `./data`). Covers both case datasets (`<path>/processed_case_data/`) and the costing charge-master (`<path>/costing_data/`). |
| `SDBENCH_LOCAL_DIR` | Base directory for local overrides — `prompts/`, `diagnostic_agents/`, and `config.py` (default: the current working directory). Set to `disable` to ignore all local overrides and use only the packaged built-ins. |

### Use as a library

When you import `sdbench` from another project (rather than running the CLIs), two defaults are tuned for standalone use and are worth overriding:

- **`.env` is not loaded automatically.** Only the CLI entry points call `python-dotenv`; the library reads its configuration straight from the environment. Load your own `.env` (or otherwise export `OPENROUTER_API_KEY`, `SDBENCH_*`, etc.) before calling into `sdbench`.
- **Local overrides default to the current working directory.** A `prompts/`, `diagnostic_agents/`, or `config.py` in your host project's CWD will be picked up. Set `SDBENCH_LOCAL_DIR=disable` to use only the packaged built-ins, or point it at a dedicated directory.

### Datasets

A dataset lives under `./data/processed_case_data/{dataset_name}/`. 

#### Existing Datasets

SDBench ships with a small, fully-synthetic dataset named `synthetic` (five NEJM-style CPC
cases), which is the default dataset so the harness runs out of the box with no data access
required. See [data/synthetic_case_data/README.md](data/synthetic_case_data/README.md) for details.

> **Note:** The `synthetic` dataset is intended only as a set for developing and testing agents and as such only provides a `train` split; it should not be used to report diagnostic performance — use a clinically sourced dataset for that.

The clinically sourced datasets are not included in the repository and require separate access or licensing:

See [data/processed_case_data/nejm/README.md](data/processed_case_data/nejm/README.md) for details about licensing the `nejm` dataset from NEJM.

The `mimic` dataset is distributed as [MIMIC-IV-Ext-SDBench v1.0.0 on PhysioNet](https://physionet.org/content/mimic-iv-ext-sdbench/1.0.0/). See [data/processed_case_data/mimic/README.md](data/processed_case_data/mimic/README.md) for access requirements and download instructions.

#### Bring your own dataset

Populate a dataset folder under `data/processed_case_data/<your_dataset>/` with cases following the case data formats detailed in the module docstring of [`case_data.py`](sdbench/case_data.py).

Note: There are two different gatekeepers in SDBench, each optimised for different NEJM and MIMIC case report formats. By default SDBench will use the NEJM optimised gatekeeper, but if your dataset is sourced from discharge summaries that are more similar to MIMIC in nature then the MIMIC gatekeeper is recommended. Regardless it is recommended to evaluate the performance of the gatekeeper on any new dataset.

## Configurations

Named configs in [`config.py`](sdbench/config.py) pin the gatekeeper/judge models and prompt versions. Select
one with `--sdbench-config <name>`:

| Config | Description |
|---|---|
| `science` | The configuration used in the peer-reviewed Science paper (`nejm` and `mimic`); pair with `--agent baseline`. |
| `default` | The current default config; uses the NEJM-optimised gatekeeper for the `synthetic` default dataset and any non-`mimic` dataset. |

## Usage

### Run with a coding agent

Open this checkout in a coding agent that follows [AGENTS.md](AGENTS.md) and ask:

> `Run SDBench on <model name>`

The agent follows [RUN_SDBENCH.md](RUN_SDBENCH.md) to ask for missing run settings,
help with credentials and dataset access, run the evaluation and grounded costing,
and report progress and results. The workflow also covers recovery and resuming
interrupted work; you do not need to reference the runbook in your request.

### Run a single case

By default it runs the baseline agent on the bundled `synthetic` train set (five cases) with GPT-5 using OpenRouter:

```bash
export OPENROUTER_API_KEY=...
uv run python -m sdbench.cli.harness
```

Some basic config options for the harness:

- `--dataset` the dataset to run on
- `--case-set` selects the split to run (e.g. `validation`, `test`, `all` — available sets depend on the dataset).
- `--max-workers N` number of cases to run in parallel
- `--num-cases N` or `--case-id <id> [<id> ...]` restrict to a subset.
- Runs are **resumable**: `--resume <results_dir>` continues an interrupted run from its saved config, skipping completed cases.

### Evaluate a new model

Point `--model` at any model your configured provider exposes (an OpenRouter slug, an Azure
deployment name, or a `local-...` model)

```bash
uv run python -m sdbench.cli.harness --model <your-model>
```

#### Local models

Any model whose name starts with `local-` is routed to a local OpenAI-compatible inference
server (e.g. vLLM, llama.cpp, Ollama) instead of OpenRouter or Azure. The full model string —
including the `local-` prefix — is forwarded verbatim as the request's `model`, so serve your
model under that exact name. OpenRouter ZDR/provider routing does not apply.

Point SDBench at your endpoint with `SDBENCH_LOCAL_BASE_URL`. If your server checks an API key,
set `SDBENCH_LOCAL_API_KEY` (it is sent as a standard `Authorization: Bearer` header and
defaults to the dummy value `sk-local`, which satisfies servers that require a key but don't
validate it):

```bash
export SDBENCH_LOCAL_BASE_URL=http://localhost:8000/v1
uv run python -m sdbench.cli.harness --model local-my-model
```

For non-standard auth (a custom header name) or extra headers needed by an SSH tunnel or proxy,
set `SDBENCH_LOCAL_HEADERS` to a JSON object. These headers are merged last, so an
`Authorization` key here overrides `SDBENCH_LOCAL_API_KEY`:

```bash
export SDBENCH_LOCAL_HEADERS='{"Authorization": "Bearer <token>"}'
```

These can also be supplied per run as model args (see [Model arguments](#model-arguments)):
`local_model_base_url`, `local_model_api_key`, `local_model_headers`.

#### Azure OpenAI

To serve models from your own Azure AI Foundry deployment (which must be ZDR-configured), set
the Azure credentials (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_VERSION`) and force Azure routing. The `--model` value is used directly as
the Azure **deployment name**, so name your deployment to match (no pre-registration in SDBench
needed — new models just pass through). The only exceptions are a few aliases remapped in
`AZURE_MODEL_MAP` in [`models.py`](sdbench/models.py) (e.g. `gpt-3.5-turbo` → `gpt-35-turbo`).

Force Azure for the whole run with the env var:

```bash
export SDBENCH_FORCE_AZURE=1
uv run python -m sdbench.cli.harness --model gpt-5
```

Or force it per call (so you can mix Azure and OpenRouter models in a single run) with the
`force_azure` model arg:

```bash
uv run python -m sdbench.cli.harness --model gpt-5 --model-args '{"force_azure": true}'
```

#### Model arguments

`--model-args` takes a JSON object that is merged into each agent API call, overriding the
per-model defaults in `MODEL_ARG_DEFAULTS`. Most keys are forwarded straight to the model API:

```bash
uv run python -m sdbench.cli.harness --model gpt-5 --model-args '{"reasoning_effort": "high"}'
```

A few keys are instead intercepted by SDBench's routing layer and are **not** forwarded to the
model: `force_azure` and `openrouter_zdr_only` (per-run equivalents of `SDBENCH_FORCE_AZURE` /
`SDBENCH_OPENROUTER_ZDR_ONLY`), and the local-endpoint overrides `local_model_base_url`,
`local_model_api_key`, and `local_model_headers`.

### Reproduce the Science paper baselines

Use the `science` config, the `baseline` agent, the `validationandtest` case set (which encompasses the `validation` and `test` splits that were reported jointly against a given model, ie: `openai/gpt-5.5`:

```bash
uv run python -m sdbench.cli.harness \
  --dataset <nejm|mimic> \
  --case-set validationandtest \
  --agent baseline \
  --sdbench-config science \
  --model "openai/gpt-5.5"
```

The `science` config pins the gatekeeper and judge per dataset so results are comparable across agent
models. Costs will need to be computed with grounded costing data for comparability to the published results (see next section).

### Costing

SDBench supports LLM based estimates of diagnostic test costs (the default) as well as estimates grounded on a public hospital charge master. Cost computation can be disabled with `--no-compute-costs`.

#### Grounded Costs

In the published paper we reported on costs that were grounded against a public hospital charge master. If you want to use grounded costing data then you'll need to configure the costing data: See [data/costing_data/README.md](data/costing_data/README.md).

To compute costs using this grounded costing data you can either compute it as the harness runs each case or as a batch operation once the harness run has finished (recommended).

To run it as a batch operation, point the recost CLI at one or more results directories.
Grounded costing needs the `cpt_costing` extra, so enable it for the run:

```bash
uv run --extra cpt_costing python -m sdbench.cli.recost <results_dir> [<results_dir> ...]
```

This recomputes each agent's per-test costs with the grounded method and writes them back
into the case JSON. It then reports how many ordered tests received a *grounded* cost (a CPT
match against the charge master) versus how many fell back to an LLM estimate:

```
  Total tests costed:      200
  Grounded (CPT):          196  ( 98.0%)
  LLM fallback:              4  (  2.0%)
```

If GPT grounding fail to match a specific test the costing will fall back to an LLM-estimate. A typical grounding rate of 98%+ is expected, if the rate is significantly below that then your grounded cost results may be unreliable.

Already-costed cases are skipped unless they contain pricing failures; pass `--force` to
recompute everything, or `--analyze-only` to just print the breakdown for costs that were
already computed (by the harness or a previous recost run) without recomputing.

Pass `--costing-method llm_only` (or `batch_llm_only`) to re-cost with the LLM method
instead of grounded CPT; the LLM methods don't need the `cpt_costing` extra or the charge
master, and the breakdown reports priced vs failed rather than grounded vs fallback. The
breakdown always reflects only the method being run, ignoring any other cost analyses
already stored on the case.

To compute grounded costs inline as the harness runs instead, set `--costing-method unified_cpt_llm`.

#### LLM Estimated Costs

By default SDBench will compute LLM Estimated costs using `gpt-5-mini`. This model can be changed with `--costing-model`, though cost estimates from different models aren't directly comparable.

LLM cost estimates are also not directly comparable with grounded cost estimates. We found the `gpt-5-mini` estimates to be 40-50% higher than the grounded estimates used in our paper. While individual tests can show more variance, in aggregate this appeared to be a consistent bias.

#### Caching costs

Costing can be slow and expensive: both the grounded (CPT) and LLM methods make model
calls per test, and the same tests (e.g. CBC, chest X-ray) recur across many cases and
across repeated runs. A cost is keyed only by `(costing_method, costing_model, test_name)`,
so a shared cache lets one priced test be reused across every case that orders it, across
re-costing runs, and across machines — turning most of a re-cost into cache hits.

SDBench supports an optional CosmosDB-backed cache for this. It needs the `azure` extra and
is enabled with these flags (all forwarded to the costing config, so they work on both the
harness and the recost CLI):

```bash
uv run --extra azure --extra cpt_costing python -m sdbench.cli.recost <results_dir> \
  --costing-cache-cosmosdb-enabled \
  --costing-cache-cosmosdb-endpoint https://<account>.documents.azure.com:443/ \
  --costing-cache-cosmosdb-database <database> \
  --costing-cache-cosmosdb-container <container>
```

Authentication uses `DefaultAzureCredential` (Azure CLI login, environment, or managed
identity). On Azure compute you can add `--costing-cache-use-managed-identity` to use the
attached managed identity instead. The cache is off by default; when disabled, costing
still runs but recomputes every test.

### Outputs

A run writes to `results/<owner>_<dataset>_<label>_<model>_<case_set>_<timestamp>_k<k>/`:

```
json/case_k{k}_{case_id}.json   # one file per (case, repetition)
html/                           # generated HTML report
summary.txt                     # console summary
run_config.json                 # full config (used to resume / restore)
```

The batch runner generates the summary, HTML, and an accuracy/cost report automatically. To
regenerate reports from one or more existing run directories:

```bash
uv run python -m sdbench.cli.reporting <results_dir> [<results_dir> ...] [--ci] [--json out.json]
```

This regenerates each folder's own `summary.txt` (with that folder's per-folder accuracy/cost
report folded in) and HTML, then prints a distinct **aggregate** accuracy + cost report grouped by
`(agent, model)` — pooling multiple folders that share the same pair — broken down by case set
(`train`/`validation`/`test` by default; override with `--case-sets`). Costs are reported as
mean-per-case and run-total for every costing method present. `--ci` adds bootstrapped CIs
(percentile bootstrap for cost, binomial for accuracy). Errored and incomplete cases are flagged and
excluded from accuracy; mismatched repetition counts are flagged. The aggregate aborts if the
folders span multiple datasets.

### Write your own agent

An agent is a module exposing `run(case_summary, test_runner, agent_llm, collector, config)` (see the
`DiagnosticAgent` protocol in [`agents/__init__.py`](sdbench/agents/__init__.py)). Agents are resolved by
module name from:

1. The packaged `baseline` agent in [`agents/`](sdbench/agents/).
2. A `diagnostic_agents/` directory under the local-override base — the current working
   directory by default, or `$SDBENCH_LOCAL_DIR` if set (use `SDBENCH_LOCAL_DIR=disable` to
   skip it). Drop a `my_agent.py` there and select it with `--agent my_agent`.

The packaged baseline drives the gatekeeper with XML-tagged requests (`<test>...</test>`,
`<question>...</question>`, `<diagnosis>...</diagnosis>`); see
[`agents/baseline.py`](sdbench/agents/baseline.py) for a complete example.

### Full CLI reference

The flag set is large; for the complete, authoritative list run:

```bash
uv run python -m sdbench.cli.harness --help
```

## Citation

If you use SDBench, please cite the Science article:

```bibtex
@article{nori2026sequentialdiagnosislanguagemodels,
  title={Sequential Diagnosis with Language Models},
  author={Harsha Nori and Mayank Daswani and Christopher Kelly and Scott Lundberg and
          Marco Tulio Ribeiro and Marc Wilson and Xiaoxuan Liu and Viknesh Sounderajah and
          Jonathan Carlson and Matthew P Lungren and Bay Gross and Peter Hames and
          Mustafa Suleyman and Dominic King and Eric Horvitz},
  journal={Science},
  year={2026},
  doi={10.1126/science.aeh7930},
  url={https://www.science.org/doi/10.1126/science.aeh7930},
}
```
