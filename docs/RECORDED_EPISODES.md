# Recorded-only episodes

This path tests the released SDBench baseline against a prepared episode from
existing records. It changes how observations are supplied. It does not implement
MAI-DxO, reconstruct missing investigations or claim the patient's entire chart
was available at the opening encounter.

```text
Private preparation: short opening + permitted extracts + expert reference
    |
    +-- Diagnosis container: released baseline sees opening only
    |       questions / tests -> sidecar -> recorded observations
    |       final diagnosis ---------------------------+
    |                                                  |
    +-- Gatekeeper sidecar: permitted evidence          |
    |       model selects IDs from permitted excerpts |
    |       code renders exact stored excerpts         |
    |       missing selection -> "not documented"      |
    |       protected events + per-call ledger --------+
    |                                                  |
    +-- Separate verifier: private expert reference <--+
            validate trace, IDs, excerpts and provenance
            released v3_reasoning judge -> score
            host-only report -> dialogue + evidence + reference
```

The v2 selector receives request text and catalog IDs, kinds, labels, aliases,
permitted evidence text and relative dates. It does not receive the expert
reference or source identifiers. Reading the excerpt lets it distinguish an
explicit answer from an unrelated finding under a broad label such as ROS.
It is instructed to select all available components of a requested panel,
including normal and zero results, and keep single-analyte requests narrow.
For compound requests it should select available evidence even if another component is missing; the excerpts do
not establish that every part was answered. It returns one ID list per request.
Unknown IDs, duplicates, wrong question/test kinds and
malformed responses fail the encounter. A valid empty selection returns exactly
`not documented`; this is not a negative clinical finding. An empty selection can
also be a retrieval error even when relevant evidence exists in the catalog;
selection completeness needs audit. Code renders selected
excerpts with their event and source-record days. The verifier reconstructs each
result before making a paid judge call.

Version 1 saw only labels and aliases. Its frozen task directories and completed
artifacts remain valid historical records; do not resume a v1 task with v2 source
or overwrite its results. Compare separate runs with identical case hashes,
diagnosis/judge code and settings. Fixed-request selector replay isolates retrieval
changes; an end-to-end rerun can ask different questions and is not by itself a
controlled accuracy comparison. Scripted tests verify boundaries, while the
authored fictional retrieval fixture provides expected ID sets for live checks.

The gatekeeper container holds permitted extracts but no `full_text`,
`final_diagnosis` or reference metadata. Diagnosis sees neither catalog nor source
IDs. Requested pathology or radiology may legitimately contain a diagnostic
interpretation: withholding the expert answer does not mean deleting informative
test results. Containers share a host kernel. These checks establish membership
in the frozen catalog, not zero semantic leakage or clinically relevant selection.

## Private case contract

Use the [fictional fixture](../tests/fixtures/recorded_episode.json) as the format
example. The existing case fields remain: `case_id`, `dataset`, `case_summary`,
`full_text`, `final_diagnosis`. `case_summary` is the short opening; `full_text`
contains the expert reference context for the judge, not an unfiltered chart.

`recorded_episode` contains:

| Field | Meaning |
| --- | --- |
| `schema_version`, `mode` | `1`, `retrospective_request` |
| `opening_source_ids` | Records supporting the presentation |
| `reference_source_ids`, `reference_day` | Attribution and ascertainment point for judge-only reference context |
| `evidence_cutoff_day` | Latest permitted diagnostic event/study date |
| `evidence` | Frozen entries: `id`, `kind`, `label`, `aliases`, `text`, `source_id`, `source_sha256`, `event_day`, `recorded_day` |

Kinds are `question` or `test`, matching SDBench's request convention. Each entry
needs a unique ID and a source digest. For narrative findings, copy the permitted
excerpt; for structured rows, render checked labels, values and units during
preparation. Preserve a private extraction receipt. A digest alone does not prove
the text was faithfully extracted, or remove identifiers from it.

A source ID can support both a permitted test excerpt and a private reference
excerpt. For example, marrow morphology can be requested even when that pathology
report also supports the expert's diagnosis. Separation applies to the selected
text and role payloads, not a blanket prohibition on sharing a source document.
The preparer must keep the expert's privileged assessment out of permitted text.

Days are relative to the selected starting encounter. `event_day` means the
documented study/collection date, or a known report signature date where chosen;
it does **not** establish when a result became available. `recorded_day` may be
later, such as a pathology report embedded in a subsequent note. Specify those
choices in the preparation receipt. This mode retrieves later diagnostic evidence
when requested; it has no advancing clinical clock or true as-of replay.

Exclude treatment response, later outcomes, privileged expert assessments and
direct identifiers from permitted evidence. Freeze the reference and explain
conflicting subtype/staging labels before scoring. The released semantic judge
returns 1–5, with 4–5 passing; it is not a keyword check or a new staging rubric.

## Test the fictional workflow

From the checkout, with Docker and the Harbor extra installed:

```bash
uv run --extra harbor python -m sdbench.cli.isolation prepare-recorded \
  --case tests/fixtures/recorded_episode.json \
  --diagnosis-model mock-diagnosis --gatekeeper-model mock-selector \
  --judge-model mock-judge --max-turns 5 --max-model-calls 8 \
  --api-mode mock --jobs results/recorded-smoke/jobs \
  --output results/recorded-smoke

uv run --extra harbor harbor run --config results/recorded-smoke/job.json

uv run --extra harbor harbor view results/recorded-smoke/jobs \
  --host 127.0.0.1 --port 8080-8089 --jobs
```

The fixture is entirely fictional. The mock diagnosis requests CBC and an absent
culture, then submits an explicitly mock answer. The mock selector matches exact
catalog labels/aliases; it does not simulate clinical reasoning. The judge returns
3/5 and reward 0. This checks execution, missingness, accounting and isolation.
It does not measure model accuracy. Use a fresh output directory on repetition.

Export the existing full-role report after the trial completes:

```bash
uv run python -m sdbench.harbor_integration.reporting \
  --trial-dir <completed-trial-directory> \
  --case results/recorded-smoke/tasks/episode-0001/tests/private/case.json
```

Open its `artifacts/clinical-report/overview.html`; the linked evidence view shows
what was permitted and what was actually returned. Native Harbor records the
diagnostic text/action trajectory, not a screen video or hidden reasoning trace.

## Preparing a real episode

`prepare-recorded` makes no API calls. It validates the case and freezes hashes,
role payloads, prompts, limits and a native Harbor job. It defaults to mock mode.
These hashes record the prepared snapshot; they are not runtime attestation
against someone who can edit the host's task directories or verifier code.
Keep the case, output directory, Docker images and reports in the permitted
research environment, outside public source control. Do not treat the absence of
direct identifiers as a privacy certification.

For a later authorized live run, pass explicit role models and `--api-mode live`.
Direct OpenAI uses the existing Chat Completions route. Azure uses
`--provider azure --base-url https://<resource>.cognitiveservices.azure.com/openai/v1/`
and the existing `sdbench.cli.azure_harbor` launcher. Match the intended deployment,
returned model versions and processing approval before sending patient text.

This command prepares one episode. It does not add private cohorts to the paired
analyzer, which deliberately rejects single-episode manifests. The provider ledgers
retain all attempts, usage and returned model IDs; clinical costing is not run.
API invoice dollars remain unknown without billing evidence. One-shot controls,
judge ensembles and patient-level cohort comparisons remain separate study work.
