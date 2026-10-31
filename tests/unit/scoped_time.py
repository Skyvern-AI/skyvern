"""Patch a module's ``time.monotonic`` without touching the process-global ``time``.

``patch("<module>.time.monotonic")`` replaces the attribute on the one ``time`` module that every thread
and the event loop's clock read. Bind a stand-in to the module under test instead::

    monkeypatch.setattr(module, "time", ScopedTime())
"""

from __future__ import annotations

import time


class ScopedTime:
    """``monotonic`` reads ``now``; every other attribute is served by the real ``time``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)
