"""Trusted Harbor adapter that launches the diagnosis process inside ``main``.

Harbor 0.24.0 requires Python 3.12. The remaining SDBench package and generated
role images continue to support Python 3.11. Import this module only when using
the optional Harbor extra.
"""

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class SDBenchAgent(BaseInstalledAgent):
    """Run the packaged client; never import a private case into the agent loop."""

    @staticmethod
    def name() -> str:
        return "sdbench-isolated-baseline"

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
        await self.exec_as_agent(
            environment,
            "python -m sdbench.isolation.diagnosis "
            "--url http://gatekeeper:8080 "
            "--token-file /run/diagnosis-token "
            "--output /logs/artifacts/diagnosis.json",
        )
        context.metadata = {
            "isolation": "docker-containers-shared-kernel",
            "model_execution": "role-bound-gatekeeper-service",
        }
