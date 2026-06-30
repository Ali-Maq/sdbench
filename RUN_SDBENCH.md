# Run SDBench

Follow this workflow when a user asks to run SDBench or continue an evaluation.
A request such as `Run SDBench on <model>` is enough to get started. Help the user
through baseline evaluation, grounded costing, and verified results.

Use the [README](README.md), [dataset READMEs](data/processed_case_data/),
[costing data guide](data/costing_data/README.md), CLI help, and relevant code as
the source of truth. Discover supported settings rather than assuming particular
model, dataset, split, or configuration values.

## 1. Establish the run

Reuse anything the user has already specified. Ask for the missing model,
dataset, splits, and named configuration, presenting valid options with short
descriptions and relevant compatibility constraints. Show which documented
datasets are installed; do not limit choices to installed datasets. Discover
actual split names and counts through the dataset loader when available.
Resolve a model's friendly name to its provider ID and check availability;
show plausible matches if ambiguous rather than selecting one silently.

Invite optional overrides in one concise question: model arguments, provider
routing, credential environment-variable names, concurrency, repetitions,
costing settings, and caching. Explain the repository defaults and let the
user accept them without answering every optional field. Summarize the
effective run plan and proceed once required choices are resolved.

## 2. Prepare the environment and credentials

Follow the README to install dependencies and required optional extras.
Check the environment and `.env` for the variables required by the chosen
evaluation and costing settings, without displaying their values. If anything
is missing, identify the variable names and file location, provide empty
placeholders if helpful, and ask the user to populate `.env` locally. Never ask
for API keys, tokens, or passwords in chat. Revalidate after they have edited
the file, including small nonclinical authentication/routing checks.

## 3. Install or validate the dataset

Follow the selected dataset's README and respect any configured data-root
override. For [NEJM](data/processed_case_data/nejm/README.md), explain the
licensing/access instructions and where the supplied dataset file belongs.
For [MIMIC](data/processed_case_data/mimic/README.md), guide PhysioNet access
requirements and help download the dataset using the documented commands.
Use an existing authenticated session or a password prompt in the user's
terminal; if an interactive step cannot be performed here, give the exact
command for the user to run. Validate the downloaded file and requested splits
through the loader. Continue independent setup while waiting for access or
user input.

## 4. Prepare grounded costing

Locate the documented charge-master source. If the costing data is absent,
download it when accessible, install missing dependencies, and validate the
file against the costing code's requirements. If unavailable, explain the
specific manual step needed. Check costing-model and embedding credentials
independently, including environment-loading behavior. Initialize and reuse
valid search indexes and any requested pricing cache; verify cache access
before depending on it. Resolve costing prerequisites before the full run
where possible.

## 5. Run the evaluation and then costing

Use the packaged baseline agent, selected configuration, and agreed overrides.
Run exactly the selected cases and repetitions, deduplicating combined splits
and retaining original split membership for reporting. Save the effective
configuration, input identities, logs, and checkpoints in a run directory.
Use resumable execution. On a continuation request, locate the matching run
and its saved configuration before starting new work. Give periodic, concise
updates showing completed, remaining, and failed cases, elapsed time, and
any blockers.

Defer cost computation during evaluation, then apply the repository's grounded
costing workflow to the saved results, preserving the diagnostic results.
Give costing progress separately, including priced versus pending work,
grounded matches, model fallbacks, and failures. Reuse valid completed
costing work.

## 6. Recover and continue

Investigate failures using logs and saved artifacts. Fix dependency,
environment, and runtime problems; retry transient errors with backoff and
adjust concurrency when appropriate. Resume unfinished or failed work without
repeating successful cases or discarding valid costs. Keep the agreed model,
dataset, evaluation configuration, and routing intact during recovery. Ask
only when a required access step or substantive run-setting change needs the
user's decision. Clearly explain unresolved blockers and how to resume.

## 7. Verify and report

Check saved results rather than relying on exit status: expected unique cases
and repetitions, valid judged outcomes, complete cost analyses, and unresolved
failures. Regenerate the repository's reports, explicitly including any
combined case-set names. Present accuracy and grounded workup costs for each
requested split and their pooled result, available uncertainty estimates,
grounded/fallback/failure counts, and paths to reports and machine-readable
results. Distinguish estimated clinical workup charges from provider API spend.
State clearly whether the requested work is complete or what remains.
