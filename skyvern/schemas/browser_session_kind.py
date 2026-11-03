"""A leaf module with no Skyvern imports, for the same import-cycle reason as ``browser_session_close``."""

from enum import StrEnum

# runnable_type of an unclaimed Copilot pre-started browser; uptime billing skips it until a test claims it.
COPILOT_PRESTART_RUNNABLE_TYPE = "copilot_build_test_prestart"


class BrowserSessionKind(StrEnum):
    """Which surface requested a browser session. Logged on its creation and Chrome-exit lines, never stored on
    the session row: created_by cannot tell an API session a signed-in user made from an editor session."""

    # Anything calling POST /v1/browser_sessions: the SDK, MCP and the UI's Browser Sessions page alike.
    api = "api"
    editor = "editor"
    editor_prewarm = "editor_prewarm"
    copilot = "copilot"
    workflow_run = "workflow_run"
