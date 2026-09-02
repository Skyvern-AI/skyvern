"""``RealBrowserState.close`` runs registered on-close callbacks first.

Lets ``_start_frame_publisher`` register ``publisher.stop`` on the browser
state so any caller of ``close()`` stops the publisher implicitly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.webeye.real_browser_state import RealBrowserState


def _bare_state() -> RealBrowserState:
    """RealBrowserState with no live browser context; ``close`` should still
    invoke registered callbacks."""
    # ``pw`` is set to a no-op stub so ``close`` can short-circuit out of the
    # playwright stop branch without raising.
    pw_stub: Any = type("_PW", (), {"stop": AsyncMock()})()
    return RealBrowserState(pw=pw_stub, browser_context=None)


@pytest.mark.asyncio
async def test_close_runs_registered_callbacks_in_order() -> None:
    state = _bare_state()
    calls: list[str] = []

    async def cb_one() -> None:
        calls.append("one")

    async def cb_two() -> None:
        calls.append("two")

    state.add_on_close(cb_one)
    state.add_on_close(cb_two)

    await state.close()

    assert calls == ["one", "two"]


@pytest.mark.asyncio
async def test_close_callbacks_are_one_shot() -> None:
    """Subsequent ``close()`` calls must not re-fire previous callbacks."""
    state = _bare_state()
    invocations = 0

    async def cb() -> None:
        nonlocal invocations
        invocations += 1

    state.add_on_close(cb)
    await state.close()
    await state.close()
    assert invocations == 1


@pytest.mark.asyncio
async def test_close_swallows_callback_errors() -> None:
    """A misbehaving on-close callback must not block the rest of close."""
    state = _bare_state()
    later_ran = False

    async def cb_explodes() -> None:
        raise RuntimeError("boom")

    async def cb_later() -> None:
        nonlocal later_ran
        later_ran = True

    state.add_on_close(cb_explodes)
    state.add_on_close(cb_later)

    # Must not raise — close() is the universal teardown path.
    await state.close()
    assert later_ran is True
