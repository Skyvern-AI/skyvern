from __future__ import annotations

import sys
from types import ModuleType
from typing import Sequence
from unittest.mock import MagicMock

# Importing a streaming route module triggers skyvern.forge.sdk.routes.__init__,
# which eagerly imports sibling route modules with heavy side effects (e.g. AWS clients).
# Stub those modules so streaming-only tests stay fast and deterministic.
_BASE_STUB_MODULES: list[str] = [
    "skyvern.forge.sdk.api.aws",
    "skyvern.forge.sdk.routes.agent_protocol",
    "skyvern.forge.sdk.routes.browser_profiles",
    "skyvern.forge.sdk.routes.browser_sessions",
    "skyvern.forge.sdk.routes.credentials",
    "skyvern.forge.sdk.routes.debug_sessions",
    "skyvern.forge.sdk.routes.prompts",
    "skyvern.forge.sdk.routes.pylon",
    "skyvern.forge.sdk.routes.run_blocks",
    "skyvern.forge.sdk.routes.scripts",
    "skyvern.forge.sdk.routes.sdk",
    "skyvern.forge.sdk.routes.webhooks",
    "skyvern.forge.sdk.routes.workflow_copilot",
    "skyvern.forge.sdk.routes.streaming.cdp_input",
    "skyvern.forge.sdk.routes.streaming.messages",
    "skyvern.forge.sdk.routes.streaming.notifications",
    "skyvern.forge.sdk.routes.streaming.vnc",
]


def import_with_stubs(module_path: str, extra_stubs: Sequence[str] = ()) -> ModuleType:
    """Import a streaming module after temporarily stubbing heavy dependencies."""
    all_stubs = list(_BASE_STUB_MODULES) + list(extra_stubs)
    installed: dict[str, MagicMock] = {}
    for mod in all_stubs:
        if mod not in sys.modules:
            installed[mod] = sys.modules[mod] = MagicMock()

    try:
        __import__(module_path)
        return sys.modules[module_path]
    finally:
        for mod in installed:
            del sys.modules[mod]
