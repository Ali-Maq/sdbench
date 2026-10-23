"""Trusted Harbor adapter that launches the diagnosis process inside ``main``.

Harbor 0.24.0 requires Python 3.12. The remaining SDBench package and generated
role images continue to support Python 3.11. Import this module only when using
the optional Harbor extra.
"""

import json
from pathlib import Path
import shlex

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json

from sdbench.harbor_integration.trajectory import public_diagnosis_trajectory


class SDBenchAgent(BaseInstalledAgent):
    """Run the packaged client; never import a private case into the agent loop."""

    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    def name() -> str:
        return "sdbench-isolated"

    async def install(self, environment: BaseEnvironment) -> None:
        # Dependencies and client source are baked into the public role image.
        await self.exec_as_agent(
            environment,
            "python -c 'import sdbench.isolation.diagnosis'",
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Model selection is part of the immutable private trial configuration,
        # not a host-side model object or arbitrary agent-kwarg shell command.
        # Harbor syncs this public log directory before populate_context_post_run.
        # Preserve a failed client's exit status while retaining its failure log.
        public_log = shlex.quote(str(self.environment_logs_dir / "diagnosis.json"))
        await self.exec_as_agent(
            environment,
            "status=0; python -m sdbench.isolation.diagnosis "
            "--url http://gatekeeper:8080 "
            "--token-file /run/diagnosis-token "
            "--output /logs/artifacts/diagnosis.json || status=$?; "
            "if [ -f /logs/artifacts/diagnosis.json ]; then "
            f"cp /logs/artifacts/diagnosis.json {public_log} || exit 1; fi; "
            'exit "$status"',
        )
        # Leave context empty: Harbor 0.24 only invokes its post-run hook then.

    def convert_trajectory(self, logs_dir: Path) -> Trajectory | None:
        report_path = logs_dir / "diagnosis.json"
        if not report_path.is_file():
            return None
        report = json.loads(report_path.read_text())
        return public_diagnosis_trajectory(
            report,
            agent_name="sdbench-isolated-" + report.get("diagnostic_agent", "baseline"),
            model_name=self.model_name,
            session_id=self.session_id,
            trajectory_id=str(self.context_id) if self.context_id else None,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        report_path = self.logs_dir / "diagnosis.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        context.metadata = {
            "diagnostic_agent": report.get("diagnostic_agent", "baseline"),
            "isolation": "docker-containers-shared-kernel",
            "model_execution": "role-bound-gatekeeper-service",
            "trajectory_scope": "public-diagnosis-transcript",
            "usage_scope": "received-diagnosis-completions",
        }
        trajectory = self.convert_trajectory(self.logs_dir)
        if trajectory is None:
            return
        (self.logs_dir / "trajectory.json").write_text(
            format_trajectory_json(trajectory.to_json_dict()) + "\n"
        )
        metrics = trajectory.final_metrics
        context.n_input_tokens = metrics.total_prompt_tokens
        context.n_output_tokens = metrics.total_completion_tokens
        context.n_cache_tokens = metrics.total_cached_tokens
        # Never infer Azure pricing. Harbor's automatic per-model backfill
        # coerces missing token counts to zero, so suppress it for unknown usage.
        if any(value is None for value in (
            context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens,
        )):
            context.model_usage = {}
