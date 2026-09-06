"""Patch a module's sleeps without touching the process-global ``asyncio``.

``monkeypatch.setattr(asyncio, "sleep", ...)`` -- and every spelling that resolves to the same
object: ``setattr(<module>.asyncio, "sleep", ...)``, ``patch("<module>.asyncio.sleep")`` --
mutates the one ``asyncio`` module shared by every thread in the process. A coroutine sleeping
on any other thread then lands in the test's recorder, so an exact await count depends on what
else is alive in the worker rather than on the code under test (SKY-14332).

Bind a stand-in to the module under test instead::

    monkeypatch.setattr(module, "asyncio", ScopedAsyncio(sleep=recorder))

``module.asyncio.sleep`` is then the recorder for that module only; every other attribute is
served by the real ``asyncio``. Only ``asyncio.<name>`` lookups at call time are intercepted,
which is how the repo writes them -- a module that binds ``from asyncio import sleep`` is
already module-local and needs no stand-in.
"""

from __future__ import annotations

import asyncio


class ScopedAsyncio:
    def __init__(self, **overrides: object) -> None:
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)
