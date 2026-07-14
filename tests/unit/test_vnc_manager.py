from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any

import pytest

from skyvern.webeye import vnc_manager as vnc_manager_module
from skyvern.webeye.vnc_manager import VncManager, VncStartupError, VncTeardownError


class FakeProcess:
    def __init__(
        self,
        command: Sequence[str],
        events: list[str],
        *,
        returncode: int | None = None,
    ) -> None:
        self.command = list(command)
        self.events = events
        self.returncode = returncode
        self.wait_timeouts: list[float | None] = []
        self.stop_fails = False

    @property
    def name(self) -> str:
        return self.command[0]

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        if self.stop_fails:
            raise OSError(f"terminate failed for {self.name}")
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return self.returncode or 0

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        if self.stop_fails:
            raise OSError(f"kill failed for {self.name}")
        self.returncode = -9


class ProcessFactory:
    def __init__(self, returncodes: list[int | None] | None = None) -> None:
        self.events: list[str] = []
        self.processes: list[FakeProcess] = []
        self.returncodes = list(returncodes or [])

    def __call__(self, command: Sequence[str], **_: Any) -> FakeProcess:
        returncode = self.returncodes.pop(0) if self.returncodes else None
        process = FakeProcess(command, self.events, returncode=returncode)
        self.processes.append(process)
        return process


@pytest.fixture(autouse=True)
def reset_vnc_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    VncManager._lock = asyncio.Lock()
    VncManager._instances = {}
    VncManager._used_displays = set()
    VncManager._used_ports = set()
    monkeypatch.setattr(vnc_manager_module.settings, "SKYVERN_DEFAULT_DISPLAY", 99)
    monkeypatch.setattr(vnc_manager_module.settings, "SKYVERN_BROWSER_VNC_PORT", 6080)
    monkeypatch.setattr(vnc_manager_module.settings, "BROWSER_WIDTH", 1280)
    monkeypatch.setattr(vnc_manager_module.settings, "BROWSER_HEIGHT", 720)
    monkeypatch.setattr(vnc_manager_module, "_is_tcp_port_available", lambda _port: True)
    monkeypatch.setattr(vnc_manager_module, "_display_is_ready", lambda _display: True)
    monkeypatch.setattr(vnc_manager_module, "_display_is_occupied", lambda _display: False)
    monkeypatch.setattr(vnc_manager_module, "_port_is_ready", lambda _port: True)
    monkeypatch.setattr(vnc_manager_module, "VNC_CHILD_STABILITY_SECONDS", 0.0, raising=False)


@pytest.mark.asyncio
async def test_successful_start_is_idempotent_and_tears_down_in_reverse_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    monkeypatch.delenv("DISPLAY", raising=False)

    assigned = await VncManager.start_vnc_for_session("pbs_1", organization_id="org_1")
    duplicate = await VncManager.start_vnc_for_session("pbs_1", organization_id="org_1")

    assert assigned == (100, 6080)
    assert duplicate == assigned
    assert len(process_factory.processes) == 3
    assert os.environ.get("DISPLAY") is None
    assert VncManager.owns_ready_stack(
        "pbs_1",
        organization_id="org_1",
        display_number=100,
        vnc_port=6080,
    )
    assert not VncManager.owns_ready_stack(
        "pbs_1",
        organization_id="org_other",
        display_number=100,
        vnc_port=6080,
    )
    assert not VncManager.owns_ready_stack(
        "pbs_1",
        organization_id="org_1",
        display_number=101,
        vnc_port=6080,
    )
    assert not VncManager.owns_ready_stack(
        "pbs_1",
        organization_id="org_1",
        display_number=100,
        vnc_port=6081,
    )

    xvfb, x11vnc, websockify = process_factory.processes
    assert xvfb.command[:2] == ["Xvfb", ":100"]
    assert xvfb.command[-2:] == ["-nolisten", "tcp"]
    assert ["-listen", "127.0.0.1"] == x11vnc.command[-2:]
    assert "-nopw" in x11vnc.command
    assert "--web=/usr/share/novnc" not in websockify.command
    assert websockify.command[-2:] == ["127.0.0.1:6080", "127.0.0.1:5900"]

    await VncManager.stop_vnc_for_session("pbs_1", organization_id="org_1")

    assert process_factory.events == ["terminate:websockify", "terminate:x11vnc", "terminate:Xvfb"]
    assert not VncManager.has_session("pbs_1")
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_ownership_requires_a_healthy_ready_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    await VncManager.start_vnc_for_session("pbs_owned", organization_id="org_1")

    process_factory.processes[2].returncode = 1

    assert not VncManager.owns_ready_stack(
        "pbs_owned",
        organization_id="org_1",
        display_number=100,
        vnc_port=6080,
    )
    assert not VncManager.owns_ready_stack(
        "pbs_missing",
        organization_id="org_1",
        display_number=100,
        vnc_port=6080,
    )

    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_child_failure_cleans_started_processes_and_releases_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory(returncodes=[None, 1])
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)

    with pytest.raises(VncStartupError, match="x11vnc exited"):
        await VncManager.start_vnc_for_session("pbs_failed", organization_id="org_1")

    assert [process.name for process in process_factory.processes] == ["Xvfb", "x11vnc"]
    assert process_factory.events == ["terminate:Xvfb"]
    assert not VncManager.has_session("pbs_failed")
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_externally_occupied_base_ports_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    monkeypatch.setattr(
        vnc_manager_module,
        "_is_tcp_port_available",
        lambda port: port not in {5900, 6080},
    )

    display_number, websocket_port = await VncManager.start_vnc_for_session("pbs_ports", organization_id="org_1")

    assert (display_number, websocket_port) == (100, 6081)
    x11vnc = process_factory.processes[1]
    websockify = process_factory.processes[2]
    assert x11vnc.command[x11vnc.command.index("-rfbport") + 1] == "5901"
    assert websockify.command[-2:] == ["127.0.0.1:6081", "127.0.0.1:5901"]
    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_externally_occupied_base_display_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    monkeypatch.setattr(
        vnc_manager_module,
        "_display_is_occupied",
        lambda display_number: display_number == 100,
        raising=False,
    )

    display_number, _ = await VncManager.start_vnc_for_session("pbs_display", organization_id="org_1")

    assert display_number == 101
    assert process_factory.processes[0].command[1] == ":101"
    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_readiness_timeout_is_bounded_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    monkeypatch.setattr(vnc_manager_module, "_display_is_ready", lambda _display: False)
    monkeypatch.setattr(vnc_manager_module, "VNC_STARTUP_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(VncStartupError, match="Xvfb readiness timed out"):
        await VncManager.start_vnc_for_session("pbs_timeout", organization_id="org_1")

    assert process_factory.events == ["terminate:Xvfb"]
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_startup_is_serialized_through_xvfb_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    first_xvfb_waiting = asyncio.Event()
    release_first_xvfb = asyncio.Event()
    held_first_xvfb = False

    async def wait_for_readiness(
        cls: type[VncManager],
        process: FakeProcess,
        readiness_check: Any,
        process_name: str,
    ) -> None:
        nonlocal held_first_xvfb
        del cls, readiness_check
        if process_name == "Xvfb" and not held_first_xvfb:
            held_first_xvfb = True
            first_xvfb_waiting.set()
            await release_first_xvfb.wait()

    monkeypatch.setattr(VncManager, "_wait_for_process_ready", classmethod(wait_for_readiness))

    first = asyncio.create_task(VncManager.start_vnc_for_session("pbs_first", organization_id="org_1"))
    await first_xvfb_waiting.wait()
    second = asyncio.create_task(VncManager.start_vnc_for_session("pbs_second", organization_id="org_1"))
    await asyncio.sleep(0)

    assert [process.name for process in process_factory.processes] == ["Xvfb"]

    release_first_xvfb.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == (100, 6080)
    assert second_result == (101, 6081)
    assert len(process_factory.processes) == 6
    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_org_mismatch_cannot_stop_another_orgs_vnc(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    await VncManager.start_vnc_for_session("pbs_owned", organization_id="org_owner")

    with pytest.raises(VncTeardownError, match="organization does not own"):
        await VncManager.stop_vnc_for_session("pbs_owned", organization_id="org_requester")

    assert VncManager.has_session("pbs_owned")
    assert process_factory.events == []

    with pytest.raises(VncStartupError, match="another organization"):
        await VncManager.start_vnc_for_session("pbs_owned")
    with pytest.raises(VncTeardownError, match="organization does not own"):
        await VncManager.stop_vnc_for_session("pbs_owned")

    assert VncManager.has_session("pbs_owned")
    await VncManager.stop_vnc_for_session("pbs_owned", organization_id="org_owner")


@pytest.mark.asyncio
async def test_idempotent_restart_replaces_dead_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    first_assignment = await VncManager.start_vnc_for_session("pbs_restart", organization_id="org_1")
    process_factory.processes[2].returncode = 1

    second_assignment = await VncManager.start_vnc_for_session("pbs_restart", organization_id="org_1")

    assert second_assignment == first_assignment
    assert len(process_factory.processes) == 6
    assert process_factory.events[:2] == ["terminate:x11vnc", "terminate:Xvfb"]
    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_child_must_remain_alive_during_post_readiness_stability_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DyingProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__(["x11vnc"], [])
            self.poll_count = 0

        def poll(self) -> int | None:
            self.poll_count += 1
            return None if self.poll_count == 1 else 1

    monkeypatch.setattr(vnc_manager_module, "VNC_CHILD_STABILITY_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(vnc_manager_module, "VNC_READINESS_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(VncStartupError, match="stability"):
        await VncManager._wait_for_process_ready(DyingProcess(), lambda: True, "x11vnc")


@pytest.mark.asyncio
async def test_startup_cancellation_waits_for_cleanup_before_releasing_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    readiness_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def block_readiness(
        cls: type[VncManager],
        process: FakeProcess,
        readiness_check: Any,
        process_name: str,
    ) -> None:
        del cls, process, readiness_check, process_name
        readiness_started.set()
        await asyncio.Event().wait()

    async def slow_cleanup(
        cls: type[VncManager],
        session_id: str,
        processes: Sequence[FakeProcess],
    ) -> None:
        del cls, session_id
        cleanup_started.set()
        await release_cleanup.wait()
        for process in processes:
            process.returncode = -15
        cleanup_finished.set()

    monkeypatch.setattr(VncManager, "_wait_for_process_ready", classmethod(block_readiness))
    monkeypatch.setattr(VncManager, "_terminate_processes", classmethod(slow_cleanup))
    task = asyncio.create_task(VncManager.start_vnc_for_session("pbs_cancel", organization_id="org_1"))
    await readiness_started.wait()

    task.cancel()
    await cleanup_started.wait()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert VncManager._used_displays == {100}
        assert VncManager._used_ports == {5900, 6080}
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_stop_cancellation_waits_for_cleanup_before_releasing_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    await VncManager.start_vnc_for_session("pbs_stop_cancel", organization_id="org_1")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_cleanup(
        cls: type[VncManager],
        session_id: str,
        processes: Sequence[FakeProcess],
    ) -> None:
        del cls, session_id
        cleanup_started.set()
        await release_cleanup.wait()
        for process in processes:
            process.returncode = -15
        cleanup_finished.set()

    monkeypatch.setattr(VncManager, "_terminate_processes", classmethod(slow_cleanup))
    task = asyncio.create_task(VncManager.stop_vnc_for_session("pbs_stop_cancel", organization_id="org_1"))
    await cleanup_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert VncManager.has_session("pbs_stop_cancel")
        assert VncManager._used_displays == {100}
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()
    assert not VncManager.has_session("pbs_stop_cancel")
    assert VncManager._used_displays == set()


@pytest.mark.asyncio
async def test_teardown_failure_attempts_all_children_and_retains_ownership_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    await VncManager.start_vnc_for_session("pbs_stubborn", organization_id="org_1")
    for process in process_factory.processes:
        process.stop_fails = True

    with pytest.raises(VncTeardownError, match="surviving") as exc_info:
        await VncManager.stop_vnc_for_session("pbs_stubborn", organization_id="org_1")

    assert exc_info.value.session_id == "pbs_stubborn"
    assert set(exc_info.value.survivors) == {"Xvfb", "x11vnc", "websockify"}
    assert process_factory.events == [
        "terminate:websockify",
        "kill:websockify",
        "terminate:x11vnc",
        "kill:x11vnc",
        "terminate:Xvfb",
        "kill:Xvfb",
    ]
    assert VncManager.has_session("pbs_stubborn")
    assert VncManager._used_displays == {100}
    assert VncManager._used_ports == {5900, 6080}

    for process in process_factory.processes:
        process.stop_fails = False
    await VncManager.stop_vnc_for_session("pbs_stubborn", organization_id="org_1")

    assert not VncManager.has_session("pbs_stubborn")
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_failed_partial_start_retains_stack_until_same_session_retry_can_clean_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory(returncodes=[None, 1])

    def create_process(command: Sequence[str], **kwargs: Any) -> FakeProcess:
        process = process_factory(command, **kwargs)
        if len(process_factory.processes) == 1:
            process.stop_fails = True
        return process

    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", create_process)

    with pytest.raises(VncStartupError, match="x11vnc exited"):
        await VncManager.start_vnc_for_session("pbs_partial", organization_id="org_1")

    assert VncManager.has_session("pbs_partial")
    assert VncManager._used_displays == {100}
    assert VncManager._used_ports == {5900, 6080}

    process_count = len(process_factory.processes)
    with pytest.raises(VncTeardownError, match="surviving") as exc_info:
        await VncManager.start_vnc_for_session("pbs_partial", organization_id="org_1")
    assert exc_info.value.session_id == "pbs_partial"
    assert exc_info.value.survivors == ("Xvfb",)
    assert len(process_factory.processes) == process_count
    assert VncManager._used_displays == {100}
    assert VncManager._used_ports == {5900, 6080}

    process_factory.processes[0].stop_fails = False
    assignment = await VncManager.start_vnc_for_session("pbs_partial", organization_id="org_1")

    assert assignment == (100, 6080)
    assert len(process_factory.processes) == 5
    await VncManager.stop_all()


@pytest.mark.asyncio
async def test_final_health_check_rejects_earlier_child_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)

    async def readiness_then_kill_xvfb(
        cls: type[VncManager],
        process: FakeProcess,
        readiness_check: Any,
        process_name: str,
    ) -> None:
        del cls, process, readiness_check
        if process_name == "websockify":
            process_factory.processes[0].returncode = 1

    monkeypatch.setattr(VncManager, "_wait_for_process_ready", classmethod(readiness_then_kill_xvfb))

    with pytest.raises(VncStartupError, match="health"):
        await VncManager.start_vnc_for_session("pbs_early_death", organization_id="org_1")

    assert not VncManager.has_session("pbs_early_death")
    assert VncManager._used_displays == set()
    assert VncManager._used_ports == set()


@pytest.mark.asyncio
async def test_stop_all_attempts_every_stack_and_retains_only_failed_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory = ProcessFactory()
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", process_factory)
    await VncManager.start_vnc_for_session("pbs_ok", organization_id="org_1")
    await VncManager.start_vnc_for_session("pbs_bad", organization_id="org_1")
    for process in process_factory.processes[3:]:
        process.stop_fails = True

    with pytest.raises(VncTeardownError, match="pbs_bad"):
        await VncManager.stop_all()

    assert not VncManager.has_session("pbs_ok")
    assert VncManager.has_session("pbs_bad")
    assert VncManager._used_displays == {101}
    assert VncManager._used_ports == {5901, 6081}

    for process in process_factory.processes[3:]:
        process.stop_fails = False
    await VncManager.stop_all()
