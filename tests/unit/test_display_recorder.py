"""Contract/lifecycle tests for the exclusive whole-display recorder.

Covers the required product behavior: OFF-path leaves Playwright recording untouched (default); ON-path
suppresses per-page recording and builds the exact X4 bridge command to a single owner-exact ``.mp4``;
narrow profile controls validate/fall back; and ``stop()`` escalation is idempotent and reap-safe.
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from skyvern.config import settings
from skyvern.webeye import display_recorder as dr


def test_profile_defaults_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    p = dr.RecordingProfile.resolve()
    assert (p.output_width, p.output_height) == (1280, 720)
    assert p.max_fps == 15 and p.crf == 28 and p.keyframe_seconds == 5


@pytest.mark.parametrize(
    "attr,value,field,expected",
    [
        ("DISPLAY_RECORDING_MAX_FPS", 0, "max_fps", 15),  # below range -> default
        ("DISPLAY_RECORDING_MAX_FPS", 999, "max_fps", 15),  # above range -> default
        ("DISPLAY_RECORDING_CRF", -1, "crf", 28),  # invalid -> default
        ("DISPLAY_RECORDING_CRF", 99, "crf", 28),
        ("DISPLAY_RECORDING_KEYFRAME_SECONDS", 0, "keyframe_seconds", 5),
        ("DISPLAY_RECORDING_OUTPUT_WIDTH", 1281, "output_width", 1280),  # odd -> floored even
        ("DISPLAY_RECORDING_OUTPUT_HEIGHT", 99999, "output_height", 720),  # > display -> default
    ],
)
def test_profile_invalid_values_fall_back(monkeypatch, attr, value, field, expected) -> None:
    monkeypatch.setattr(settings, attr, value, raising=False)
    p = dr.RecordingProfile.resolve()
    assert getattr(p, field) == expected


def _args_with_playwright_recording() -> dict:
    return {"record_video_dir": "/tmp/vid", "record_video_size": {"width": 800, "height": 450}, "args": []}


def _all_on_gate(monkeypatch) -> None:
    monkeypatch.setattr(dr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(settings, "EXCLUSIVE_DISPLAY_RECORDING", True)
    monkeypatch.setattr(settings, "VIDEO_PATH", "/tmp/artifacts")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(dr.attach_only, "is_enforcing", lambda: False)


def _gate_default_off(mp) -> None:
    assert settings.EXCLUSIVE_DISPLAY_RECORDING is False  # default OFF


def _gate_conc1(mp) -> None:
    _all_on_gate(mp)
    mp.setattr(settings, "BROWSER_WORKER_MAX_CONCURRENT_ACTIVITIES", 1, raising=False)


def _gate_falsy_video(mp) -> None:
    _all_on_gate(mp)
    mp.setattr(settings, "VIDEO_PATH", "")  # falsy: recorder can't write


def _gate_conc2(mp) -> None:
    _all_on_gate(mp)
    mp.setattr(settings, "BROWSER_WORKER_MAX_CONCURRENT_ACTIVITIES", 2, raising=False)  # prod browserConcurrency>1


def _gate_flag_off(mp) -> None:
    # Everything eligible (Linux, DISPLAY, VIDEO_PATH, concurrency 1, owner present); ONLY the default-OFF flag
    # decides. Single-mutation-precise: deleting the EXCLUSIVE_DISPLAY_RECORDING conjunct flips this row to eligible.
    _gate_conc1(mp)
    mp.setattr(settings, "EXCLUSIVE_DISPLAY_RECORDING", False)


@pytest.mark.parametrize(
    "setup,owner,expected",
    [
        (_gate_default_off, "wr_123", False),
        (_gate_conc1, "wr_conc1", True),
        (_gate_falsy_video, "wr_novideo", False),
        (_all_on_gate, None, False),
        (_gate_conc2, "wr_conc2", False),
        (_gate_flag_off, "wr_flagoff", False),
    ],
    ids=["default_off", "conc1_eligible", "falsy_video_path", "no_owner", "conc2_declines", "flag_off_declines"],
)
def test_configure_eligibility_gate(monkeypatch, setup, owner, expected) -> None:
    # Eligibility-ONLY (never pops record_video_*); every fail-closed case (falsy VIDEO_PATH, no owner,
    # concurrency != 1) keeps per-page recording so a run never ends with NEITHER recording.
    setup(monkeypatch)
    args = _args_with_playwright_recording()
    kw = {"workflow_run_id": owner} if owner else {}
    assert dr.configure_local_display_recording(args, **kw) is expected
    assert args["record_video_dir"] == "/tmp/vid"  # eligibility never pops
    assert args["record_video_size"] == {"width": 800, "height": 450}


@pytest.mark.asyncio
async def test_prepare_pops_playwright_args_only_on_acquired_recorder(monkeypatch) -> None:
    # Success: a live recorder is acquired, so prepare pops record_video_* and seeds one VideoArtifact.
    from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
    from skyvern.webeye.display_recorder import DisplayRecorderAcquisition

    _gate_conc1(monkeypatch)  # eligible: all gates on, concurrency 1
    va = VideoArtifact(video_path="/tmp/artifacts/rec.mp4", video_file_extension="mp4")
    fake_recorder = object()

    async def _fake_acquire(*a, **k):
        return DisplayRecorderAcquisition(fake_recorder, va, True)

    monkeypatch.setattr(dr, "acquire_display_recorder", _fake_acquire)
    args = _args_with_playwright_recording()
    artifacts = BrowserArtifacts()
    await dr.prepare_local_display_recording(args, artifacts, workflow_run_id="wr_ok")
    assert "record_video_dir" not in args and "record_video_size" not in args  # popped on success
    assert artifacts._display_recorder is fake_recorder
    assert artifacts.video_artifacts == [va]  # exactly one recording
    assert artifacts._display_recorder_acquisition is not None


@pytest.mark.asyncio
async def test_prepare_keeps_playwright_args_on_refusal(monkeypatch) -> None:
    # Refusal (e.g. display has a live owner): recorder is None, so prepare must NOT pop record_video_* — the
    # run falls back to Playwright per-page recording.
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.display_recorder import DisplayRecorderAcquisition

    _gate_conc1(monkeypatch)  # eligible: all gates on, concurrency 1

    async def _refuse(*a, **k):
        return DisplayRecorderAcquisition(None, None, False)

    monkeypatch.setattr(dr, "acquire_display_recorder", _refuse)
    args = _args_with_playwright_recording()
    args[dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY] = {"width": 1920, "height": 1080}
    artifacts = BrowserArtifacts()
    await dr.prepare_local_display_recording(args, artifacts, workflow_run_id="wr_refused")
    assert artifacts.local_display_recording_eligible is True  # was eligible...
    assert args["record_video_dir"] == "/tmp/vid"  # ...but refusal leaves Playwright recording intact
    assert args["record_video_size"] == {"width": 800, "height": 450}
    # The carrier is consumed even on the eligible-but-refused path, so it never leaks into launch kwargs
    # on a run that falls back to Playwright per-page recording.
    assert dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY not in args
    assert artifacts._display_recorder is None
    assert artifacts._display_recorder_acquisition is None


def test_build_capture_command_emits_single_owner_exact_mp4() -> None:
    cmd, out = dr.build_capture_command(":99", "wr_abc", Path("/video"))
    assert out.suffix == ".mp4"
    assert out.name.startswith("wr_abc-")  # owner-derived, collision-safe digest
    joined = " ".join(cmd)
    assert "skyvern.webeye.display_capture_bridge" in joined
    assert "--out-size 1280x720" in joined
    assert str(out) in joined


def test_acquired_video_artifact_advertises_mp4_before_first_artifact_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The recorder always writes a fragmented MP4, so the seeded VideoArtifact must advertise ``mp4`` up front,
    # or the step-0 artifact uri defaults to ``.webm`` for H.264 bytes.
    if dr.fcntl is None:
        pytest.skip("fcntl unavailable on this platform")

    async def _fake_exec(*_args: object, **_kwargs: object) -> "_FakeProc":
        return _FakeProc(exits_on="never")  # emits the default READY ack, stays live -> healthy startup

    monkeypatch.setattr(dr, "STARTUP_ACK_TIMEOUT", 0.5)
    monkeypatch.setattr(dr.os, "open", lambda *a, **k: 7)
    monkeypatch.setattr(dr.fcntl, "flock", lambda *a, **k: None)
    monkeypatch.setattr(dr.asyncio, "create_subprocess_exec", _fake_exec)

    async def run() -> dr.DisplayRecorderAcquisition:
        return await dr.acquire_display_recorder(":99", "wr_ext", tmp_path)

    try:
        acq = asyncio.run(run())
        assert acq.started is True
        assert acq.video_artifact is not None
        assert acq.video_artifact.video_file_extension == "mp4"
        assert (acq.video_artifact.video_path or "").endswith(".mp4")
    finally:
        dr._REGISTRY.pop((":99", "wr_ext"), None)


def test_owner_id_is_deterministic_and_owner_exact() -> None:
    a = dr._safe_owner_id("wr_abc")
    assert a == dr._safe_owner_id("wr_abc")  # deterministic
    assert a != dr._safe_owner_id("wr_abd")  # different owner -> different filename


def test_capture_sizes_scale_from_window_and_fall_back_when_invalid() -> None:
    (iw, ih), (ow, oh) = dr.resolve_display_capture_sizes({"args": ["--window-size=1600,900"]})
    assert (iw, ih) == (1600, 900)
    assert (ow, oh) == (1280, 720)  # aspect-preserved fit, even
    (iw2, ih2), _ = dr.resolve_display_capture_sizes({"args": ["--window-size=99999,99999"]})
    assert (iw2, ih2) == dr.CAPTURE_DISPLAY_SIZE


def test_capture_sizes_honor_raw_output_bound_not_capped_playwright_size() -> None:
    # 1000x700 window + raw 1080p bound: the window fits the raw bound whole, so output == window.
    # Passing the viewport-CAPPED Playwright size (1000x562) as the bound instead fits the window a
    # SECOND time and corrupts it to 802x562 — the double-aspect-fit defect the raw carrier prevents.
    args = {"args": ["--window-size=1000,700"]}
    (iw, ih), (ow, oh) = dr.resolve_display_capture_sizes(args, output_bound={"width": 1920, "height": 1080})
    assert (iw, ih) == (1000, 700)
    assert (ow, oh) == (1000, 700)
    _, corrupted = dr.resolve_display_capture_sizes(args, output_bound={"width": 1000, "height": 562})
    assert corrupted == (802, 562)  # what using the capped Playwright size would produce (never used here)


def test_capture_sizes_fit_raw_profile_cross_aspect() -> None:
    # 800x600 (4:3) window + raw 480p (854x480) bound: largest even 4:3 fitting the profile, no pad/crop.
    (_input, output) = dr.resolve_display_capture_sizes(
        {"args": ["--window-size=800,600"]}, output_bound={"width": 854, "height": 480}
    )
    assert output == (640, 480)


def test_capture_sizes_reject_malformed_or_mixed_validity_bound_atomically() -> None:
    # Atomic PAIR validation: any absent / non-dict / partial / non-int / bool / out-of-bounds axis
    # falls back to the COMPLETE static profile pair (1280x720), never mixing a valid carrier axis
    # with a profile axis. Proven discriminating by a 1900x300 window: per-axis mixing would let the
    # width-valid cases leak through as 1900x300 (carrier width 1920 + profile height 720); the atomic
    # fallback yields 1280x202 for every malformed input.
    args = {"args": ["--window-size=1900,300"]}
    for bad in (
        None,
        123,
        "1280x720",
        {},
        {"width": 1920},  # height missing
        {"height": 1080},  # width missing
        {"width": 1920, "height": "1080"},  # height non-int
        {"width": "x", "height": "y"},  # both non-int
        {"width": True, "height": 1080},  # bool is not a real dimension
        {"width": 1920, "height": 99999},  # height out of display bounds
        {"width": 0, "height": 1080},  # width below the even minimum
    ):
        (_input, output) = dr.resolve_display_capture_sizes(args, output_bound=bad)
        assert output == (1280, 202)


@pytest.mark.asyncio
async def test_prepare_consumes_output_bound_carrier_into_capture_sizes(monkeypatch) -> None:
    # Eligible/acquired path: the raw-output-bound carrier is REMOVED from browser_args (never reaching
    # launch kwargs) and freezes the capture output. A 1000x900 window under a raw 1080p bound yields
    # output 1000x900; the static 720p default would instead shrink it to 800x720, so this fails if the
    # carrier is dropped, resolved before the pop, or replaced by the capped Playwright size.
    from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
    from skyvern.webeye.display_recorder import DisplayRecorderAcquisition

    _gate_conc1(monkeypatch)
    va = VideoArtifact(video_path="/tmp/artifacts/rec.mp4", video_file_extension="mp4")

    async def _fake_acquire(*a, **k):
        return DisplayRecorderAcquisition(object(), va, True)

    monkeypatch.setattr(dr, "acquire_display_recorder", _fake_acquire)
    args = {
        "record_video_dir": "/tmp/vid",
        "record_video_size": {"width": 1000, "height": 562},  # the viewport-capped Playwright size
        "args": ["--window-size=1000,900"],
        dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY: {"width": 1920, "height": 1080},  # raw 1080p selection
    }
    artifacts = BrowserArtifacts()
    await dr.prepare_local_display_recording(args, artifacts, workflow_run_id="wr_bound")
    assert dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY not in args  # consumed + removed before launch
    assert artifacts._display_capture_sizes == ((1000, 900), (1000, 900))  # raw bound, not capped/static


@pytest.mark.asyncio
async def test_prepare_pops_output_bound_carrier_even_when_ineligible(monkeypatch) -> None:
    # Ineligible (flag off): the carrier is consumed BEFORE the eligibility short-circuit so it can never
    # leak into the browser launch kwargs on a run that keeps Playwright per-page recording.
    from skyvern.webeye.browser_artifacts import BrowserArtifacts

    _gate_flag_off(monkeypatch)
    args = {
        "record_video_dir": "/tmp/vid",
        "record_video_size": {"width": 800, "height": 450},
        "args": ["--window-size=1000,700"],
        dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY: {"width": 1920, "height": 1080},
    }
    artifacts = BrowserArtifacts()
    await dr.prepare_local_display_recording(args, artifacts, workflow_run_id="wr_ineligible")
    assert artifacts.local_display_recording_eligible is False
    assert dr.DISPLAY_RECORDING_OUTPUT_BOUND_KEY not in args  # removed even on the ineligible path
    assert args["record_video_dir"] == "/tmp/vid"  # Playwright recording intact


def test_resolve_owner_id_prefers_canonical_over_override() -> None:
    assert dr.resolve_owner_id(owner_id_override="ovr", task_id="tsk_1") == "tsk_1"
    assert dr.resolve_owner_id(owner_id_override="ovr") == "ovr"


class _FakeStdout:
    """Models the bridge's readiness pipe: one line, or "wedge" to block forever (parent readline times out)."""

    def __init__(self, ack: bytes | str) -> None:
        self._ack = ack

    async def readline(self) -> bytes:
        if self._ack == "wedge":
            while True:
                await asyncio.sleep(0.005)  # never returns: wait_for(readline) times out or is cancelled
        return self._ack  # type: ignore[return-value]


class _FakeProc:
    """Controllable asyncio-process double: real stop() escalation logic runs against it."""

    def __init__(
        self,
        *,
        exits_on: str,
        exit_code: int = 0,
        ack: bytes | str = b"READY\n",
        ack_returncode: int | None = None,
    ) -> None:
        self.returncode: int | None = ack_returncode  # already-exited child when set before the readiness read
        self._exits_on = exits_on  # "SIGINT" | "SIGTERM" | "never"
        self._exit_code = exit_code
        self.signals: list[int] = []
        self.killed = False
        self.pid = 2_147_483_646  # non-existent pid: getpgid fails -> _kill_process_group falls back to kill()
        self.stdout = _FakeStdout(ack)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self._exits_on == "SIGINT" and sig == signal.SIGINT:
            self.returncode = self._exit_code

    def terminate(self) -> None:
        self.signals.append(signal.SIGTERM)
        if self._exits_on == "SIGTERM":
            self.returncode = self._exit_code

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        # Block while returncode is None so wait_for(timeout) times out during SIGINT/SIGTERM escalation.
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return self.returncode


def _recorder(proc: _FakeProc) -> dr.DisplayRecorder:
    from skyvern.webeye.browser_artifacts import VideoArtifact

    return dr.DisplayRecorder(
        display=":99",
        owner_id="wr_x",
        process=proc,
        lock_fd=-1,
        video_artifact=VideoArtifact(video_path="/video/wr_x.mp4"),
    )


@pytest.mark.asyncio
async def test_stop_graceful_on_sigint() -> None:
    proc = _FakeProc(exits_on="SIGINT")
    rec = _recorder(proc)
    assert await rec.stop() is True
    assert signal.SIGINT in proc.signals
    assert not proc.killed
    assert rec.is_stopped


@pytest.mark.asyncio
async def test_stop_reports_nonzero_bridge_exit_as_not_graceful() -> None:
    # A prompt SIGINT exit with a NONZERO code (failed finalize) must report not-graceful, not masked as success.
    proc = _FakeProc(exits_on="SIGINT", exit_code=1)
    rec = _recorder(proc)
    assert await rec.stop() is False
    assert signal.SIGINT in proc.signals
    assert not proc.killed  # it DID exit; escalation should not have force-killed


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    proc = _FakeProc(exits_on="SIGINT")
    rec = _recorder(proc)
    first = await rec.stop()
    n_signals = len(proc.signals)
    second = await rec.stop()  # must short-circuit, not re-signal
    assert first == second
    assert len(proc.signals) == n_signals


@pytest.mark.asyncio
async def test_stop_owner_sweep_finishes_then_reraises_cancellation(monkeypatch) -> None:
    # Finish-then-re-raise (mirrors stop()/_reap/_reclaim): a per-owner release that COMPLETES then raises
    # CancelledError must neither abort nor swallow the sweep — the remaining entry is still released and the
    # caller's cancellation surfaces after the sweep, not silently discarded.
    r1 = _recorder(_FakeProc(exits_on="never"))
    r2 = _recorder(_FakeProc(exits_on="never"))
    r2.display = ":100"
    dr._REGISTRY[(":99", "wr_x")] = r1
    dr._REGISTRY[(":100", "wr_x")] = r2

    async def _fake_release(rec: dr.DisplayRecorder) -> bool:
        rec._stop_result = True  # the shielded stop completed
        dr._REGISTRY.pop((rec.display, rec.owner_id), None)  # ...and deregistered
        if rec is r1:
            raise asyncio.CancelledError()  # ...then the caller's cancel re-raises after the finish
        return True

    monkeypatch.setattr(dr, "release_display_recorder", _fake_release)
    try:
        with pytest.raises(asyncio.CancelledError):
            await dr.stop_display_recorders_for_owner("wr_x")
        assert not any(key[1] == "wr_x" for key in dr._REGISTRY)  # BOTH released despite the mid-sweep cancel
    finally:
        dr._REGISTRY.pop((":99", "wr_x"), None)
        dr._REGISTRY.pop((":100", "wr_x"), None)


# Leader: force-kill the whole GROUP (bridge + ffmpeg child) so ffmpeg never orphans. Non-leader: NEVER killpg
# (would hit the worker's own group) — fall back to a single-process kill. Both branches stay distinct rows.
@pytest.mark.parametrize(
    "getpgid_of,expected_killpg,expected_kill",
    [(lambda pid: pid, 1, 0), (lambda pid: 1, 0, 1)],
    ids=["bridge_is_leader_group_kill", "not_leader_falls_back"],
)
def test_kill_process_group_leader_vs_fallback(monkeypatch, getpgid_of, expected_killpg, expected_kill) -> None:
    proc = _FakeProc(exits_on="never")
    proc.pid = 4321
    calls = {"killpg": [], "kill": 0}
    monkeypatch.setattr(dr.os, "getpgid", getpgid_of)
    monkeypatch.setattr(dr.os, "killpg", lambda pgid, sig: calls["killpg"].append((pgid, sig)))
    monkeypatch.setattr(proc, "kill", lambda: calls.__setitem__("kill", calls["kill"] + 1))
    dr._kill_process_group(proc)
    assert len(calls["killpg"]) == expected_killpg
    assert calls["kill"] == expected_kill
    if expected_killpg:
        assert calls["killpg"] == [(4321, signal.SIGKILL)]  # SIGKILL to the group, not a single process


@pytest.mark.asyncio
async def test_stop_escalates_to_kill_when_process_never_exits(monkeypatch) -> None:
    monkeypatch.setattr(dr, "SIGINT_TIMEOUT", 0.05)  # shrink escalation timeouts so the test is fast
    monkeypatch.setattr(dr, "SIGTERM_TIMEOUT", 0.05)
    proc = _FakeProc(exits_on="never")
    rec = _recorder(proc)
    assert await rec.stop() is False
    assert proc.killed  # SIGINT -> SIGTERM -> kill escalation reached the reap


def test_arm_capture_sends_sigusr1_exactly_once() -> None:
    proc = _FakeProc(exits_on="never")
    rec = _recorder(proc)
    assert rec.arm_capture() is True
    assert proc.signals == [signal.SIGUSR1]
    assert rec.arm_capture() is True  # idempotent: no second delivery
    assert proc.signals == [signal.SIGUSR1]


def test_arm_capture_noops_on_exited_bridge() -> None:
    proc = _FakeProc(exits_on="never")
    proc.returncode = 0  # bridge already gone
    rec = _recorder(proc)
    assert rec.arm_capture() is False
    assert proc.signals == []


def test_arm_capture_failed_send_does_not_latch_and_retries() -> None:
    class _RaiseOnce:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.calls = 0

        def send_signal(self, sig: int) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient")

    proc = _RaiseOnce()
    rec = _recorder(proc)  # type: ignore[arg-type]
    assert rec.arm_capture() is False  # first send raised -> not armed
    assert rec._armed is False
    assert rec.arm_capture() is True  # retry succeeds
    assert proc.calls == 2
    assert rec._armed is True


def test_arm_capture_is_ownership_neutral() -> None:
    proc = _FakeProc(exits_on="never")
    rec = _recorder(proc)
    before_lock = rec.lock_fd
    before_registry = dict(dr._REGISTRY)
    rec.arm_capture()
    assert rec.lock_fd == before_lock  # no flock change
    assert dict(dr._REGISTRY) == before_registry  # no registry / ownership change


def test_trampoline_blocks_sigusr1_before_exec() -> None:
    """S4: the PDEATHSIG trampoline must block SIGUSR1 before execvp so an arm during the exec window stays
    pending (delivered after the bridge installs its handler) rather than default-killing the bridge."""
    src = dr._PDEATHSIG_TRAMPOLINE
    assert "pthread_sigmask" in src and "SIG_BLOCK" in src and "SIGUSR1" in src
    assert src.index("SIG_BLOCK") < src.index("execvp")


def _install_acquire_stubs(monkeypatch, proc: _FakeProc, closed: list[int]) -> None:
    monkeypatch.setattr(dr.os, "open", lambda *a, **k: 7)
    monkeypatch.setattr(dr.fcntl, "flock", lambda *a, **k: None)
    monkeypatch.setattr(dr.os, "close", lambda fd: closed.append(fd))

    async def _fake_exec(*_a: object, **_k: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(dr.asyncio, "create_subprocess_exec", _fake_exec)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack, ack_returncode, expect_acquired, expect_killed",
    [
        (b"READY\n", None, True, False),  # exact READY, live child -> acquired, Playwright args popped
        (b"", 1, False, False),  # EOF + nonzero rc (early exit pre-ACK) -> refusal, dead child not re-killed
        (b"garbage\n", None, False, True),  # wrong line, live child -> refusal + group kill
        (b"READY\n", 0, False, False),  # READY but already exited (live-child guard, R3) -> refusal, not killed
        ("wedge", None, False, True),  # no line before the deadline (wedged pre-loop) -> refusal + group kill
    ],
    ids=["ready", "early_exit_eof", "wrong_line", "exit_after_ack", "wedged_timeout"],
)
async def test_acquire_readiness_gates_playwright_fallback(
    monkeypatch, tmp_path, ack, ack_returncode, expect_acquired, expect_killed
) -> None:
    # One outcome table over the READY-pipe protocol, asserted at the prepare entry point so the Playwright-args
    # outcome is pinned: only an exact READY line from a still-live child acquires (args popped); every other
    # outcome fails closed (args intact, nothing registered, lock released, a live child force-killed).
    if dr.fcntl is None:
        pytest.skip("fcntl unavailable on this platform")
    from skyvern.webeye.browser_artifacts import BrowserArtifacts

    _gate_conc1(monkeypatch)  # eligible: Linux, DISPLAY=:99, concurrency 1
    monkeypatch.setattr(settings, "VIDEO_PATH", str(tmp_path))
    monkeypatch.setattr(dr, "STARTUP_ACK_TIMEOUT", 0.1)  # wedged case pays this once
    closed: list[int] = []
    proc = _FakeProc(exits_on="never", ack=ack, ack_returncode=ack_returncode)
    _install_acquire_stubs(monkeypatch, proc, closed)

    args = _args_with_playwright_recording()
    artifacts = BrowserArtifacts()
    key = (":99", "wr_ready")
    try:
        await dr.prepare_local_display_recording(args, artifacts, workflow_run_id="wr_ready")
        if expect_acquired:
            assert "record_video_dir" not in args and "record_video_size" not in args  # popped: recorder owns MP4
            assert artifacts._display_recorder is not None
            assert dr._REGISTRY.get(key) is not None
        else:
            assert args["record_video_dir"] == "/tmp/vid"  # Playwright per-page recording preserved
            assert artifacts._display_recorder is None
            assert key not in dr._REGISTRY  # nothing registered on refusal
            assert 7 in closed  # lock fd released
            assert proc.killed is expect_killed  # live child force-killed; an already-exited child is not
    finally:
        dr._REGISTRY.pop(key, None)


@pytest.mark.asyncio
async def test_acquire_cancel_during_readiness_reraises_after_cleanup(monkeypatch, tmp_path) -> None:
    # A cancel delivered while awaiting the READY line flows through the existing BaseException path: kill the
    # group, reap, close the lock, then re-raise CancelledError. No recorder is registered.
    if dr.fcntl is None:
        pytest.skip("fcntl unavailable on this platform")
    closed: list[int] = []
    proc = _FakeProc(exits_on="never", ack="wedge")
    _install_acquire_stubs(monkeypatch, proc, closed)

    task = asyncio.ensure_future(dr.acquire_display_recorder(":99", "wr_cancel", tmp_path))
    await asyncio.sleep(0.02)  # let it reach the readline await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 7 in closed  # lock released
    assert proc.killed is True  # child force-killed and reaped
    assert (":99", "wr_cancel") not in dr._REGISTRY


@pytest.mark.asyncio
async def test_acquire_timeout_reap_swallowed_cancel_reraises(monkeypatch, tmp_path) -> None:
    # If a cancel is swallowed DURING the post-timeout kill-reap, acquire must still re-raise CancelledError
    # (cancellation wins) rather than returning a fallback — after closing the lock.
    if dr.fcntl is None:
        pytest.skip("fcntl unavailable on this platform")
    closed: list[int] = []
    proc = _FakeProc(exits_on="never", ack="wedge")
    _install_acquire_stubs(monkeypatch, proc, closed)
    monkeypatch.setattr(dr, "STARTUP_ACK_TIMEOUT", 0.05)

    async def _reap_swallows_cancel(_p: object) -> bool:
        return True  # a cancel landed during the reap and was absorbed by the finish-then-reraise contract

    monkeypatch.setattr(dr, "_reap_surviving_cancellation", _reap_swallows_cancel)

    with pytest.raises(asyncio.CancelledError):
        await dr.acquire_display_recorder(":99", "wr_reapcancel", tmp_path)
    assert 7 in closed
    assert (":99", "wr_reapcancel") not in dr._REGISTRY


@pytest.mark.asyncio
async def test_acquire_kills_real_wedged_subprocess_and_falls_back(monkeypatch, tmp_path) -> None:
    # Real OS process that never prints READY: acquire must time out, kill the process group, reap, and refuse
    # (no recorder), proving the group-kill + reap works against a live child, not just the fake double.
    if dr.fcntl is None or os.name != "posix":
        pytest.skip("posix + fcntl required")
    monkeypatch.setattr(dr, "STARTUP_ACK_TIMEOUT", 0.3)

    def _wedged_cmd(*_a: object, **_k: object) -> tuple[list[str], Path]:
        return [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path / "wr_real.mp4"

    monkeypatch.setattr(dr, "build_capture_command", _wedged_cmd)

    key = (":99", "wr_real")
    try:
        acq = await dr.acquire_display_recorder(":99", "wr_real", tmp_path)
        assert acq.recorder is None and acq.started is False  # wedged real child -> refusal
        assert key not in dr._REGISTRY
    finally:
        dr._REGISTRY.pop(key, None)
