from ddtrace import tracer
from ddtrace.filters import FilterRequestsOnUrl

from skyvern.forge.sdk.forge_log import setup_logger
from typing import Any, List
from skyvern.forge.sdk.models import Step

tracer.configure(
    settings={
        "FILTERS": [
            FilterRequestsOnUrl(r"http://.*/heartbeat$"),
        ],
    },
)
setup_logger()

async def llama_handler(
    prompt: str,
    step: Step | None = None,
    screenshots: list[bytes] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Implement Llama 3.2 vision API integration here
    ...
