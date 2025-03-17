from ddtrace import tracer
from ddtrace.filters import FilterRequestsOnUrl

from skyvern.agent import SkyvernAgent, SkyvernClient
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunResponse

tracer.configure(
    settings={
        "FILTERS": [
            FilterRequestsOnUrl(r"http://.*/heartbeat$"),
        ],
    },
)
setup_logger()

__all__ = ["SkyvernAgent", "SkyvernClient", "WorkflowRunResponse"]
