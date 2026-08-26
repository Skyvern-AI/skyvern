"""Bounded CDP probes that run when a browser operation has already failed.

``Target.getTargets`` is answered by the browser process and ``Runtime.evaluate`` by
the renderer, so their outcomes disagree when only one side is wedged. Read the logged
fields against this table rather than deriving the mapping:

===================  ======================  ==============  =====================================
probe_cdp_session    probe_browser_endpoint  probe_renderer  reading
===================  ======================  ==============  =====================================
timeout              not reached             not reached     control endpoint hung; no session
error:*              not reached             not reached     connection gone -- process exit or
                                                             closed target (refuses fast, the
                                                             shape a hung endpoint does NOT show)
ok                   timeout                 timeout         browser process wedged, socket alive
ok                   ok                      timeout         renderer saturated, browser healthy
ok                   timeout                 ok              browser-level stall only; unexpected
ok                   error:*                 any             answered and REFUSED, not hung: method
                                                             rejected by a transport that relays a
                                                             subset, or target closed mid-probe.
                                                             Process-exit shape, not the wedge
ok                   ok                      error:*         renderer refused rather than hung;
                                                             same reading, renderer side
ok                   ok                      ok              control plane healthy when probed;
                                                             look outside CDP (e.g. frame pipeline)
===================  ======================  ==============  =====================================

A process exit surfaces at session open rather than at both commands, because the
probes are only reached once a session exists.

Every probe carries its own timeout and reports its own failure mode, because a
probe that does not answer is itself the finding. Probes are scheduled off the
observed operation so a wedged endpoint can never add latency to the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

LOG = structlog.get_logger()

PROBE_TIMEOUT_SECONDS = 2.0

# Browser-process command, then renderer, then metrics. Declared here so the
# allowlist guard checks the contract rather than how the call is spelled.
BROWSER_PROBE_METHOD = "Target.getTargets"
RENDERER_PROBE_METHOD = "Runtime.evaluate"
METRICS_PROBE_METHOD = "Performance.getMetrics"
PROBE_METHODS = (BROWSER_PROBE_METHOD, RENDERER_PROBE_METHOD, METRICS_PROBE_METHOD)

_METRICS_OF_INTEREST = frozenset(
    {"Documents", "Frames", "JSHeapUsedSize", "JSHeapTotalSize", "LayoutCount", "Nodes", "TaskDuration"}
)

# Fire-and-forget tasks are strongly referenced until they finish; the event loop
# only holds weak references and would otherwise collect them mid-flight.
_PENDING: set[asyncio.Task[None]] = set()


async def _timed(coro: Any, timeout: float) -> tuple[str, float]:
    """Await ``coro`` under ``timeout``; return its outcome label and elapsed ms."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        return "ok", (loop.time() - started) * 1000
    except TimeoutError:
        return "timeout", (loop.time() - started) * 1000
    except Exception as exc:
        return f"error:{type(exc).__name__}", (loop.time() - started) * 1000


def _page_closed(page: Any) -> bool | None:
    try:
        return bool(page.is_closed())
    except Exception:
        return None


async def collect_control_endpoint_diagnostics(page: Any, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Probe the browser and renderer control paths. Never raises; never logs URLs."""
    fields: dict[str, Any] = {"probe_timeout_seconds": timeout, "probe_page_closed": _page_closed(page)}

    try:
        context = page.context
        fields["probe_page_count"] = len(context.pages)
    except Exception:
        fields["probe_page_count"] = None
    try:
        fields["probe_frame_count"] = len(page.frames)
    except Exception:
        fields["probe_frame_count"] = None

    session: Any = None
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        session = await asyncio.wait_for(page.context.new_cdp_session(page), timeout=timeout)
        fields["probe_cdp_session"] = "ok"
    except TimeoutError:
        fields["probe_cdp_session"] = "timeout"
    except Exception as exc:
        fields["probe_cdp_session"] = f"error:{type(exc).__name__}"
    fields["probe_cdp_session_ms"] = round((loop.time() - started) * 1000, 1)
    if session is None:
        return fields

    try:
        # Browser-process command: answers even when the renderer is saturated. Target.
        # rather than Browser., which extension-backed browsers reject by allowlist -- a
        # rejected probe would be recorded as a dead endpoint on a healthy browser.
        outcome, elapsed = await _timed(session.send(BROWSER_PROBE_METHOD), timeout)
        fields["probe_browser_endpoint"] = outcome
        fields["probe_browser_endpoint_ms"] = round(elapsed, 1)

        # Renderer command: diverges from the above when only the renderer is stuck.
        outcome, elapsed = await _timed(session.send(RENDERER_PROBE_METHOD, {"expression": "1"}), timeout)
        fields["probe_renderer"] = outcome
        fields["probe_renderer_ms"] = round(elapsed, 1)

        metrics: dict[str, Any] = {}

        async def _metrics() -> None:
            result = await session.send(METRICS_PROBE_METHOD)
            for metric in result.get("metrics", []):
                if metric.get("name") in _METRICS_OF_INTEREST:
                    metrics[f"probe_metric_{metric['name']}"] = metric.get("value")

        outcome, elapsed = await _timed(_metrics(), timeout)
        fields["probe_metrics"] = outcome
        fields["probe_metrics_ms"] = round(elapsed, 1)
        fields.update(metrics)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.detach(), timeout=timeout)

    return fields


def schedule_control_endpoint_diagnostics(page: Any, message: str, **log_fields: Any) -> None:
    """Probe the control endpoint out of band and log the outcome under ``message``."""

    async def _run() -> None:
        try:
            fields = await collect_control_endpoint_diagnostics(page)
        except Exception:
            LOG.warning(message, probe_error="diagnostics_collection_failed", **log_fields)
            return
        LOG.warning(message, **log_fields, **fields)

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
