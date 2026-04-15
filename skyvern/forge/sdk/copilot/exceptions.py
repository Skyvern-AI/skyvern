from __future__ import annotations

from skyvern.exceptions import SkyvernException


class CopilotClientDisconnectedError(SkyvernException):
    """Raised when the SSE client disconnects during a streaming copilot run."""
