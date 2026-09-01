from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye import attach_only, display_recorder
from skyvern.webeye.display_recorder import (
    CAPTURE_DISPLAY_SIZE,
    CAPTURE_OUTPUT_BOUND,
    DisplayRecorder,
    acquire_display_recorder,
    build_ffmpeg_command,
    configure_local_display_recording,
    normalize_display,
    release_display_recorder,
    resolve_display_capture_sizes,
    resolve_owner_id,
    stop_display_recorders_for_owner,
)


@pytest.fixture(autouse=True)
def _clear_recorder_registry() -> Iterator[None]:
    display_recorder._REGISTRY.clear()
    yield
    # Close lock fds still held by leaked entries so the per-display lock does not fail-close a later test.
    for rec in list(display_recorder._REGISTRY.values()):
        rec._release_lock()
    display_recorder._REGISTRY.clear()


@pytest.mark.parametrize(
    "raw, expected",
    [
        (":99", ":99"),
        (":99.0", ":99"),
        (" :99.0 ", ":99"),
        ("host.example.com:0", "host.example.com:0"),
        ("host.example.com:0.0", "host.example.com:0"),
    ],
)
def test_normalize_display_strips_screen_but_preserves_hostname_dots(raw: str, expected: str) -> None:
    assert normalize_display(raw) == expected


def test_eligibility_hard_gates_attach_only_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_recorder.settings, "EXCLUSIVE_DISPLAY_RECORDING", True)
    monkeypatch.setattr(display_recorder.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(attach_only, "is_enforcing", lambda: True)
    args: dict[str, object] = {"record_video_dir": "/video", "record_video_size": {"width": 800}}

    assert configure_local_display_recording(args, task_id="task") is False
    assert args == {"record_video_dir": "/video", "record_video_size": {"width": 800}}


def test_ffmpeg_command_uses_safe_run_path_and_production_shape(tmp_path: Path) -> None:
    command, output_path = build_ffmpeg_command(display=":99", owner_id="../../run / unsafe", video_dir=tmp_path)

    assert output_path.parent.resolve().is_relative_to(tmp_path.resolve())
    assert output_path.name == f"run___unsafe-{hashlib.sha256(b'../../run / unsafe').hexdigest()}.webm"
    assert command == [
        "ffmpeg",
        "-y",
        "-f",
        "x11grab",
        "-video_size",
        "1920x1080",
        "-framerate",
        "15",
        "-i",
        ":99",
        "-vf",
        "scale=1280:720",
        "-c:v",
        "libvpx",
        "-b:v",
        "1M",
        "-qmin",
        "0",
        "-qmax",
        "50",
        "-an",
        "-deadline",
        "realtime",
        str(output_path),
    ]


@pytest.mark.parametrize(
    "window, expected",
    [
        ("1600,900", ((1600, 900), (1280, 720))),
        ("1512,982", ((1512, 982), (1108, 720))),  # height binds; width floored to an even 1108
        ("1920,1080", ((1920, 1080), (1280, 720))),  # base window → the fixed full-screen capture
    ],
)
def test_resolve_display_capture_sizes_scales_final_window_size(
    window: str, expected: tuple[tuple[int, int], tuple[int, int]]
) -> None:
    # The seam derives the capture rectangle from the FINAL --window-size (last entry wins, as the viewport
    # override leaves exactly one) and bounds the output within 1280x720, aspect-preserved with even dims.
    args = {"args": ["--window-position=0,0", "--window-size=1920,1080", f"--window-size={window}"]}
    assert resolve_display_capture_sizes(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        {"args": ["--window-position=0,0"]},  # no --window-size at all
        {"args": ["--window-size=not,a,size"]},  # unparseable
        {"args": ["--window-size=2000,1200"]},  # larger than the 1920x1080 display
        {"args": "not-a-list"},  # malformed args
    ],
)
def test_resolve_display_capture_sizes_falls_back_to_full_screen(args: dict[str, object]) -> None:
    assert resolve_display_capture_sizes(args) == (CAPTURE_DISPLAY_SIZE, CAPTURE_OUTPUT_BOUND)


@pytest.mark.parametrize(
    "capture_sizes, video_size, scale",
    [
        (((1600, 900), (1280, 720)), "1600x900", "scale=1280:720"),
        (((1512, 982), (1108, 720)), "1512x982", "scale=1108:720"),
        (None, "1920x1080", "scale=1280:720"),  # control: no resolved sizes → fixed full-screen capture
    ],
)
def test_ffmpeg_command_captures_window_and_scales_to_resolved_output(
    tmp_path: Path,
    capture_sizes: tuple[tuple[int, int], tuple[int, int]] | None,
    video_size: str,
    scale: str,
) -> None:
    command, _ = build_ffmpeg_command(":99", "owner", tmp_path, capture_sizes=capture_sizes)
    assert command[command.index("-video_size") + 1] == video_size
    assert command[command.index("-vf") + 1] == scale


def test_local_eligibility_strips_playwright_video_only_with_all_structural_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_recorder.settings, "EXCLUSIVE_DISPLAY_RECORDING", True)
    monkeypatch.setattr(display_recorder.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":99.0")
    args: dict[str, object] = {"record_video_dir": "/video", "record_video_size": {"width": 800}}

    assert configure_local_display_recording(args, workflow_run_id="workflow", task_id="task") is True
    assert "record_video_dir" not in args
    assert "record_video_size" not in args
    assert resolve_owner_id(workflow_run_id="workflow", task_id="task", script_id="script") == "workflow"


@pytest.mark.parametrize("missing", ["marker", "display", "owner"])
def test_ineligible_path_preserves_playwright_video_args(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_recorder.settings, "EXCLUSIVE_DISPLAY_RECORDING", True)
    monkeypatch.setattr(display_recorder.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":99")
    owner = "owner"
    if missing == "marker":
        monkeypatch.setattr(display_recorder.settings, "EXCLUSIVE_DISPLAY_RECORDING", False)
    elif missing == "display":
        monkeypatch.delenv("DISPLAY")
    else:
        owner = None
    args: dict[str, object] = {"record_video_dir": "/video", "record_video_size": {"width": 800}}

    assert configure_local_display_recording(args, task_id=owner) is False
    assert args == {"record_video_dir": "/video", "record_video_size": {"width": 800}}


def test_non_linux_remains_ineligible_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_recorder.settings, "EXCLUSIVE_DISPLAY_RECORDING", True)
    monkeypatch.setattr(display_recorder.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("DISPLAY", ":99")
    args = {"record_video_dir": "/video", "record_video_size": {"width": 800}}
    assert configure_local_display_recording(args, task_id="task") is False
    assert args == {"record_video_dir": "/video", "record_video_size": {"width": 800}}


@pytest.mark.asyncio
async def test_same_owner_reuses_recorder_and_exact_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=None)
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    first = await acquire_display_recorder(":99", "owner", tmp_path)
    second = await acquire_display_recorder(":99", "owner", tmp_path)

    assert first.started is True
    assert second.started is False
    assert second.recorder is first.recorder
    assert second.video_artifact is first.video_artifact
    first.video_artifact.video_artifact_id = "artifact-id"
    assert second.video_artifact.video_artifact_id == "artifact-id"
    assert spawn.await_count == 1

    process.returncode = 0
    assert await release_display_recorder(first.recorder) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "owner-c"], ids=["no-override", "foreign-override"])
async def test_different_owner_cannot_touch_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    # The override re-adopts a live display ONLY when it names the current holder; a missing or foreign
    # override is refused, so a different owner never adopts another run's recorder.
    process = MagicMock(returncode=None)
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    first = await acquire_display_recorder(":99", "owner-a", tmp_path)
    refused = await acquire_display_recorder(":99", "owner-b", tmp_path, owner_id_override=override)

    assert refused.recorder is None
    assert refused.video_artifact is None
    assert process.send_signal.call_count == 0
    assert process.terminate.call_count == 0
    assert process.kill.call_count == 0
    assert spawn.await_count == 1

    process.returncode = 0
    assert await release_display_recorder(first.recorder) is True


@pytest.mark.asyncio
async def test_finalized_recorder_keeps_display_reserved_until_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security: finalizing a recorder while its owner's browser stays mapped (deferred live-view close) keeps
    the display reserved to that owner until real browser teardown — a different owner must not capture it."""
    proc_a = MagicMock(returncode=None)
    proc_a.wait = AsyncMock()
    proc_b = MagicMock(returncode=None)
    proc_b.wait = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[proc_a, proc_b]))

    a = await acquire_display_recorder(":99", "owner-a", tmp_path)
    proc_a.returncode = 0  # ffmpeg exits gracefully on the finalize SIGINT
    await a.recorder.finalize_keeping_reservation()
    assert a.recorder.is_stopped

    # The owner's activity-finally cleanup must NOT reap the reserved recorder (dropping the reserved-skip
    # guard in stop_display_recorders_for_owner reaps it here and REDs both assertions below).
    assert await stop_display_recorders_for_owner("owner-a") == 0, (
        "the activity finally must not reap a recorder still reserved to its deferred browser"
    )

    refused = await acquire_display_recorder(":99", "owner-b", tmp_path)
    assert refused.recorder is None, "a different owner must not acquire a display reserved to the prior owner"

    # Only the real browser teardown releases the reservation.
    await release_display_recorder(a.recorder)
    b = await acquire_display_recorder(":99", "owner-b", tmp_path)
    assert b.recorder is not None
    proc_b.returncode = 0
    await release_display_recorder(b.recorder)


@pytest.mark.asyncio
async def test_recorder_launch_is_fork_safe_without_preexec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: a fork-time preexec_fn (ctypes prctl) can hang the multithreaded worker's fork child before
    exec; the launch must pass NO preexec_fn and set PDEATHSIG via an exec'd python trampoline instead."""
    process = MagicMock(returncode=None)
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    rec = await acquire_display_recorder(":99", "owner", tmp_path)
    assert rec.recorder is not None

    assert spawn.call_args.kwargs.get("preexec_fn") is None, "recorder launch must not run a fork-time preexec_fn"
    argv = list(spawn.call_args.args)
    assert argv[:2] == [sys.executable, "-c"], "ffmpeg must be launched through the exec'd python trampoline"
    assert "prctl" in argv[2] and "os.execvp(sys.argv[2]" in argv[2], "trampoline sets PDEATHSIG then execs ffmpeg"
    # Parent passes its pid to catch a pre-exec reparent; trampoline-time getppid capture drops it (RED below).
    assert argv[3] == str(os.getpid()), "the parent must pass its own pid as the expected-parent argv"
    assert argv[4] == "ffmpeg", "ffmpeg must follow the trampoline and the expected-parent pid, passed as argv"


@pytest.mark.asyncio
async def test_sequential_owners_get_fresh_recorders_artifacts_and_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_a = MagicMock(returncode=None)
    process_a.wait = AsyncMock()
    process_b = MagicMock(returncode=None)
    process_b.wait = AsyncMock()
    spawn = AsyncMock(side_effect=[process_a, process_b])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    owner_a = await acquire_display_recorder(":99", "owner/a", tmp_path)
    assert owner_a.recorder is not None and owner_a.video_artifact is not None
    process_a.returncode = 0
    assert await release_display_recorder(owner_a.recorder) is True
    assert (":99", "owner/a") not in display_recorder._REGISTRY
    assert process_a.send_signal.call_count == 0
    assert process_a.terminate.call_count == 0
    assert process_a.kill.call_count == 0

    owner_b = await acquire_display_recorder(":99", "owner_a", tmp_path)
    assert owner_b.recorder is not None and owner_b.video_artifact is not None
    assert owner_b.recorder is not owner_a.recorder
    assert owner_b.video_artifact is not owner_a.video_artifact
    assert owner_a.video_artifact.video_path != owner_b.video_artifact.video_path
    assert Path(owner_a.video_artifact.video_path).name == (f"owner_a-{hashlib.sha256(b'owner/a').hexdigest()}.webm")
    assert Path(owner_b.video_artifact.video_path).name == (f"owner_a-{hashlib.sha256(b'owner_a').hexdigest()}.webm")
    assert spawn.await_count == 2

    process_b.returncode = 0
    assert await release_display_recorder(owner_b.recorder) is True
    assert display_recorder._REGISTRY == {}


@pytest.mark.asyncio
async def test_stop_escalates_reaps_and_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=None)

    async def wait() -> None:
        if process.kill.called:
            process.returncode = -signal.SIGKILL
            return
        await asyncio.Future()

    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGINT_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGTERM_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.STARTUP_READINESS_TIMEOUT", 0.001)

    acquired = await acquire_display_recorder(":99", "owner", tmp_path)
    recorder = acquired.recorder
    assert recorder is not None
    lock_path = recorder.lock_path_for_display(":99")

    assert await recorder.stop() is False
    assert await recorder.stop() is False
    process.send_signal.assert_called_once_with(signal.SIGINT)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_start_failure_releases_display_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("missing ffmpeg")))

    failed = await acquire_display_recorder(":99", "owner-a", tmp_path)
    assert failed.recorder is None

    lock_path = DisplayRecorder.lock_path_for_display(":99")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_dead_different_owner_entry_does_not_block_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_a = MagicMock(returncode=None)
    process_a.wait = AsyncMock()
    process_b = MagicMock(returncode=None)
    process_b.wait = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process_a, process_b]))

    first = await acquire_display_recorder(":99", "owner-a", tmp_path)
    assert first.recorder is not None
    # owner-a's recorder exits without a clean release (e.g. pod-local leak).
    process_a.returncode = 0

    second = await acquire_display_recorder(":99", "owner-b", tmp_path)
    assert second.recorder is not None
    assert second.started is True
    # The dead holder must not have been signalled while it was being pruned.
    assert process_a.kill.call_count == 0
    assert process_a.terminate.call_count == 0

    process_b.returncode = 0
    assert await release_display_recorder(second.recorder) is True


@pytest.mark.asyncio
async def test_stop_reaps_and_releases_lock_on_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=None)

    async def wait() -> None:
        if process.kill.called:
            process.returncode = -signal.SIGKILL
            return
        await asyncio.Future()

    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("skyvern.webeye.display_recorder.STARTUP_READINESS_TIMEOUT", 0.001)

    acquired = await acquire_display_recorder(":99", "owner", tmp_path)
    recorder = acquired.recorder
    assert recorder is not None

    task = asyncio.ensure_future(recorder.stop())
    await asyncio.sleep(0.01)  # let stop() reach the SIGINT wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation must have reaped the child and released the lock before propagating.
    assert process.kill.called
    lock_fd = os.open(recorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_stop_survives_double_cancel_and_still_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cancel arriving while stop() is under the post-kill reap must not skip lock release."""
    killed = asyncio.Event()
    allow_reap = asyncio.Event()
    process = MagicMock(returncode=None)

    def _kill() -> None:
        killed.set()

    async def wait() -> int:
        await allow_reap.wait()
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.kill = _kill
    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("skyvern.webeye.display_recorder.STARTUP_READINESS_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGINT_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGTERM_TIMEOUT", 0.001)

    acquired = await acquire_display_recorder(":99", "owner", tmp_path)
    recorder = acquired.recorder
    assert recorder is not None

    task = asyncio.ensure_future(recorder.stop())
    await asyncio.sleep(0)
    task.cancel()  # first cancel: escalation is abandoned, finally kills and reaps
    await killed.wait()
    task.cancel()  # second cancel: lands while the post-kill reap is pending
    await asyncio.sleep(0)
    allow_reap.set()  # let the reap complete
    with pytest.raises(asyncio.CancelledError):
        await task

    # Despite two cancels, the child was reaped and the lock is free for the next run.
    lock_fd = os.open(recorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_release_holds_resources_until_serialized_stop_is_terminal_under_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-A: a release caller cancelled while parked behind another release's serialized stop must not free the
    reservation/lock/registry until that stop is terminal — otherwise a different owner could acquire the display
    mid-shutdown. The cancelled caller re-raises exactly once, only after the stop completes and resources drop."""
    entered = asyncio.Event()
    allow = asyncio.Event()
    process = MagicMock(returncode=None)
    lock_fd = os.open(DisplayRecorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    recorder = DisplayRecorder(
        display=":99", owner_id="owner", process=process, lock_fd=lock_fd, video_artifact=MagicMock()
    )
    key = (recorder.display, recorder.owner_id)
    display_recorder._REGISTRY[key] = recorder

    async def _escalate(_graceful: bool) -> bool:
        entered.set()
        await allow.wait()
        process.returncode = 0
        return True

    monkeypatch.setattr(recorder, "_escalate_shutdown", _escalate)

    first = asyncio.ensure_future(release_display_recorder(recorder))
    await entered.wait()  # first release's serialized stop holds _stop_lock, parked mid-shutdown
    second = asyncio.ensure_future(release_display_recorder(recorder))
    await asyncio.sleep(0)  # park second behind _stop_lock
    second.cancel()
    await asyncio.sleep(0)  # deliver the cancel while second is parked

    assert not recorder.is_stopped, "the serialized stop must still be in flight"
    assert recorder.lock_fd == lock_fd, "a cancelled parked release must not release the flock early"
    assert display_recorder._REGISTRY.get(key) is recorder, "the registry reservation must not drop early"

    allow.set()
    assert await first is True
    with pytest.raises(asyncio.CancelledError):
        await second

    assert recorder.is_stopped
    assert recorder.lock_fd == -1
    assert key not in display_recorder._REGISTRY


@pytest.mark.asyncio
async def test_dead_same_owner_fails_closed_and_preserves_exact_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exited SAME-owner recorder must not be restarted (-y overwrites the partial WebM and mints a second
    RECORDING row); reuse the exact prior artifact object instead."""
    process = MagicMock(returncode=None)
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    first = await acquire_display_recorder(":99", "owner", tmp_path)
    assert first.recorder is not None
    first.video_artifact.video_artifact_id = "va_1"
    process.returncode = 1  # the recorder died mid-run

    second = await acquire_display_recorder(":99", "owner", tmp_path)
    assert second.recorder is first.recorder
    assert second.video_artifact is first.video_artifact
    assert second.video_artifact.video_artifact_id == "va_1"
    assert second.started is False
    assert spawn.await_count == 1  # never restarted


@pytest.mark.asyncio
async def test_startup_readiness_detects_early_ffmpeg_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg that spawns then dies (bad x11grab init) must not seed an artifact or leak the lock."""
    process = MagicMock(returncode=None)

    async def wait() -> int:
        process.returncode = 1  # exits shortly after spawn
        return 1

    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    acquisition = await acquire_display_recorder(":99", "owner", tmp_path)
    assert acquisition.recorder is None
    assert acquisition.video_artifact is None

    lock_fd = os.open(DisplayRecorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_stale_release_does_not_evict_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A delayed release of a superseded recorder must not remove the replacement under the same key."""
    process_a = MagicMock(returncode=None)
    process_a.wait = AsyncMock()
    process_b = MagicMock(returncode=None)
    process_b.wait = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process_a, process_b]))

    a = await acquire_display_recorder(":99", "owner", tmp_path)
    process_a.returncode = 0
    await release_display_recorder(a.recorder)  # A leaves the registry

    b = await acquire_display_recorder(":99", "owner", tmp_path)  # B registers under the same key
    assert b.recorder is not a.recorder  # B is a distinct recorder
    assert b.started is True

    process_a.returncode = 0
    await release_display_recorder(a.recorder)  # delayed duplicate release of A — must not evict B

    reuse = await acquire_display_recorder(":99", "owner", tmp_path)
    assert reuse.recorder is b.recorder
    assert reuse.video_artifact is b.video_artifact

    process_b.returncode = 0
    await release_display_recorder(b.recorder)


@pytest.mark.asyncio
async def test_acquire_unwind_propagates_cancel_during_plain_exception_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup fails with an ordinary Exception while a (repeated) cancel lands mid-reap: the unwind must still
    reap, release the fd, and PROPAGATE the cancellation rather than swallow it into a fail-closed acquisition."""
    allow_reap = asyncio.Event()
    killed = asyncio.Event()
    calls = {"n": 0}
    process = MagicMock(returncode=None)

    def _kill() -> None:
        killed.set()

    async def wait() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("readiness probe boom")  # ordinary-Exception unwind, process still alive
        await allow_reap.wait()
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.kill = _kill
    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    task = asyncio.ensure_future(acquire_display_recorder(":99", "owner", tmp_path))
    await killed.wait()  # unwind entered, kill sent, reap now blocked
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancel during the blocked reap
    await asyncio.sleep(0)
    allow_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    lock_fd = os.open(DisplayRecorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_healthy_recorder_survives_readiness_wait_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy ffmpeg does not exit inside the readiness window, so wait_for times out and cancels that
    initial Process.wait. The recorder must still be seeded AND remain fully stoppable/finalizable after."""
    exited = asyncio.Event()
    process = MagicMock(returncode=None)

    def _sigint(_sig: signal.Signals) -> None:
        process.returncode = 0
        exited.set()

    async def wait() -> int:
        await exited.wait()
        return process.returncode or 0

    process.send_signal = MagicMock(side_effect=_sigint)
    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("skyvern.webeye.display_recorder.STARTUP_READINESS_TIMEOUT", 0.02)

    acquisition = await acquire_display_recorder(":99", "owner", tmp_path)
    assert acquisition.recorder is not None
    assert acquisition.started is True  # readiness timed out (healthy) → seeded

    # The timed-out/cancelled initial wait() must not have broken later shutdown: stop finalizes cleanly.
    assert await release_display_recorder(acquisition.recorder) is True
    process.send_signal.assert_called_once_with(signal.SIGINT)

    lock_fd = os.open(acquisition.recorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


def _seed(video_root: Path, owner_id: str, date: str) -> Path:
    date_dir = video_root / date
    date_dir.mkdir(parents=True, exist_ok=True)
    path = date_dir / f"{display_recorder._safe_owner_id(owner_id)}.webm"
    path.write_bytes(b"webm")
    return path


def test_unlink_recordings_removes_only_owner_digest_across_date_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(display_recorder.settings, "VIDEO_PATH", str(tmp_path))
    own_day1 = _seed(tmp_path, "owner", "2026-08-26")
    own_day2 = _seed(tmp_path, "owner", "2026-08-27")
    sibling = _seed(tmp_path, "other-owner", "2026-08-27")
    playwright_style = tmp_path / "2026-08-27" / "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0.webm"
    playwright_style.write_bytes(b"pw")

    removed = display_recorder.unlink_recordings_for_owner("owner")

    assert removed == 2
    assert not own_day1.exists()
    assert not own_day2.exists()
    assert sibling.exists(), "a sibling owner's digest must survive"
    assert playwright_style.exists(), "a Playwright random-name recording must survive"


@pytest.mark.parametrize("root", [None, "/nonexistent/video/root"])
def test_unlink_recordings_missing_or_unset_root_is_log_only(monkeypatch: pytest.MonkeyPatch, root: str | None) -> None:
    monkeypatch.setattr(display_recorder.settings, "VIDEO_PATH", root)
    # No raise, no file system requirement — a missing/unset root simply removes nothing.
    assert display_recorder.unlink_recordings_for_owner("owner") == 0


def test_resolve_owner_id_accepts_browser_session_id_as_lowest_priority() -> None:
    assert resolve_owner_id(browser_session_id="bs_1") == "bs_1"
    assert resolve_owner_id(task_id="tsk_1", browser_session_id="bs_1") == "tsk_1"
    assert (
        resolve_owner_id(workflow_run_id="wr_1", task_id="tsk_1", script_id="scr_1", browser_session_id="bs_1")
        == "wr_1"
    )


def test_owner_id_override_is_fallback_only() -> None:
    """The reconnect override preserves the standalone owner ONLY when no canonical id is present; any explicit
    canonical owner wins so a stray/hostile override can't weaken isolation (RED if the override resolves first)."""
    assert resolve_owner_id(owner_id_override="ovr") == "ovr"
    for field in ("workflow_run_id", "task_id", "script_id", "browser_session_id"):
        assert resolve_owner_id(owner_id_override="ovr", **{field: "canon"}) == "canon"


async def _acquire_live(display: str, owner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DisplayRecorder:
    process = MagicMock(returncode=None)

    async def wait() -> None:
        if process.kill.called:
            process.returncode = -signal.SIGKILL
            return
        await asyncio.Future()

    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGINT_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.SIGTERM_TIMEOUT", 0.001)
    monkeypatch.setattr("skyvern.webeye.display_recorder.STARTUP_READINESS_TIMEOUT", 0.001)
    acquired = await acquire_display_recorder(display, owner, tmp_path)
    assert acquired.recorder is not None
    return acquired.recorder


@pytest.mark.asyncio
async def test_stop_display_recorders_for_owner_reaps_live_recorder_and_clears_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B2: a cancelled activity leaves a live ffmpeg + registry entry + display lock; the owner-scoped stop
    # must reap it BEFORE the unlink so it is not orphaned into the next activity on this worker.
    recorder = await _acquire_live(":99", "wr_cancelled", tmp_path, monkeypatch)
    assert (":99", "wr_cancelled") in display_recorder._REGISTRY

    released = await display_recorder.stop_display_recorders_for_owner("wr_cancelled")

    assert released == 1
    assert recorder.process.kill.called, "the surviving ffmpeg must be forcibly reaped"
    assert (":99", "wr_cancelled") not in display_recorder._REGISTRY, "the registry entry must be dropped"
    # The display lock must be free for the next run.
    lock_fd = os.open(recorder.lock_path_for_display(":99"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)


@pytest.mark.asyncio
async def test_stop_display_recorders_for_owner_leaves_other_owners_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = await _acquire_live(":99", "wr_mine", tmp_path, monkeypatch)
    theirs = await _acquire_live(":98", "wr_theirs", tmp_path, monkeypatch)

    released = await display_recorder.stop_display_recorders_for_owner("wr_mine")

    assert released == 1
    assert mine.process.kill.called
    assert (":98", "wr_theirs") in display_recorder._REGISTRY, "a different owner's live recorder must survive"
    assert not theirs.process.kill.called


@pytest.mark.asyncio
async def test_stop_display_recorders_for_owner_absent_is_noop() -> None:
    assert await display_recorder.stop_display_recorders_for_owner("wr_absent") == 0


@pytest.mark.asyncio
async def test_stop_display_recorders_for_owner_absorbs_cancellation_and_clears_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stop() re-raises a single cancellation AFTER it has reaped and released; the owner-scoped helper
    # must absorb it (so the caller's unlink still runs) yet still drop the registry entry.
    recorder = await _acquire_live(":99", "wr_cancel_in_stop", tmp_path, monkeypatch)

    async def _cancel_stop() -> bool:
        recorder._release_lock()
        key = (recorder.display, recorder.owner_id)
        display_recorder._REGISTRY.pop(key, None)
        raise asyncio.CancelledError()

    monkeypatch.setattr(recorder, "stop", _cancel_stop)

    released = await display_recorder.stop_display_recorders_for_owner("wr_cancel_in_stop")

    assert released == 1
    assert (":99", "wr_cancel_in_stop") not in display_recorder._REGISTRY
