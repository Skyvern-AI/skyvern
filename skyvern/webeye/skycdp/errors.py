"""Error identities for the raw-CDP browser engine.

These mirror the shape the per-run engine seam expects from a driver package (see
``skyvern.webeye.browser_engine``): one base error, a timeout subclass, and a target-closed subclass,
so ``BrowserEngineSelection`` can bind them as this engine's error families. Every error raised out of
the skycdp stack derives from ``CdpError``, so a caller that catches the selected engine's base error
catches everything this engine can raise.
"""

from __future__ import annotations


class CdpError(Exception):
    """Base identity for every failure raised by the raw-CDP engine."""


class CdpTimeoutError(CdpError):
    """A command or wait exceeded its deadline."""


class CdpTargetClosedError(CdpError):
    """The websocket, browser, page, or session went away while a command was outstanding."""


class CdpExecutionContextLost(CdpError):
    """A navigation destroyed the execution context a command was addressed to.

    Distinct from target-closed on purpose: the page is still alive, so callers that poll (locators,
    load-state waits) should re-resolve and continue rather than treat it as page death.
    """


class CdpScriptCompileError(CdpError):
    """The evaluated source failed to COMPILE, so none of it ran.

    Distinct from a script that ran and threw, because only this one is safe to retry: `evaluate`
    disambiguates an expression from a statement body by trying the expression form first, and
    re-sending a script that already executed would repeat every DOM write, click and request it made
    before throwing. Chrome tells the two apart structurally -- a compile failure carries no
    stackTrace, because there were no frames to record.
    """


class CdpConnectionError(CdpError):
    """The DevTools endpoint could not be reached or the handshake failed."""


class CdpProtocolError(CdpError):
    """Chrome answered a command with a protocol-level error object."""

    def __init__(self, method: str, code: int | None, message: str, data: str | None = None) -> None:
        self.method = method
        self.code = code
        self.data = data
        detail = f"{method}: {message}"
        if data:
            detail = f"{detail} ({data})"
        super().__init__(detail)


# Chrome reports a vanished session/target through these protocol errors rather than by closing the
# socket, so they must classify as target-closed for retry/recovery branches to behave the same way
# they do under Playwright.
_TARGET_CLOSED_MARKERS = (
    "session with given id not found",
    "target closed",
    "session closed",
    "target with given id not found",
    "no target with given id",
    "inspected target navigated or closed",
    "not attached to an active page",
)

# A destroyed execution context means a navigation swapped the document out from under a command.
# The page is alive and the command is worth retrying, so this must NOT classify as target-closed --
# that identity aborts locator polling and routes recovery down the page-died branch.
_CONTEXT_LOST_MARKERS = (
    "cannot find context with specified id",
    "execution context was destroyed",
    "execution context is not available",
    "uniquecontextid not found",
)


def is_target_closed_message(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TARGET_CLOSED_MARKERS)


def is_context_lost_message(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _CONTEXT_LOST_MARKERS)


def protocol_error(method: str, error: dict) -> CdpError:
    """Build the right error identity for a CDP error payload."""
    message = str(error.get("message", "unknown protocol error"))
    if is_context_lost_message(message):
        return CdpExecutionContextLost(f"{method}: {message}")
    if is_target_closed_message(message):
        return CdpTargetClosedError(f"{method}: {message}")
    code = error.get("code")
    return CdpProtocolError(
        method=method,
        code=int(code) if isinstance(code, int) else None,
        message=message,
        data=error.get("data"),
    )


class CdpUnsupportedOperation(CdpError):
    """A Playwright capability this engine deliberately does not provide (launching, recording).

    Raised rather than silently ignored: a driver that logs a capability it dropped has already cost
    this codebase a fleet-wide stealth regression once.
    """
