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
import os
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

import structlog

from skyvern.webeye.browser_health import in_container_cgroup, read_container_cpu_counters, read_container_pids
from skyvern.webeye.browser_object_predicates import is_page_like
from skyvern.webeye.main_world_eval import evaluate_in_main_world, get_main_world_prefix

if TYPE_CHECKING:
    from playwright.async_api import Browser

    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

PROBE_TIMEOUT_SECONDS = 2.0
DEGRADED_BROWSER_DIAGNOSTICS_TIMEOUT_SECONDS = 2.0
RENDERER_LOAD_WINDOW_SECONDS = 0.5
# A driver call that outlives its caller is cancelled this long after the caller gave up. Teardown of the browser
# normally settles it first, with an error that _settle_in_background retrieves.
SETTLE_DEADLINE_SECONDS = 60.0
_RENDERER_LOAD_STEP_TIMEOUT_SECONDS = 0.3
# The durations and ThreadTime count from Performance.enable, ProcessTime from renderer start; each field is the
# difference of two reads, so both origins cancel. TaskDuration is the main thread's wall time in tasks, ThreadTime
# its CPU time, ProcessTime the whole renderer's.
_RENDERER_LOAD_DELTAS = {
    "TaskDuration": "renderer_task_seconds",
    "ScriptDuration": "renderer_script_seconds",
    "LayoutDuration": "renderer_layout_seconds",
    "ThreadTime": "renderer_main_thread_cpu_seconds",
    "ProcessTime": "renderer_process_cpu_seconds",
}

# Browser-process command, then renderer, then metrics. Declared here so the
# allowlist guard checks the contract rather than how the call is spelled.
BROWSER_PROBE_METHOD = "Target.getTargets"
RENDERER_PROBE_METHOD = "Runtime.evaluate"
METRICS_PROBE_METHOD = "Performance.getMetrics"
# getMetrics answers an empty list on a session that has not enabled the domain.
METRICS_ENABLE_METHOD = "Performance.enable"
METRICS_DISABLE_METHOD = "Performance.disable"
PROBE_METHODS = (
    BROWSER_PROBE_METHOD,
    RENDERER_PROBE_METHOD,
    METRICS_ENABLE_METHOD,
    METRICS_PROBE_METHOD,
    METRICS_DISABLE_METHOD,
)
# Renderer CPU of one browser, read on a browser-level session. Kept out of PROBE_METHODS: extension-backed browsers
# reject SystemInfo, which only costs the reading (renderer_os_cpu=process_info_error:*).
PROCESS_INFO_METHOD = "SystemInfo.getProcessInfo"
# getProcessInfo names the GPU process "GPU" and each utility process by the service it hosts, such as
# "network.mojom.NetworkService", so every type not listed here is a utility.
_CHROMIUM_PROCESS_TYPE_GROUPS = {"browser": "browser", "renderer": "renderer", "GPU": "gpu"}

PROC_PATH = Path("/proc")
# Lowercased process names of the Chromium builds the workers launch. Chromium never renames a process's main thread,
# so every one of its processes reads as the binary's name.
CHROMIUM_PROCESS_NAME_PREFIXES = ("chrome", "chromium", "headless_shell", "msedge")
# Groups by the start of a lowercased /proc comm once the worker's own pid is matched; Playwright's recorder is
# ffmpeg-linux.
_PROCESS_GROUP_BY_COMM_PREFIX = (
    *((prefix, "chromium") for prefix in CHROMIUM_PROCESS_NAME_PREFIXES),
    ("node", "node_driver"),
    ("xvfb", "xvfb"),
    ("ffmpeg", "ffmpeg"),
    ("x11vnc", "vnc"),
    ("websockify", "vnc"),
    ("python", "other_python"),
)
PROCESS_GROUPS = ("chromium", "node_driver", "xvfb", "ffmpeg", "vnc", "worker", "other_python", "other")
# One thread, so a /proc read stuck in the kernel parks at most one thread; later reads queue behind it and time out.
_PROCESS_CPU_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="process-cpu")
# Candidates for cpu_top_consumer from the CDP reading; they replace the /proc "chromium" group when the browser runs
# in this container.
_CHROMIUM_SHARE_FIELDS = {
    "chromium_browser": "chromium_browser_cpu_share",
    "chromium_gpu": "chromium_gpu_cpu_share",
    "chromium_utility": "chromium_utility_cpu_share",
    "chromium_renderer": "renderer_os_cpu_share",
}

_T = TypeVar("_T")


class DiagnosticsReading(NamedTuple):
    fields: dict[str, Any]
    # The processes present at both reads: the CDP reading's, or the /proc reading's Chromium ones.
    pids: frozenset[int] = frozenset()


_METRICS_OF_INTEREST = frozenset(
    {
        "Documents",
        "Frames",
        "JSEventListeners",
        "JSHeapUsedSize",
        "JSHeapTotalSize",
        "LayoutCount",
        "Nodes",
        "TaskDuration",
    }
)

# Reads the optional observer-stats helper only when the page already exposes it; nothing is injected,
# since injecting under memory pressure would add to it. A missing helper reports observer_probe=absent
# with no observer fields; a helper missing any stat reports "partial" and omits the unreported stats.
OBSERVER_STATS_EXPRESSION = (
    "() => typeof globalThis.readIncrementalObserverStats === 'function'"
    " ? globalThis.readIncrementalObserverStats() : null"
)
_OBSERVER_STATS_KEYS = ("listening", "jobs", "pending", "depth_buckets", "retained_nodes", "parsed", "version")

# Fire-and-forget tasks are strongly referenced until they finish; the event loop
# only holds weak references and would otherwise collect them mid-flight.
_PENDING: set[asyncio.Future[Any]] = set()


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
            await session.send(METRICS_ENABLE_METHOD)
            result = await session.send(METRICS_PROBE_METHOD)
            for metric in result.get("metrics", []):
                if metric.get("name") in _METRICS_OF_INTEREST:
                    metrics[f"probe_metric_{metric['name']}"] = metric.get("value")

        try:
            outcome, elapsed = await _timed(_metrics(), timeout)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.send(METRICS_DISABLE_METHOD), timeout=timeout)
        fields["probe_metrics"] = outcome
        fields["probe_metrics_ms"] = round(elapsed, 1)
        fields.update(metrics)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.detach(), timeout=timeout)

    return fields


async def collect_page_memory_diagnostics(
    page: Any, observed_frame: Any, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Control-endpoint probe plus the incremental observer's scalar counters. Never raises."""
    fields: dict[str, Any] = {}
    if page is not None:
        fields.update(await collect_control_endpoint_diagnostics(page, timeout=timeout))

    target = observed_frame if observed_frame is not None else page
    if target is None:
        fields["observer_probe"] = "no_target"
        return fields
    stats: Any = None

    async def _read() -> None:
        nonlocal stats
        # Prefix-configured contexts install the helpers in the main world, which a plain evaluate cannot see.
        if is_page_like(target) and target.context is not None and get_main_world_prefix(target.context) is not None:
            stats = await evaluate_in_main_world(target, OBSERVER_STATS_EXPRESSION)
        else:
            stats = await target.evaluate(OBSERVER_STATS_EXPRESSION)

    outcome, elapsed = await _timed(_read(), timeout)
    fields["observer_probe_ms"] = round(elapsed, 1)
    if outcome == "ok" and not isinstance(stats, dict):
        outcome = "absent"
    if isinstance(stats, dict):
        for key in _OBSERVER_STATS_KEYS:
            value = stats.get(key)
            if key in stats and (value is None or isinstance(value, (bool, int, float))):
                fields[f"observer_{key}"] = value
            elif outcome == "ok":
                outcome = "partial"
    fields["observer_probe"] = outcome
    return fields


def _metric_values(result: Any) -> dict[str, float]:
    return {
        metric["name"]: metric["value"]
        for metric in result.get("metrics", [])
        if isinstance(metric.get("value"), (int, float))
    }


async def collect_renderer_load(
    page: Any,
    *,
    window_seconds: float = RENDERER_LOAD_WINDOW_SECONDS,
    step_timeout: float = _RENDERER_LOAD_STEP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read Performance.getMetrics twice across ``window_seconds`` on a fresh page session; never raises.
    ``renderer_metrics=enable_timeout`` means the page renderer's main thread took no task within ``step_timeout``."""
    loop = asyncio.get_running_loop()
    session: Any = None
    stage = "session"
    try:
        session = await _driver_call(page.context.new_cdp_session(page), step_timeout, on_late_result=_schedule_detach)
        stage = "enable"
        await _driver_call(session.send(METRICS_ENABLE_METHOD), step_timeout)
        stage = "read"
        before = _metric_values(await _driver_call(session.send(METRICS_PROBE_METHOD), step_timeout))
        read_before_at = loop.time()
        await asyncio.sleep(window_seconds)
        after = _metric_values(await _driver_call(session.send(METRICS_PROBE_METHOD), step_timeout))
        measured_window = loop.time() - read_before_at
    except TimeoutError:
        return {"renderer_metrics": f"{stage}_timeout"}
    except Exception as exc:
        return {"renderer_metrics": f"{stage}_error:{type(exc).__name__}"}
    finally:
        if session is not None:
            _schedule_detach(session)

    fields: dict[str, Any] = {"renderer_metrics": "ok", "renderer_metrics_window_seconds": round(measured_window, 3)}
    for metric, field_name in _RENDERER_LOAD_DELTAS.items():
        if metric in before and metric in after:
            fields[field_name] = round(after[metric] - before[metric], 3)
    if "renderer_task_seconds" in fields:
        fields["renderer_task_share"] = round(fields["renderer_task_seconds"] / measured_window, 3)
    if "renderer_main_thread_cpu_seconds" in fields:
        fields["renderer_main_thread_cpu_share"] = round(
            fields["renderer_main_thread_cpu_seconds"] / measured_window, 3
        )
    if "JSHeapUsedSize" in after:
        fields["renderer_js_heap_used_bytes"] = int(after["JSHeapUsedSize"])
    return fields


def _process_info_cpu(result: Any) -> dict[int, tuple[str, float]]:
    return {
        process["id"]: (str(process.get("type")), process["cpuTime"])
        for process in result.get("processInfo", [])
        if isinstance(process.get("cpuTime"), (int, float))
    }


def _cpu_by_group(
    before: dict[int, tuple[str, float]], after: dict[int, tuple[str, float]]
) -> dict[str, tuple[int, float]]:
    """Process count and CPU seconds by group between two reads keyed by pid. Only processes present at both reads
    count, so one that started or exited between them cannot skew the sum."""
    by_group: dict[str, tuple[int, float]] = {}
    for pid in before.keys() & after.keys():
        group, cpu_seconds = after[pid]
        count, total = by_group.get(group, (0, 0.0))
        by_group[group] = (count + 1, total + cpu_seconds - before[pid][1])
    return by_group


async def _read_again_after(read: Callable[[], Awaitable[_T]], window_seconds: float) -> tuple[_T, float]:
    """The second read of a sampling window, and the window measured from the end of the first read."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(window_seconds)
    after = await read()
    return after, loop.time() - started


def _process_info_fields(by_type: dict[str, tuple[int, float]], window_seconds: float) -> dict[str, Any]:
    renderer_processes, renderer_seconds = by_type.get("renderer", (0, 0.0))
    fields: dict[str, Any] = {
        "renderer_os_processes": renderer_processes,
        "renderer_os_cpu_seconds": round(renderer_seconds, 3),
        "renderer_os_cpu_share": round(renderer_seconds / window_seconds, 3),
    }
    group_seconds = dict.fromkeys(("browser", "gpu", "utility"), 0.0)
    utility_seconds: dict[str, float] = {}
    for process_type, (_, seconds) in by_type.items():
        group = _CHROMIUM_PROCESS_TYPE_GROUPS.get(process_type, "utility")
        if group == "utility":
            utility_seconds[process_type] = seconds
        if group in group_seconds:
            group_seconds[group] += seconds
    for group, seconds in group_seconds.items():
        fields[f"chromium_{group}_cpu_share"] = round(seconds / window_seconds, 3)
    top_utility = max(utility_seconds, key=utility_seconds.__getitem__, default=None)
    if top_utility is not None and round(utility_seconds[top_utility] / window_seconds, 3) > 0:
        fields["chromium_utility_top_type"] = top_utility
    return fields


async def _collect_process_info_cpu(browser: Browser, window_seconds: float, step_timeout: float) -> DiagnosticsReading:
    session: Any = None

    async def read() -> dict[int, tuple[str, float]]:
        return _process_info_cpu(await _driver_call(session.send(PROCESS_INFO_METHOD), step_timeout))

    try:
        session = await _driver_call(browser.new_browser_cdp_session(), step_timeout, on_late_result=_schedule_detach)
        before = await read()
        after, window = await _read_again_after(read, window_seconds)
    except TimeoutError:
        return DiagnosticsReading({"renderer_os_cpu": "process_info_timeout"})
    except Exception as exc:
        return DiagnosticsReading({"renderer_os_cpu": f"process_info_error:{type(exc).__name__}"})
    finally:
        if session is not None:
            _schedule_detach(session)
    fields = {"renderer_os_cpu": "process_info", **_process_info_fields(_cpu_by_group(before, after), window)}
    return DiagnosticsReading(fields, frozenset(before.keys() & after.keys()))


async def collect_renderer_os_cpu(
    browser_state: BrowserState,
    *,
    window_seconds: float = RENDERER_LOAD_WINDOW_SECONDS,
    step_timeout: float = _RENDERER_LOAD_STEP_TIMEOUT_SECONDS,
) -> DiagnosticsReading:
    """CPU of this run's browser's renderer processes, cross-origin iframe ones included, and of its browser, GPU and
    utility processes, across ``window_seconds``, read from outside the renderers so a main thread too busy to answer
    CDP still gets a reading. A context with no Browser object cannot open a browser-level session and reads
    ``unavailable``; persistent contexts carry one on the pinned drivers. Never raises."""
    browser_context = browser_state.browser_context
    if browser_context is None or browser_context.browser is None:
        return DiagnosticsReading({"renderer_os_cpu": "unavailable"})
    return await _collect_process_info_cpu(browser_context.browser, window_seconds, step_timeout)


class _ContainerCpuSnapshot(NamedTuple):
    # By pid: the process's group and its own CPU seconds.
    own: dict[int, tuple[str, float]]
    cgroup_usage_usec: int | None


def _process_group(pid: int, comm: str) -> str:
    if pid == os.getpid():
        return "worker"
    name = comm.lower()
    return next((group for prefix, group in _PROCESS_GROUP_BY_COMM_PREFIX if name.startswith(prefix)), "other")


def _read_container_process_cpu() -> _ContainerCpuSnapshot | None:
    """Cumulative CPU of each process in this container's cgroup, from ``/proc/<pid>/stat``, and the cgroup's usage.
    None outside a private cgroup v2 namespace, as for the container_* stall fields."""
    if not in_container_cgroup(PROC_PATH / "self" / "cgroup"):
        return None
    pids = read_container_pids()
    if pids is None:
        # Each /proc/<pid>/cgroup read takes the kernel's global cgroup_mutex, so this is only the fallback.
        pids = frozenset(
            int(entry.name)
            for entry in PROC_PATH.iterdir()
            if entry.name.isdigit() and in_container_cgroup(entry / "cgroup")
        )
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    own: dict[int, tuple[str, float]] = {}
    for pid in pids:
        try:
            stat = (PROC_PATH / str(pid) / "stat").read_text()
            # comm can hold spaces and parentheses, so the fixed fields start after its last ")". utime and stime are
            # fields 14 and 15 of proc_pid_stat(5).
            utime, stime = (int(value) for value in stat[stat.rindex(")") + 2 :].split()[11:13])
        except (OSError, ValueError):
            continue
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        own[pid] = (_process_group(pid, comm), (utime + stime) / ticks_per_second)
    return _ContainerCpuSnapshot(own, read_container_cpu_counters().get("usage_usec"))


async def collect_container_process_cpu(
    *,
    window_seconds: float = RENDERER_LOAD_WINDOW_SECONDS,
    step_timeout: float = _RENDERER_LOAD_STEP_TIMEOUT_SECONDS,
) -> DiagnosticsReading:
    """CPU across ``window_seconds`` of each process group in this container's cgroup and of the cgroup itself, read
    from /proc on one dedicated thread. Never raises; ``process_cpu=unavailable`` outside a private cgroup v2
    namespace."""
    loop = asyncio.get_running_loop()

    async def read() -> _ContainerCpuSnapshot | None:
        return await asyncio.wait_for(
            loop.run_in_executor(_PROCESS_CPU_EXECUTOR, _read_container_process_cpu), step_timeout
        )

    try:
        before = await read()
        if before is None:
            return DiagnosticsReading({"process_cpu": "unavailable"})
        after, window = await _read_again_after(read, window_seconds)
    except TimeoutError:
        return DiagnosticsReading({"process_cpu": "timeout"})
    except Exception as exc:
        return DiagnosticsReading({"process_cpu": f"error:{type(exc).__name__}"})
    if after is None:
        return DiagnosticsReading({"process_cpu": "unavailable"})
    by_group = _cpu_by_group(before.own, after.own)
    fields: dict[str, Any] = {
        "process_cpu": "ok",
        "process_cpu_window_seconds": round(window, 3),
        "process_cpu_processes": sum(count for count, _ in by_group.values()),
    }
    for group in PROCESS_GROUPS:
        fields[f"process_{group}_cpu_share"] = round(by_group.get(group, (0, 0.0))[1] / window, 3)
    if before.cgroup_usage_usec is not None and after.cgroup_usage_usec is not None:
        cgroup_seconds = (after.cgroup_usage_usec - before.cgroup_usage_usec) / 1_000_000
        attributed_seconds = sum(seconds for _, seconds in by_group.values())
        fields["process_cpu_cgroup_share"] = round(cgroup_seconds / window, 3)
        # The cgroup counts the in-window CPU of processes that started or exited inside the window; the per-process
        # shares cannot, since each needs both reads.
        fields["process_unattributed_cpu_share"] = round((cgroup_seconds - attributed_seconds) / window, 3)
    chromium_pids = frozenset(pid for pid in before.own.keys() & after.own.keys() if after.own[pid][0] == "chromium")
    return DiagnosticsReading(fields, chromium_pids)


def _top_cpu_consumer(fields: dict[str, Any], *, browser_in_pod: bool) -> str | None:
    """The consumer with the largest share in the /proc sample. When this run's browser is in the pod, Chromium's own
    breakdown names its processes, and ``chromium_other`` is the Chromium share it does not explain."""
    if fields.get("process_cpu") != "ok":
        return None
    shares: dict[str, float] = {group: fields[f"process_{group}_cpu_share"] for group in PROCESS_GROUPS}
    if "process_unattributed_cpu_share" in fields:
        shares["unattributed"] = fields["process_unattributed_cpu_share"]
    if browser_in_pod:
        chromium = {consumer: fields[name] for consumer, name in _CHROMIUM_SHARE_FIELDS.items()}
        shares["chromium_other"] = shares.pop("chromium") - sum(chromium.values())
        shares |= chromium
    top = max(shares, key=shares.__getitem__)
    return top if shares[top] > 0 else None


async def _collect_working_page_renderer_load(browser_state: BrowserState) -> DiagnosticsReading:
    try:
        page = await asyncio.wait_for(
            browser_state.get_working_page(prune_excess_pages=False), _RENDERER_LOAD_STEP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return DiagnosticsReading({"renderer_metrics": "page_timeout"})
    except Exception as exc:
        return DiagnosticsReading({"renderer_metrics": f"page_error:{type(exc).__name__}"})
    if page is None:
        return DiagnosticsReading({"renderer_metrics": "no_page"})
    return DiagnosticsReading(await collect_renderer_load(page))


async def collect_degraded_browser_diagnostics(browser_state: BrowserState) -> dict[str, Any]:
    """Renderer load read over CDP, the browser's processes by type, and the container's processes from /proc, side by
    side within a hard budget; a reading still running at the deadline is ``timeout``. Always sets
    ``renderer_metrics``. Raises only a cancel."""
    fields: dict[str, Any] = {}
    pids: dict[str, frozenset[int]] = {}
    collectors: dict[str, asyncio.Task[DiagnosticsReading]] = {}
    try:
        collectors["renderer_metrics"] = asyncio.ensure_future(_collect_working_page_renderer_load(browser_state))
        collectors["renderer_os_cpu"] = asyncio.ensure_future(collect_renderer_os_cpu(browser_state))
        collectors["process_cpu"] = asyncio.ensure_future(collect_container_process_cpu())
        done, _ = await asyncio.wait(collectors.values(), timeout=DEGRADED_BROWSER_DIAGNOSTICS_TIMEOUT_SECONDS)
        for status_field, task in collectors.items():
            if task not in done:
                fields[status_field] = "timeout"
            elif task.cancelled():
                fields[status_field] = "cancelled"
            elif (error := task.exception()) is not None:
                fields[status_field] = f"error:{type(error).__name__}"
            else:
                reading = task.result()
                fields.update(reading.fields)
                pids[status_field] = reading.pids
        # Pids are namespace-local, so a browser on another host can share its pid with an unrelated local Chromium
        # process; it is in this pod only if every process it reports at both reads is a Chromium process here.
        cdp_pids = pids.get("renderer_os_cpu", frozenset())
        if cdp_pids and fields.get("process_cpu") == "ok":
            fields["chromium_browser_in_pod"] = cdp_pids <= pids["process_cpu"]
        top_consumer = _top_cpu_consumer(fields, browser_in_pod=fields.get("chromium_browser_in_pod") is True)
        if top_consumer is not None:
            fields["cpu_top_consumer"] = top_consumer
    except Exception as exc:
        fields.setdefault("renderer_metrics", f"error:{type(exc).__name__}")
    finally:
        for task in collectors.values():
            if not task.done():
                task.cancel()
                _PENDING.add(task)
                task.add_done_callback(_PENDING.discard)
    return fields


async def _driver_call(
    call: Awaitable[Any], timeout: float, *, on_late_result: Callable[[Any], None] | None = None
) -> Any:
    """``wait_for`` that leaves a timed-out driver call to settle in the background instead of cancelling it.
    Playwright never reads a cancelled call's reply future, so its exception at browser close would be logged as
    never retrieved; a call that succeeds late, such as a new CDP session, goes to ``on_late_result``."""
    task = asyncio.ensure_future(call)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except BaseException:
        _settle_in_background(task, on_late_result)
        raise


def _settle_in_background(task: asyncio.Future[Any], on_late_result: Callable[[Any], None] | None) -> None:
    def _settled(done: asyncio.Future[Any]) -> None:
        deadline.cancel()
        _PENDING.discard(done)
        if done.cancelled() or done.exception() is not None:
            return
        if on_late_result is not None:
            on_late_result(done.result())

    # Cancelling the task that issued the call is the one cancel Playwright abandons the reply for, so a call
    # that never answers stops being retained without leaving an unretrieved future behind.
    deadline = asyncio.get_running_loop().call_later(SETTLE_DEADLINE_SECONDS, task.cancel)
    _PENDING.add(task)
    task.add_done_callback(_settled)


def _schedule_detach(session: Any) -> None:
    async def _detach() -> None:
        with contextlib.suppress(Exception):
            await _driver_call(session.detach(), PROBE_TIMEOUT_SECONDS)

    try:
        task = asyncio.get_running_loop().create_task(_detach())
    except RuntimeError:
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


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
