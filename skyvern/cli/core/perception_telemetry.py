from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

PerceptionSnapshotCategory = Literal["model_visible", "automatic", "stale_ref_refresh"]


@dataclass
class MCPPerceptionCounters:
    top_level_mcp_calls: int = 1
    perception_snapshots: int = 0
    model_visible_observe_results: int = 0
    automatic_observe_snapshots: int = 0
    stale_ref_refresh_snapshots: int = 0
    failed_perception_probes: int = 0
    evaluate_page_scans: int = 0
    browser_perception_wall_ms: int = 0
    _last_failed_probe: BaseException | None = None

    def event_fields(self) -> dict[str, int]:
        return {
            "top_level_mcp_calls": self.top_level_mcp_calls,
            "perception_snapshots": self.perception_snapshots,
            "model_visible_observe_results": self.model_visible_observe_results,
            "automatic_observe_snapshots": self.automatic_observe_snapshots,
            "stale_ref_refresh_snapshots": self.stale_ref_refresh_snapshots,
            "failed_perception_probes": self.failed_perception_probes,
            "evaluate_page_scans": self.evaluate_page_scans,
            "browser_perception_wall_ms": self.browser_perception_wall_ms,
        }


_perception_counters: ContextVar[MCPPerceptionCounters | None] = ContextVar(
    "mcp_perception_counters",
    default=None,
)
_perception_timing_depth: ContextVar[int] = ContextVar("mcp_perception_timing_depth", default=0)


def _failure_already_counted(counters: MCPPerceptionCounters, error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if current is counters._last_failed_probe:
            return True
        current = current.__cause__ or current.__context__
    return False


@contextmanager
def perception_counters_scope() -> Iterator[MCPPerceptionCounters]:
    counters = MCPPerceptionCounters()
    counters_token = _perception_counters.set(counters)
    depth_token = _perception_timing_depth.set(0)
    try:
        yield counters
    finally:
        _perception_timing_depth.reset(depth_token)
        _perception_counters.reset(counters_token)


@asynccontextmanager
async def track_perception_snapshot(category: PerceptionSnapshotCategory) -> AsyncIterator[None]:
    """Account for one do_observe attempt without changing its result or errors."""
    counters = _perception_counters.get()
    if counters is None:
        yield
        return

    counters.perception_snapshots += 1
    if category == "model_visible":
        counters.model_visible_observe_results += 1
    elif category == "automatic":
        counters.automatic_observe_snapshots += 1
    else:
        counters.stale_ref_refresh_snapshots += 1

    failed_before = counters.failed_perception_probes
    depth = _perception_timing_depth.get()
    depth_token = _perception_timing_depth.set(depth + 1)
    start = time.perf_counter() if depth == 0 else None
    try:
        yield
    except (Exception, asyncio.CancelledError) as exc:
        if counters.failed_perception_probes == failed_before or not _failure_already_counted(counters, exc):
            counters.failed_perception_probes += 1
        counters._last_failed_probe = exc
        raise
    finally:
        if start is not None:
            counters.browser_perception_wall_ms += int((time.perf_counter() - start) * 1000)
        _perception_timing_depth.reset(depth_token)


@asynccontextmanager
async def track_perception_probe(*, evaluate_page_scan: bool = False) -> AsyncIterator[None]:
    """Account for a non-snapshot browser perception probe."""
    counters = _perception_counters.get()
    if counters is None:
        yield
        return

    if evaluate_page_scan:
        counters.evaluate_page_scans += 1
    depth = _perception_timing_depth.get()
    depth_token = _perception_timing_depth.set(depth + 1)
    start = time.perf_counter() if depth == 0 else None
    try:
        yield
    except (Exception, asyncio.CancelledError) as exc:
        counters.failed_perception_probes += 1
        counters._last_failed_probe = exc
        raise
    finally:
        if start is not None:
            counters.browser_perception_wall_ms += int((time.perf_counter() - start) * 1000)
        _perception_timing_depth.reset(depth_token)
