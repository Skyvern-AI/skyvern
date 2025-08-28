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
from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage  # noqa: E402
from skyvern.core.script_generations.run_initializer import setup  # noqa: E402
from skyvern.core.script_generations.workflow_wrappers import (  # noqa: E402
    cached,  # noqa: E402
    workflow,  # noqa: E402
)  # noqa: E402
from skyvern.services.script_service import (  # noqa: E402
    action,  # noqa: E402
    download,  # noqa: E402
    extract,  # noqa: E402
    generate_text,  # noqa: E402
    login,  # noqa: E402
    render_template,  # noqa: E402
    run_script,  # noqa: E402
    run_task,  # noqa: E402
    wait,  # noqa: E402
)  # noqa: E402


__all__ = [
    "Skyvern",
    "SkyvernPage",
    "RunContext",
    "action",
    "cached",
    "download",
    "extract",
    "generate_text",
    "login",
    "render_template",
    "run_script",
    "run_task",
    "setup",
    "wait",
    "workflow",
]
