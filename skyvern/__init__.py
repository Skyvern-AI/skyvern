from ddtrace import tracer
from ddtrace.filters import FilterRequestsOnUrl

from skyvern.forge.sdk.forge_log import setup_logger

tracer.configure(
    settings={
        "FILTERS": [
            FilterRequestsOnUrl(r"http://.*/heartbeat$"),
        ],
    },
)
setup_logger()
