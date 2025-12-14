import re
import typing
from typing import Any

from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.utils import setup_windows_event_loop_policy

if typing.TYPE_CHECKING:
    from skyvern.library import Skyvern  # noqa: E402

try:
    from ddtrace import tracer
    from ddtrace.ext import http
    from ddtrace.trace import TraceFilter, Span

    class FilterHeartbeat(TraceFilter):
        _HB_URL = re.compile(r"http://.*/heartbeat$")

        def process_trace(self, trace: list[Span]) -> list[Span] | None:
            for span in trace:
                url = span.get_tag(http.URL)
                if span.parent_id is None and url is not None and self._HB_URL.match(url):
                    # drop the full trace chunk
                    return None
            return trace

    _DDTRACE_AVAILABLE = True
except ImportError:
    _DDTRACE_AVAILABLE = False


setup_windows_event_loop_policy()
if _DDTRACE_AVAILABLE:
    tracer.configure(trace_processors=[FilterHeartbeat()])
setup_logger()

# noinspection PyUnresolvedReferences
__all__ = [
    "Skyvern",
    "SkyvernPage",
    "RunContext",
    "action",
    "cached",
    "download",
    "extract",
    "http_request",
    "goto",
    "login",
    "loop",
    "parse_file",
    "parse_pdf",
    "prompt",
    "render_list",
    "render_template",
    "run_code",
    "run_script",
    "run_task",
    "send_email",
    "setup",
    "upload_file",
    "validate",
    "wait",
    "workflow",
]

_lazy_imports = {
    "Skyvern": "skyvern.library",
    "SkyvernPage": "skyvern.core.script_generations.skyvern_page",
    "RunContext": "skyvern.core.script_generations.skyvern_page",
    "setup": "skyvern.core.script_generations.run_initializer",
    "cached": "skyvern.core.script_generations.workflow_wrappers",
    "workflow": "skyvern.core.script_generations.workflow_wrappers",
    "action": "skyvern.services.script_service",
    "download": "skyvern.services.script_service",
    "extract": "skyvern.services.script_service",
    "http_request": "skyvern.services.script_service",
    "goto": "skyvern.services.script_service",
    "login": "skyvern.services.script_service",
    "loop": "skyvern.services.script_service",
    "parse_file": "skyvern.services.script_service",
    "parse_pdf": "skyvern.services.script_service",
    "prompt": "skyvern.services.script_service",
    "render_list": "skyvern.services.script_service",
    "render_template": "skyvern.services.script_service",
    "run_code": "skyvern.services.script_service",
    "run_script": "skyvern.services.script_service",
    "run_task": "skyvern.services.script_service",
    "send_email": "skyvern.services.script_service",
    "upload_file": "skyvern.services.script_service",
    "validate": "skyvern.services.script_service",
    "wait": "skyvern.services.script_service",
}


def __getattr__(name: str) -> Any:
    if name in _lazy_imports:
        module_path = _lazy_imports[name]
        from importlib import import_module  # noqa: PLC0415

        module = import_module(module_path)

        # For attributes that need to be extracted from the module
        if hasattr(module, name):
            value = getattr(module, name)
        else:
            # For module-level imports like "app"
            value = module

        # Cache the imported value
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
