"""Control-endpoint probe behaviour for browser-invariant failures (SKY-14877 AC3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page

from skyvern.browser_extension.protocol import is_cdp_method_allowed
from skyvern.webeye.browser_diagnostics import (
    _PENDING,
    PROBE_METHODS,
    collect_control_endpoint_diagnostics,
    collect_page_memory_diagnostics,
    schedule_control_endpoint_diagnostics,
)
from skyvern.webeye.main_world_eval import clear_main_world_prefix, configure_main_world_prefix


class _Session:
    """CDP session whose per-command behaviour the test dictates."""

    def __init__(self, behaviours: dict[str, Any]) -> None:
        self._behaviours = behaviours
        self.detached = False
        self.calls: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(method)
        behaviour = self._behaviours.get(method)
        if behaviour == "hang":
            await asyncio.Event().wait()
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour or {}

    async def detach(self) -> None:
        self.calls.append("detach")
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


@pytest.mark.asyncio
async def test_metrics_domain_is_enabled_for_the_read_and_disabled_before_detach() -> None:
    """Performance.getMetrics answers an empty list unless the domain was enabled on that session."""
    session = _Session(
        {
            "Performance.getMetrics": {
                "metrics": [
                    {"name": "JSHeapUsedSize", "value": 5.0e9},
                    {"name": "Nodes", "value": 250000},
                    {"name": "JSEventListeners", "value": 90000},
                    {"name": "ScriptDuration", "value": 1.0},
                ]
            }
        }
    )

    fields = await collect_control_endpoint_diagnostics(_page(session), timeout=0.05)

    calls = session.calls
    assert calls.index("Performance.enable") < calls.index("Performance.getMetrics")
    assert calls.index("Performance.getMetrics") < calls.index("Performance.disable") < calls.index("detach")
    assert fields["probe_metric_JSHeapUsedSize"] == 5.0e9
    assert fields["probe_metric_Nodes"] == 250000
    assert fields["probe_metric_JSEventListeners"] == 90000
    assert "probe_metric_ScriptDuration" not in fields


@pytest.mark.asyncio
async def test_metrics_domain_is_disabled_even_when_the_read_hangs() -> None:
    session = _Session({"Performance.getMetrics": "hang"})

    fields = await collect_control_endpoint_diagnostics(_page(session), timeout=0.05)

    assert fields["probe_metrics"] == "timeout"
    assert session.calls[-2:] == ["Performance.disable", "detach"]


class _Frame:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.expressions: list[str] = []

    async def evaluate(self, expression: str) -> Any:
        self.expressions.append(expression)
        if self._result == "hang":
            await asyncio.Event().wait()
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_page_memory_probe_reads_only_allowlisted_observer_scalars() -> None:
    frame = _Frame({"listening": True, "jobs": 41, "pending": 3, "retained_nodes": 12000, "nodeHtml": "<div>x</div>"})

    fields = await collect_page_memory_diagnostics(None, frame, timeout=0.05)

    assert fields["observer_probe"] == "partial"
    assert fields["observer_jobs"] == 41
    assert fields["observer_retained_nodes"] == 12000
    assert not any("html" in key.lower() for key in fields)
    assert "probe_cdp_session" not in fields
    # The read must never inject or call into the DOM-walking helpers.
    assert "readIncrementalObserverStats" in frame.expressions[0]
    assert "with_dom_utils" not in frame.expressions[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "outcome"), [("hang", "timeout"), (RuntimeError("gone"), "error:RuntimeError")])
async def test_page_memory_probe_bounds_an_unresponsive_renderer(result: Any, outcome: str) -> None:
    session = _Session({"Runtime.evaluate": "hang"})

    fields = await collect_page_memory_diagnostics(_page(session), _Frame(result), timeout=0.05)

    assert fields["observer_probe"] == outcome
    assert fields["probe_renderer"] == "timeout"
    assert session.detached is True


class _PrefixContext:
    """Hashable, weakly referenceable stand-in for a BrowserContext keyed by the prefix registry."""


@pytest.mark.asyncio
async def test_page_memory_probe_reads_observer_in_the_main_world_when_a_prefix_is_configured() -> None:
    """Helpers on prefix-configured contexts live in the main world; an isolated read would report absent."""
    context = _PrefixContext()
    page = MagicMock(spec=Page)
    page.context = context
    page.evaluate = AsyncMock(return_value=None)
    session = MagicMock()
    session.send = AsyncMock(return_value={"result": {"value": {"listening": True, "jobs": 5, "retained_nodes": 77}}})
    session.detach = AsyncMock()
    context.new_cdp_session = AsyncMock(return_value=session)  # type: ignore[attr-defined]
    configure_main_world_prefix(context, "/*main-world-marker*/")  # type: ignore[arg-type]
    try:
        fields = await collect_page_memory_diagnostics(None, page, timeout=0.5)
    finally:
        clear_main_world_prefix(context)  # type: ignore[arg-type]

    page.evaluate.assert_not_awaited()
    sent = session.send.await_args
    assert sent.args[0] == "Runtime.evaluate"
    assert sent.args[1]["expression"].startswith("/*main-world-marker*/")
    assert "readIncrementalObserverStats" in sent.args[1]["expression"]
    assert fields["observer_probe"] == "partial"
    assert fields["observer_jobs"] == 5
    assert fields["observer_retained_nodes"] == 77


class _HelperGatedFrame:
    """Evaluates only the guard the probe sends: the helper is called iff the page exposes it."""

    def __init__(self, helper: Any = None) -> None:
        self.helper = helper
        self.expressions: list[str] = []

    async def evaluate(self, expression: str) -> Any:
        self.expressions.append(expression)
        assert "typeof globalThis.readIncrementalObserverStats === 'function'" in expression
        return self.helper() if callable(self.helper) else None


@pytest.mark.asyncio
async def test_page_memory_probe_reports_absent_when_the_page_has_no_observer_helper() -> None:
    frame = _HelperGatedFrame()

    fields = await collect_page_memory_diagnostics(None, frame, timeout=0.05)

    assert fields["observer_probe"] == "absent"
    assert not any(key.startswith("observer_") and key not in {"observer_probe", "observer_probe_ms"} for key in fields)
    assert len(frame.expressions) == 1


@pytest.mark.asyncio
async def test_page_memory_probe_reports_ok_only_when_every_observer_stat_is_present() -> None:
    complete = {
        "listening": True,
        "jobs": 3,
        "pending": 0,
        "depth_buckets": 4,
        "retained_nodes": 12,
        "parsed": 9,
        "version": None,
    }
    partial = {"listening": False, "jobs": 3, "retained_nodes": 12, "pending": "many"}

    full = await collect_page_memory_diagnostics(None, _HelperGatedFrame(lambda: complete), timeout=0.05)
    missing = await collect_page_memory_diagnostics(None, _HelperGatedFrame(lambda: partial), timeout=0.05)

    assert full["observer_probe"] == "ok"
    assert full["observer_version"] is None and full["observer_depth_buckets"] == 4
    assert missing["observer_probe"] == "partial"
    assert missing["observer_jobs"] == 3 and missing["observer_retained_nodes"] == 12
    # Unreported stats stay absent rather than appearing as a null that reads like a reported value.
    assert not {"observer_pending", "observer_depth_buckets", "observer_parsed", "observer_version"} & set(missing)
