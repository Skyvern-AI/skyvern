import typing
from typing import Any

from skyvern._version import __version__
from skyvern.utils import setup_windows_event_loop_policy

if typing.TYPE_CHECKING:
    from skyvern.library import Skyvern  # noqa: E402,F401


setup_windows_event_loop_policy()
# Server entrypoints configure package logging; base SDK imports stay side-effect-light.

_base_lazy_imports = {
    "Skyvern": "skyvern.library",
    "SkyvernEnvironment": "skyvern.client",
}

_server_lazy_imports = {
    "SkyvernPage": "skyvern.core.script_generations.skyvern_page",
    "RunContext": "skyvern.core.script_generations.skyvern_page",
    "setup": "skyvern.core.script_generations.run_initializer",
    "cached": "skyvern.core.script_generations.workflow_wrappers",
    "workflow": "skyvern.core.script_generations.workflow_wrappers",
    "conditional": "skyvern.services.script_service",
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
# Keep server-only names in __all__ for backwards-compatible wildcard exports;
# direct access remains lazily gated by the skyvern[server] extra.
_all_lazy_imports = {**_base_lazy_imports, **_server_lazy_imports}
__all__ = ["__version__", *_base_lazy_imports, *_server_lazy_imports]


def __getattr__(name: str) -> Any:
    if name in _all_lazy_imports:
        module_path = _all_lazy_imports[name]
        from importlib import import_module  # noqa: PLC0415

        try:
            module = import_module(module_path)
        except ImportError as exc:
            if name in _server_lazy_imports:
                from skyvern.exceptions import raise_server_extra_required  # noqa: PLC0415

                raise_server_extra_required(f"skyvern.{name}", exc)
            raise

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
