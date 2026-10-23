"""Build private, local Harbor tasks with distinct role build contexts.

The diagnosis container has no case files or outbound network. It talks to a
gatekeeper sidecar on an internal Docker network. A separate verifier receives
the authoritative sidecar evidence after the diagnosis process stops. These
are container boundaries on a shared kernel, not three independent VMs.

Generated task directories contain private clinical data and per-trial access
tokens. They are local experiment inputs, not redistributable benchmark bundles.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any, Mapping

import yaml

from sdbench.isolation.common import write_json

HARBOR_VERSION = "0.24.0"
AGENT_TIMEOUT_SECONDS = 1800
ISOLATION = "docker-containers-shared-kernel"
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PROVIDER_ENV = ("OPENAI_API_KEY",)


def _azure_runtime() -> dict[str, Any]:
    """Compose binds a host-managed credential directory only into trusted roles."""
    return {
        "environment": {"SDBENCH_AZURE_TOKEN_FILE": "/run/sdbench-azure/token.json"},
        "volumes": [{
            "type": "bind", "source": "${SDBENCH_AZURE_TOKEN_DIR:?Use the Azure Harbor launcher}",
            "target": "/run/sdbench-azure", "read_only": True,
            "bind": {"create_host_path": False},
        }],
    }
# Python 3.11/Linux dependency closure from this repository's uv.lock. Keep this
# snapshot in sync when intentionally updating the role runtime. Do not resolve
# fresh versions during an experiment build.
_RUNTIME_REQUIREMENTS = """annotated-types==0.7.0
anyio==4.15.1
certifi==2026.6.17
distro==1.9.0
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.18
jiter==0.15.0
openai==2.44.0
pydantic==2.13.4
pydantic-core==2.46.4
pyyaml==6.0.3
sniffio==1.3.1
tqdm==4.68.3
typing-extensions==4.16.0
typing-inspection==0.4.2
"""
_BASE_DOCKERFILE = """FROM python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SDBENCH_LOCAL_DIR=disable
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt && pip check
WORKDIR /app
COPY sdbench /app/sdbench
"""


def _check_no_credentials(value: Any, path: str = "trial_config") -> None:
    """Provider credentials must be supplied at runtime, never baked into images."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {"api_key", "authorization", "password", "secret"} or key_lower.endswith("_api_key"):
                raise ValueError(f"{path}.{key} must be supplied through runtime environment variables")
            _check_no_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_no_credentials(item, f"{path}[{index}]")


def _copy_code(destination: Path, *, public: bool) -> None:
    """Copy package source, never repository contents, datasets, caches or .env."""
    for source in sorted(_PACKAGE_ROOT.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(_PACKAGE_ROOT)
        if set(relative.parts) & {"_bundled_data", "__pycache__", "harbor_integration", "cli"}:
            continue
        if source.suffix not in {".py", ".yaml"}:
            continue
        if public and (source.suffix != ".py" or relative.as_posix() in {"isolation/service.py", "isolation/judge.py"}):
            continue
        target = destination / "sdbench" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _runtime_provenance(directory: Path) -> dict[str, Any]:
    """Record the exact copied source and dependency snapshot without private data."""
    inventory = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((directory / "sdbench").rglob("*"))
        if path.is_file()
    }
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return {
        "source_inventory": inventory,
        "source_inventory_sha256": hashlib.sha256(encoded).hexdigest(),
        "requirements_sha256": hashlib.sha256((directory / "requirements.txt").read_bytes()).hexdigest(),
        "dockerfile_sha256": hashlib.sha256((directory / "Dockerfile").read_bytes()).hexdigest(),
    }


def _task_toml(case: Mapping[str, Any], config: dict[str, Any]) -> str:
    quoted = lambda value: json.dumps(str(value), ensure_ascii=True)
    result = f'''schema_version = "1.4"
artifacts = [
  {{ source = "/logs/artifacts" }},
  {{ source = "/private/evidence", destination = "gatekeeper-evidence", service = "gatekeeper" }},
]

[metadata]
case_id = {quoted(case["case_id"])}
dataset = {quoted(case.get("dataset", "unknown"))}
api_mode = {quoted(config["api_mode"])}
diagnostic_agent = {quoted(config.get("diagnostic_agent", "baseline"))}
isolation = "{ISOLATION}"
harbor_version = "{HARBOR_VERSION}"

[agent]
timeout_sec = {AGENT_TIMEOUT_SECONDS:.1f}

[environment]
build_timeout_sec = 900.0
cpus = 1
memory_mb = 1024

[verifier]
environment_mode = "separate"
timeout_sec = 600.0

[verifier.environment]
cpus = 1
memory_mb = 1024
'''
    if config["api_mode"] == "live" and config.get("provider", "openai") != "azure":
        result += "\n[verifier.env]\n"
        result += "".join(f'{name} = "${{{name}:-}}"\n' for name in _PROVIDER_ENV)
    return result


def build_task(
    case: Mapping[str, Any], trial_config: Mapping[str, Any], destination: str | Path
) -> Path:
    """Create one local Harbor task and return its path.

    ``trial_config`` must be JSON serializable. ``api_mode`` defaults to ``mock``;
    only the explicit value ``live`` enables outbound provider access. The
    destination must not already exist. No model or Docker calls occur here.
    """
    config = json.loads(json.dumps(dict(trial_config), allow_nan=False))
    case_data = json.loads(json.dumps(dict(case), allow_nan=False))
    for field in ("case_id", "case_summary", "full_text", "final_diagnosis"):
        if field not in case_data or not str(case_data[field]).strip():
            raise ValueError(f"case.{field} must be present and nonempty")
    config.setdefault("api_mode", "mock")
    config.setdefault("diagnostic_agent", "baseline")
    if config["diagnostic_agent"] != "baseline":
        raise ValueError("Only the released baseline diagnostic_agent is supported")
    if config["api_mode"] not in {"mock", "live"}:
        raise ValueError("trial_config.api_mode must be 'mock' or 'live'")
    _check_no_credentials(config)
    config["diagnosis_token"] = secrets.token_urlsafe(32)
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Refusing to replace existing task: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        main = staging / "environment" / "diagnosis"
        gatekeeper = staging / "environment" / "gatekeeper"
        judge = staging / "tests"
        for directory, public in ((main, True), (gatekeeper, False), (judge, False)):
            directory.mkdir(parents=True)
            _copy_code(directory, public=public)
            (directory / ".dockerignore").write_text("**/__pycache__\n**/*.pyc\n.env\n")
            (directory / "requirements.txt").write_text(_RUNTIME_REQUIREMENTS)
        (main / "diagnosis-token").write_text(config["diagnosis_token"] + "\n")
        (main / "Dockerfile").write_text(
            _BASE_DOCKERFILE + "COPY diagnosis-token /run/diagnosis-token\n"
        )
        for directory in (gatekeeper, judge):
            write_json(directory / "private" / "trial.json", config)
            write_json(directory / "private" / "case.json", case_data)
        (gatekeeper / "Dockerfile").write_text(
            _BASE_DOCKERFILE
            + "COPY private /private\n"
            + "RUN mkdir -p /private/evidence\n"
            + 'CMD ["python", "-m", "sdbench.isolation.service", "--config", "/private/trial.json", "--case", "/private/case.json", "--output-dir", "/private/evidence", "--port", "8080"]\n'
        )
        (judge / "Dockerfile").write_text(
            _BASE_DOCKERFILE + "COPY private /private\nCOPY test.sh /tests/test.sh\n"
        )
        if config["api_mode"] == "mock":
            # Docker's native network namespace works on Docker Desktop even
            # where Harbor's optional policy backend reports no capability.
            (judge / "docker-compose.yaml").write_text(
                yaml.safe_dump({"services": {"main": {"network_mode": "none"}}})
            )
        elif config.get("provider") == "azure":
            (judge / "docker-compose.yaml").write_text(
                yaml.safe_dump({"services": {"main": _azure_runtime()}})
            )
        (judge / "test.sh").write_text(
            "#!/bin/sh\nset -eu\n"
            "test -s /private/evidence/events.jsonl\n"
            "python -m sdbench.isolation.judge --config /private/trial.json "
            "--case /private/case.json --evidence /private/evidence/events.jsonl "
            "--output-dir /logs/verifier\n"
        )
        os.chmod(judge / "test.sh", 0o755)
        service: dict[str, Any] = {
            "build": {"context": "./gatekeeper"},
            "networks": ["clinical"],
            "healthcheck": {
                "test": ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=2).close()"],
                "interval": "1s", "timeout": "3s", "retries": 60,
            },
        }
        networks: dict[str, Any] = {"clinical": {"internal": True}}
        if config["api_mode"] == "live":
            service["networks"].append("model_api")
            if config.get("provider") == "azure":
                service.update(_azure_runtime())
            else:
                service["environment"] = {name: f"${{{name}:-}}" for name in _PROVIDER_ENV}
            networks["model_api"] = {}
        compose = {
            "services": {
                "main": {
                    "build": {"context": "./diagnosis"},
                    "networks": ["clinical"],
                    "depends_on": {"gatekeeper": {"condition": "service_healthy"}},
                },
                "gatekeeper": service,
            },
            "networks": networks,
        }
        (staging / "environment" / "docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        (staging / "task.toml").write_text(_task_toml(case_data, config))
        (staging / "instruction.md").write_text(
            "Diagnose the patient through the provided sequential-diagnosis client. "
            "The authenticated gatekeeper session supplies the initial presentation. "
            "Save the final client output to /logs/artifacts/diagnosis.json.\n"
        )
        write_json(staging / "isolation.json", {
            "isolation": ISOLATION,
            "harbor_version": HARBOR_VERSION,
            "agent_build_context": "environment/diagnosis",
            "gatekeeper_build_context": "environment/gatekeeper",
            "judge_build_context": "tests",
            "agent_network": "internal-only",
            "judge_network": "none" if config["api_mode"] == "mock" else "provider-egress",
            "authoritative_evidence_service": "gatekeeper",
            "private_task": True,
            "runtime_provenance": {
                "diagnosis": _runtime_provenance(main),
                "gatekeeper": _runtime_provenance(gatekeeper),
                "judge": _runtime_provenance(judge),
            },
        })
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
