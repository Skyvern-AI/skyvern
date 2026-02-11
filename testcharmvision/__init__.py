import typing
from typing import Any

from testcharmvision.forge.sdk.forge_log import setup_logger
from testcharmvision.utils import setup_windows_event_loop_policy

if typing.TYPE_CHECKING:
    from testcharmvision.library import Testcharmvision  # noqa: E402
setup_windows_event_loop_policy()
setup_logger()

# noinspection PyUnresolvedReferences
__all__ = [
    "Testcharmvision",
    "TestcharmvisionPage",
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
    "Testcharmvision": "testcharmvision.library",
    "TestcharmvisionPage": "testcharmvision.core.script_generations.testcharmvision_page",
    "RunContext": "testcharmvision.core.script_generations.testcharmvision_page",
    "setup": "testcharmvision.core.script_generations.run_initializer",
    "cached": "testcharmvision.core.script_generations.workflow_wrappers",
    "workflow": "testcharmvision.core.script_generations.workflow_wrappers",
    "action": "testcharmvision.services.script_service",
    "download": "testcharmvision.services.script_service",
    "extract": "testcharmvision.services.script_service",
    "http_request": "testcharmvision.services.script_service",
    "goto": "testcharmvision.services.script_service",
    "login": "testcharmvision.services.script_service",
    "loop": "testcharmvision.services.script_service",
    "parse_file": "testcharmvision.services.script_service",
    "parse_pdf": "testcharmvision.services.script_service",
    "prompt": "testcharmvision.services.script_service",
    "render_list": "testcharmvision.services.script_service",
    "render_template": "testcharmvision.services.script_service",
    "run_code": "testcharmvision.services.script_service",
    "run_script": "testcharmvision.services.script_service",
    "run_task": "testcharmvision.services.script_service",
    "send_email": "testcharmvision.services.script_service",
    "upload_file": "testcharmvision.services.script_service",
    "validate": "testcharmvision.services.script_service",
    "wait": "testcharmvision.services.script_service",
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
