# Upstream sources

This fork starts from Microsoft SDBench commit
[`304ce9625a046cea97d64e3b1ba5d19217b5095c`](https://github.com/microsoft/sdbench/tree/304ce9625a046cea97d64e3b1ba5d19217b5095c).
The released diagnostic agent is [`baseline.py`](https://github.com/microsoft/sdbench/blob/304ce9625a046cea97d64e3b1ba5d19217b5095c/sdbench/agents/baseline.py).

The October 22, 2026 source review checked:

| Source | What it establishes |
| --- | --- |
| [Official repository](https://github.com/microsoft/sdbench) | Baseline diagnostic loop, gatekeeper, judge, costing and prompt templates |
| [Science article](https://www.science.org/doi/10.1126/science.aeh7930) | Published method and evaluation |
| [Supplementary methods](https://www.science.org/doi/suppl/10.1126/science.aeh7930/suppl_file/science.aeh7930_sm.pdf) | MAI-DxO role descriptions and architecture; Figure S4 is the baseline prompt |
| [Paper-linked archive](https://zenodo.org/records/21286121) | Runtime Python/YAML files match the pinned repository; no additional MAI-DxO implementation |

The executable original MAI-DxO orchestrator and complete matching prompts were
not found in those sources. That does not rule out private code or a later release.
This fork's earlier experimental panel was independently written and has been
removed; it was not an original MAI-DxO implementation. Git history retains it.

Archive verification: `sdbench-main.zip`, 1,530,176 bytes,
MD5 `3cee3d04cf37b36e74e52c91eda95efa`.
Supplement SHA-256:
`617f8ce7df659e7353d25df8bad36e375f6fa875ad6768203c11a9ad9975ed61`.

Harbor is pinned to **0.24.0**. The adapter uses its custom-agent, multi-container,
separate-verifier, artifact and ATIF interfaces. Installed-version schemas and
integration tests govern compatibility; newer web documentation can differ.
See the [Harbor implementation guide](HARBOR_EXPERIMENT.md).
