import importlib.util

from skyvern.forge.sdk.routes import agent_protocol  # noqa: F401
from skyvern.forge.sdk.routes import browser_profiles  # noqa: F401
from skyvern.forge.sdk.routes import browser_sessions  # noqa: F401
from skyvern.forge.sdk.routes import credentials  # noqa: F401
from skyvern.forge.sdk.routes import custom_llms  # noqa: F401
from skyvern.forge.sdk.routes import debug_sessions  # noqa: F401
from skyvern.forge.sdk.routes import prompts  # noqa: F401
from skyvern.forge.sdk.routes import pylon  # noqa: F401
from skyvern.forge.sdk.routes import run_blocks  # noqa: F401
from skyvern.forge.sdk.routes import runtime_config  # noqa: F401
from skyvern.forge.sdk.routes import scripts  # noqa: F401
from skyvern.forge.sdk.routes import sdk  # noqa: F401
from skyvern.forge.sdk.routes import webhooks  # noqa: F401
from skyvern.forge.sdk.routes import workflow_schedules  # noqa: F401
from skyvern.forge.sdk.routes.streaming import cdp_input  # noqa: F401
from skyvern.forge.sdk.routes.streaming import messages  # noqa: F401
from skyvern.forge.sdk.routes.streaming import screenshot  # noqa: F401
from skyvern.forge.sdk.routes.streaming import vnc  # noqa: F401

# Workflow Copilot depends on openai-agents, imported as `agents`, which is
# intentionally server-only.
# skyvern[local] starts the embedded API app without that extra, so skip these
# private routes unless the SDK package is installed.
if importlib.util.find_spec("agents") is not None:
    from skyvern.forge.sdk.routes import workflow_copilot  # noqa: F401
