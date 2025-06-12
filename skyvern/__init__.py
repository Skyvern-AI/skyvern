from ddtrace import tracer
from ddtrace.trace import TraceFilter, Span
from ddtrace.ext import http
import re
from skyvern.forge.sdk.forge_log import setup_logger


class FilterHeartbeat(TraceFilter):
    _HB_URL = re.compile(r"http://.*/heartbeat$")

    def process_trace(self, trace: list[Span]) -> list[Span] | None:
        for span in trace:
            url = span.get_tag(http.URL)
            if span.parent_id is None and url is not None and self._HB_URL.match(url):
                # drop the full trace chunk
                return None
        return trace


tracer.configure(trace_processors=[FilterHeartbeat()])
setup_logger()


from skyvern.forge import app  # noqa: E402, F401
from skyvern.library import Skyvern  # noqa: E402

__all__ = ["Skyvern"]
