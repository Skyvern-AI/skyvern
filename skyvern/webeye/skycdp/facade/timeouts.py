"""The one place milliseconds become seconds.

Playwright's public API takes timeouts in milliseconds and every Skyvern call site was written
against that -- `settings.BROWSER_ACTION_TIMEOUT_MS` is 5000, forwarded by two dozen call sites in
`webeye/actions/handler.py`. This engine's internals wait in seconds, so the conversion has to happen
exactly once, at the boundary, or a five-second budget silently becomes eighty-three minutes and the
run hangs with nothing in the logs to say why.
"""

from __future__ import annotations

# Playwright's default action timeout, so an omitted budget behaves the same on either engine.
DEFAULT_ACTION_TIMEOUT_MS = 30_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000


def seconds_from_ms(timeout_ms: float | None, default_ms: float = DEFAULT_ACTION_TIMEOUT_MS) -> float:
    """Convert a caller's millisecond budget into the seconds the internals wait in."""
    resolved = default_ms if timeout_ms is None else timeout_ms
    if resolved < 0:
        raise ValueError(f"timeout must not be negative; got {timeout_ms}")
    return resolved / 1000
