# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

SDBench (Sequential Diagnosis Benchmark) evaluates diagnostic AI agents on clinical cases under a
sequential setting: an **agent** iteratively orders tests / asks questions (answered by a
**gatekeeper** model) before committing a diagnosis, which a **judge** model scores against ground
truth on a 1–5 scale. See `README.md` for the full overview and usage.

## Running SDBench

When a user asks to run SDBench, evaluate a model, resume an evaluation, or add
grounded costing, read and follow [RUN_SDBENCH.md](RUN_SDBENCH.md). A request such
as `Run SDBench on <model>` is sufficient; use that workflow to collect missing
settings and carry the run through setup, evaluation, costing, and reporting.
The user does not need to name the runbook explicitly.

## Layout

- `sdbench/` — the Python package (import path `sdbench`).
  - `cli/` — command-line entry points (`uv run python -m sdbench.cli.<name>`).
  - `harness/harness.py` — core per-case loop (agent ↔ gatekeeper ↔ judge).
  - `agents/` — the packaged baseline diagnostic agent and agent-loading interface.
  - `gatekeeper.py`, `judge.py`, `case_data.py`, `config.py`, `models.py`, `reporting.py`.
  - `costing/` — test-cost estimation.
  - `prompts/` — versioned gatekeeper/judge prompt templates (`.yaml`).
  - `isolation/` — authenticated role services, provider accounting and paired analysis.
  - `harbor_integration/` — task generation, runner adapter, trajectories and optional tracing.
- `data/` — dataset directory (`SDBENCH_DATA_PATH`, default `./data`). Five fictional
  cases and their Markdown sources are committed. Restricted datasets are not redistributed.

## Setup

This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Stock CLI commands load model-provider credentials from `.env`. The Harbor
commands require exported environment variables instead. See `RUN_SDBENCH.md`
for stock providers and `docs/HARBOR_EXPERIMENT.md` for the adapter.

## Common commands

```bash
# Inspect a dataset / case sets
uv run python -m sdbench.cli.case_data <dataset> --list-only

# Single case
uv run python -m sdbench.cli.agent <dataset> <case_id> --agent baseline --model <model>

# Batch run over a case set (parallel, resumable)
uv run python -m sdbench.cli.harness --dataset <dataset> --case-set validation \
  --agent baseline --model <model> --sdbench-config science --max-workers 4

# Full, authoritative flag list
uv run python -m sdbench.cli.harness --help
```

## Conventions

- **Target Python is 3.11+.** Do not use 3.12-only syntax (e.g. backslashes or nested same-quote
  f-strings inside f-string expressions, PEP 695 `type`/generic syntax). Sanity-check with:
  ```bash
  uv run --python 3.11 python -m compileall sdbench
  ```
- Named run configurations (`default`, `science`) live in `sdbench/config.py`; select with
  `--sdbench-config`. Most `SDBenchConfig` fields are overridable from the harness CLI.
- The case data formats (single-file `dataset.json`/`dataset.csv` and the per-case layout) are
  documented in the `sdbench/case_data.py` module docstring.
- Optional features are isolated behind dependency extras and imported lazily, so the package runs
  on a minimal core (`openai`, `python-dotenv`, `pyyaml`, `tqdm`, `numpy`). Extras: `cpt_costing` (CPT-code
  costing), `azure` (Blob Storage upload + CosmosDB costing cache), `harbor` (Python
  3.12 runner), and `tracing` (host-side Langfuse export; full clinical mode is local-only and opt-in). Install one with
  `uv sync --extra <name>`, or all with `uv sync --all-extras`.
