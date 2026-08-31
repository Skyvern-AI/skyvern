"""Control-endpoint probe behaviour for browser-invariant failures (SKY-14877 AC3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.browser_extension.protocol import is_cdp_method_allowed
from skyvern.webeye.browser_diagnostics import (
    _PENDING,
    PROBE_METHODS,
    collect_control_endpoint_diagnostics,
    schedule_control_endpoint_diagnostics,
)


class _Session:
    """CDP session whose per-command behaviour the test dictates."""

    def __init__(self, behaviours: dict[str, Any]) -> None:
        self._behaviours = behaviours
        self.detached = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        behaviour = self._behaviours.get(method)
        if behaviour == "hang":
            await asyncio.Event().wait()
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour or {}

    async def detach(self) -> None:
        self.detached = True


def _page(session: _Session | str, *, frames: int = 2, pages: int = 1) -> Any:
    async def new_cdp_session(_page: Any) -> _Session:
        if session == "hang":
            await asyncio.Event().wait()
        assert isinstance(session, _Session)
        return session

    context = SimpleNamespace(pages=[object()] * pages, new_cdp_session=new_cdp_session)
    return SimpleNamespace(context=context, frames=[object()] * frames, is_closed=lambda: False)


@pytest.mark.asyncio
async def test_hung_control_endpoint_is_recorded_rather_than_raised() -> None:
    """A probe that never answers is the finding, so it must be reported, not swallowed."""
    fields = await collect_control_endpoint_diagnostics(_page("hang"), timeout=0.05)

    assert fields["probe_cdp_session"] == "timeout"
    assert fields["probe_page_closed"] is False


def test_probe_uses_only_allowlisted_cdp_prefixes() -> None:
    """Browser. is absent from the extension allowlist; a rejected probe would be
    recorded as a dead endpoint on a healthy browser."""
    unlisted = sorted(method for method in PROBE_METHODS if not is_cdp_method_allowed(method))

    assert not unlisted, f"probe uses CDP methods an extension-backed browser rejects: {unlisted}"


@pytest.mark.asyncio
async def test_process_exit_is_distinguishable_from_a_hung_endpoint() -> None:
    """A gone process fails at session open; a hung one times out. AC3's core distinction."""

    async def refused(_page: Any) -> Any:
        raise ConnectionRefusedError("target closed")

    page = SimpleNamespace(
        context=SimpleNamespace(pages=[object()], new_cdp_session=refused),
        frames=[object()],
        is_closed=lambda: False,
    )

    fields = await collect_control_endpoint_diagnostics(page, timeout=0.05)

    assert fields["probe_cdp_session"] == "error:ConnectionRefusedError"
    # The command probes are unreachable without a session, so their absence is meaningful.
    assert "probe_browser_endpoint" not in fields
    assert "probe_renderer" not in fields


@pytest.mark.asyncio
async def test_browser_and_renderer_outcomes_are_reported_separately() -> None:
    """AC3's distinction: the browser process can answer while the renderer is wedged."""
    session = _Session({"Target.getTargets": {"product": "x"}, "Runtime.evaluate": "hang"})

    fields = await collect_control_endpoint_diagnostics(_page(session), timeout=0.05)

    assert fields["probe_browser_endpoint"] == "ok"
    assert fields["probe_renderer"] == "timeout"
    assert session.detached is True


@pytest.mark.asyncio
async def test_metrics_failure_does_not_hide_the_reachable_endpoint() -> None:
    session = _Session(
        {
            "Target.getTargets": {},
            "Runtime.evaluate": {},
            "Performance.getMetrics": RuntimeError("boom"),
        }
    )

    fields = await collect_control_endpoint_diagnostics(_page(session), timeout=0.05)

    assert fields["probe_browser_endpoint"] == "ok"
    assert fields["probe_metrics"] == "error:RuntimeError"


@pytest.mark.asyncio
async def test_scheduling_never_blocks_or_raises_into_the_caller() -> None:
    """The probe observes an already-failing operation; it must not extend or break it."""
    loop = asyncio.get_running_loop()
    before = set(_PENDING)

    started = loop.time()
    schedule_control_endpoint_diagnostics(_page("hang"), "probe", workflow_run_block_id="wrb_1")
    elapsed = loop.time() - started

    # Returns without awaiting the probe, even though the endpoint never answers.
    assert elapsed < 0.05
    scheduled = set(_PENDING) - before
    assert len(scheduled) == 1

    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)
