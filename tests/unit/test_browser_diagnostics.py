"""Control-endpoint probe behaviour for browser-invariant failures (SKY-14877 AC3)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page, async_playwright

from skyvern.browser_extension.protocol import is_cdp_method_allowed
from skyvern.webeye import browser_diagnostics, browser_health
from skyvern.webeye.browser_diagnostics import (
    _PENDING,
    PROBE_METHODS,
    PROCESS_INFO_METHOD,
    _driver_call,
    collect_control_endpoint_diagnostics,
    collect_degraded_browser_diagnostics,
    collect_page_memory_diagnostics,
    collect_renderer_os_cpu,
    schedule_control_endpoint_diagnostics,
)
from skyvern.webeye.main_world_eval import clear_main_world_prefix, configure_main_world_prefix


class _Session:
    """CDP session whose per-command behaviour the test dictates; a list answers successive calls, the last repeating."""

    def __init__(self, behaviours: dict[str, Any]) -> None:
        self._behaviours = behaviours
        self.detached = False
        self.calls: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(method)
        behaviour = self._behaviours.get(method)
        if isinstance(behaviour, list):
            behaviour = behaviour.pop(0) if len(behaviour) > 1 else behaviour[0]
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
async def test_a_driver_call_that_never_answers_is_cancelled_instead_of_retained_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out call is left running, so a driver that never answers would otherwise outlive its run."""
    monkeypatch.setattr(browser_diagnostics, "SETTLE_DEADLINE_SECONDS", 0.01)
    hung = asyncio.ensure_future(asyncio.Event().wait())

    with pytest.raises(TimeoutError):
        await _driver_call(hung, 0.01)
    assert hung in _PENDING

    await asyncio.wait({hung}, timeout=5)

    assert hung.cancelled()
    assert hung not in _PENDING


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


@pytest.mark.asyncio
async def test_a_persistent_context_launch_yields_a_renderer_cpu_reading(tmp_path: Path) -> None:
    """The cloud factory launches with launch_persistent_context. The reading needs the context's Browser to open a
    browser-level session, which a driver that returned None for a persistent context would turn into "unavailable"."""
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Playwright chromium is not installed")
        context = await playwright.chromium.launch_persistent_context(
            str(tmp_path), headless=True, args=["--use-mock-keychain", "--password-store=basic"]
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.set_content("<p>stalled</p>")

            reading = await collect_renderer_os_cpu(
                SimpleNamespace(browser_context=context),  # type: ignore[arg-type]
                window_seconds=0.05,
                step_timeout=5.0,
            )
        finally:
            await context.close()

    assert reading.fields["renderer_os_cpu"] == "process_info", reading.fields
    assert reading.fields["renderer_os_processes"] >= 1


def _process_info(cpu_seconds: dict[tuple[str, int], float]) -> dict[str, Any]:
    return {"processInfo": [{"type": kind, "id": pid, "cpuTime": cpu} for (kind, pid), cpu in cpu_seconds.items()]}


def _write_proc(root: Path, processes: dict[int, tuple[str, str, int, int]], cgroup_procs: Path | None) -> None:
    """A /proc with ``self`` in a private cgroup v2 namespace and, by pid, each process's comm, cgroup membership as the
    worker reads it, own CPU ticks (utime + stime) and its waited-for children's ticks (cutime + cstime). Membership
    goes to ``cgroup_procs`` when given, else to each /proc/<pid>/cgroup."""
    (root / "self").mkdir(parents=True, exist_ok=True)
    (root / "self" / "cgroup").write_text("0::/\n")
    for entry in root.iterdir():
        if entry.name.isdigit():
            for file in entry.iterdir():
                file.unlink()
            entry.rmdir()
    if cgroup_procs is not None:
        cgroup_procs.write_text("".join(f"{pid}\n" for pid, (_, cgroup, _, _) in processes.items() if cgroup == "0::/"))
    for pid, (comm, cgroup, own, reaped) in processes.items():
        (root / str(pid)).mkdir()
        if cgroup_procs is None:
            (root / str(pid) / "cgroup").write_text(f"{cgroup}\n")
        utime, stime, cutime, cstime = own - own // 4, own // 4, reaped - reaped // 2, reaped // 2
        # proc_pid_stat(5): pid (comm) state ppid pgrp session tty_nr tpgid flags minflt cminflt majflt cmajflt utime
        # stime cutime cstime priority nice num_threads itrealvalue starttime vsize rss ...
        (root / str(pid) / "stat").write_text(
            f"{pid} ({comm}) S 1 {pid} {pid} 0 -1 4194560 1200 0 3 0 {utime} {stime} {cutime} {cstime} 20 0 12 0 "
            "4821 1073741824 51200 18446744073709551615 1 1 0 0 0 0 0 4096 0 0 0 17 1 0 0 0 0 0\n"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed_by_cgroup_procs", "cdp_pid_of", "late_renderer_ticks", "browser_in_pod", "top_consumer"),
    [
        pytest.param(True, lambda kind, pid: pid, 0, True, "chromium_gpu", id="browser_in_pod"),
        pytest.param(False, lambda kind, pid: pid, 0, True, "chromium_gpu", id="cgroup_procs_unreadable"),
        # A browser reached over CDP on another host reports that host's pids, none of them this pod's.
        pytest.param(True, lambda kind, pid: pid + 5_000, 0, False, "chromium", id="browser_on_another_host"),
        # Pids are namespace-local, so that host's browser can share its pid with this pod's own Chromium.
        pytest.param(
            True,
            lambda kind, pid: pid if kind == "browser" else pid + 5_000,
            0,
            False,
            "chromium",
            id="browser_on_another_host_with_a_local_chromium_pid",
        ),
        # The readings disagree on a renderer that started between their first reads: /proc cannot attribute its CPU, so
        # the CDP breakdown must not be subtracted from the /proc Chromium total.
        pytest.param(
            True, lambda kind, pid: pid, 40, False, "chromium", id="renderer_started_after_the_first_proc_read"
        ),
    ],
)
async def test_a_guard_trip_names_cpu_by_chromium_process_type_and_by_container_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listed_by_cgroup_procs: bool,
    cdp_pid_of: Callable[[str, int], int],
    late_renderer_ticks: int,
    browser_in_pod: bool,
    top_consumer: str,
) -> None:
    """A trip can find the container at its CPU limit while the renderers use half a core, so these fields must name
    the rest. A process outside the container's cgroup, or present at only one read, must not count."""
    in_pod, other_container, base, worker = "0::/", "0::/../../datadog-agent", 10_000_000, os.getpid()
    # Cumulative ticks at the first read, then the ticks each process uses across the window.
    before = {
        base + 1: ("bash", in_pod, 40, 300),
        base + 8: ("Xvfb", in_pod, 9_000, 0),
        base + 15: ("xterm", in_pod, 120, 0),
        base + 17: ("python", in_pod, 4_000, 60_000),
        base + 20: ("x11vnc", in_pod, 500, 0),
        worker: ("python", in_pod, 30_000, 0),
        base + 300: ("node", in_pod, 12_000, 0),
        base + 310: ("chrome", in_pod, 20_000, 0),
        base + 311: ("chrome", in_pod, 50, 0),
        base + 312: ("chrome_crashpad", in_pod, 10, 0),
        base + 320: ("chrome", in_pod, 90_000, 0),
        base + 321: ("chrome", in_pod, 3_000, 0),
        base + 322: ("chrome", in_pod, 800, 0),
        base + 330: ("chrome", in_pod, 15_000, 0),
        base + 331: ("chrome", in_pod, 8_000, 0),
        base + 333: ("chrome", in_pod, 9_000, 0),
        base + 340: ("ffmpeg-linux", in_pod, 25_000, 0),
        base + 350: ("sh", in_pod, 2, 0),
        base + 900: ("agent", other_container, 70_000, 0),
    }
    used = {base + 8: 3, base + 17: 1, base + 20: 2, worker: 1, base + 300: 6, base + 310: 10, base + 320: 45}
    used |= {base + 321: 2, base + 330: 15, base + 331: 10, base + 340: 5, base + 900: 50}
    after = {pid: (comm, cgroup, own + used.get(pid, 0), reaped) for pid, (comm, cgroup, own, reaped) in before.items()}
    # The screenshot worker reaps a 4-tick capture, and the zygote a renderer that ran 4 more ticks before exiting:
    # that reap adds the renderer's whole 9,004-tick life to the zygote's children's time.
    after[base + 17] = (*after[base + 17][:3], 60_004)
    after[base + 311] = (*after[base + 311][:3], 9_004)
    del after[base + 333], after[base + 350]
    after[base + 360] = ("xwd", in_pod, 3, 0)
    if late_renderer_ticks:
        after[base + 334] = ("chrome", in_pod, late_renderer_ticks, 0)
    # The cgroup also counts the 12 in-window ticks of processes at only one read: 4 + 4 + 1 exited, 3 started. A late
    # renderer adds all of its own ticks.
    attributed_ticks, unattributed_ticks = 100, 12 + late_renderer_ticks

    proc, cgroup_procs = tmp_path / "proc", tmp_path / "cgroup.procs"
    listing = cgroup_procs if listed_by_cgroup_procs else None
    _write_proc(proc, before, listing)
    monkeypatch.setattr(browser_diagnostics, "PROC_PATH", proc)
    monkeypatch.setattr(browser_health, "CGROUP_PROCS_PATH", cgroup_procs)
    seconds_per_tick = 1 / os.sysconf("SC_CLK_TCK")
    cgroup_usage_usec = [9_000_000, 9_000_000 + round((attributed_ticks + unattributed_ticks) * seconds_per_tick * 1e6)]

    def read_cgroup_counters() -> dict[str, int]:
        # Read last in each snapshot, so moving /proc on here puts the second snapshot across the window.
        _write_proc(proc, after, listing)
        return {"usage_usec": cgroup_usage_usec.pop(0)}

    monkeypatch.setattr(browser_diagnostics, "read_container_cpu_counters", read_cgroup_counters)
    chromium_before = {
        ("browser", 310): 200.0,
        ("GPU", 320): 900.0,
        ("network.mojom.NetworkService", 321): 30.0,
        ("storage.mojom.StorageService", 322): 8.0,
        ("renderer", 330): 150.0,
        ("renderer", 331): 80.0,
    }
    chromium_after = chromium_before | {
        ("browser", 310): 200.1,
        ("GPU", 320): 900.45,
        ("network.mojom.NetworkService", 321): 30.02,
        ("renderer", 330): 150.15,
        ("renderer", 331): 80.1,
        ("renderer", 332): 0.3,
    }
    if late_renderer_ticks:
        chromium_before[("renderer", 334)] = 5.0
        chromium_after[("renderer", 334)] = 5.0 + late_renderer_ticks * seconds_per_tick
    payloads = [
        _process_info({(kind, cdp_pid_of(kind, base + pid)): cpu for (kind, pid), cpu in reading.items()})
        for reading in (chromium_before, chromium_after)
    ]
    session = _Session({PROCESS_INFO_METHOD: payloads})
    browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=None),
        browser_context=SimpleNamespace(
            browser=SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=session))
        ),
    )

    fields = await collect_degraded_browser_diagnostics(browser_state)  # type: ignore[arg-type]

    assert fields["renderer_os_cpu"] == "process_info" and fields["process_cpu"] == "ok", fields
    assert fields["renderer_os_processes"] == 2 + bool(late_renderer_ticks)
    assert fields["renderer_os_cpu_seconds"] == pytest.approx(0.25 + late_renderer_ticks * seconds_per_tick, abs=0.001)
    cdp_window = fields["renderer_os_cpu_seconds"] / fields["renderer_os_cpu_share"]
    assert {
        "browser": fields["chromium_browser_cpu_share"],
        "gpu": fields["chromium_gpu_cpu_share"],
        "utility": fields["chromium_utility_cpu_share"],
    } == pytest.approx({"browser": 0.1 / cdp_window, "gpu": 0.45 / cdp_window, "utility": 0.02 / cdp_window}, abs=0.01)
    assert fields["chromium_utility_top_type"] == "network.mojom.NetworkService"

    proc_window = fields["process_cpu_window_seconds"]
    ticks_by_group = {
        "chromium": 82,
        "node_driver": 6,
        "xvfb": 3,
        "ffmpeg": 5,
        "vnc": 2,
        "worker": 1,
        "other_python": 1,
        "other": 0,
        "unattributed": unattributed_ticks,
    }
    assert {group: fields[f"process_{group}_cpu_share"] for group in ticks_by_group} == pytest.approx(
        {group: ticks * seconds_per_tick / proc_window for group, ticks in ticks_by_group.items()}, abs=0.01
    )
    assert fields["process_cpu_processes"] == 16
    assert fields["process_cpu_cgroup_share"] == pytest.approx(
        (attributed_ticks + unattributed_ticks) * seconds_per_tick / proc_window, abs=0.01
    )
    assert fields["chromium_browser_in_pod"] is browser_in_pod
    assert fields["cpu_top_consumer"] == top_consumer
