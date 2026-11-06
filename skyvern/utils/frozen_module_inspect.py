import inspect
import sys
from types import ModuleType


def prime_inspect_for_frozen_modules() -> None:
    # Playwright falls back to inspect.stack() to capture the caller on API calls that carry no saved stack, and a
    # frame from a frozen module (runpy's two under `python -m`) misses inspect's filename cache, so every lookup
    # rescans all of sys.modules.
    # Not documented inspect API, so a Python without it skips the priming rather than failing startup.
    cache: dict[str, str] | None = getattr(inspect, "modulesbyfile", None)
    if cache is None:
        return
    for module in list(sys.modules.values()):
        if isinstance(module, ModuleType) and module.__spec__ is not None and module.__spec__.origin == "frozen":
            cache.setdefault(f"<frozen {module.__spec__.name}>", module.__spec__.name)
