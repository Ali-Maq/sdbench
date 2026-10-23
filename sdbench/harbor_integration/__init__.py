"""Optional Harbor integration; task generation does not import Harbor."""

from sdbench.harbor_integration.tasks import HARBOR_VERSION, build_task

__all__ = ["HARBOR_VERSION", "build_task"]
