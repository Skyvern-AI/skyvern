"""Contract tests for the change-driven capture bridge command builders and pipe-write safety.

These guard the exact encode/reliability contract the recorder depends on: single-pass H.264 fragmented
MP4 (no second transcode), wall-clock VFR, bounded keyframe/fragment cadence, flushed packets, NO faststart
(crash-salvage), no-scale when capture==output, and short-write/EINTR-tolerant frame writes.
"""

import pytest

from skyvern.webeye import display_capture_bridge as bridge


def _ff(no_scale: bool = True, **overrides):
    kwargs = {
        "pix_fmt": "bgra",
        "input_width": 1280 if no_scale else 1920,
        "input_height": 720 if no_scale else 1080,
        "output_width": 1280,
        "output_height": 720,
        "max_fps": 15,
        "crf": 28,
        "keyframe_seconds": 5,
        "output_path": "/tmp/out.mp4",
    }
    kwargs.update(overrides)
    return bridge.build_ffmpeg_command(**kwargs)


def test_ffmpeg_is_single_pass_h264_fragmented_mp4_no_faststart() -> None:
    cmd = " ".join(_ff(no_scale=True))
    assert "-c:v libx264" in cmd
    assert "-preset ultrafast" in cmd and "-tune zerolatency" in cmd
    assert "-use_wallclock_as_timestamps 1" in cmd  # wall-clock timeline
    assert "-fps_mode vfr" in cmd  # change-driven VFR, not fixed-rate
    assert "+frag_keyframe" in cmd and "empty_moov" in cmd
    assert "-flush_packets 1" in cmd  # fragments hit disk as they close
    assert "faststart" not in cmd  # would defer moov to close-time rewrite -> unusable on abrupt kill
    assert cmd.endswith("/tmp/out.mp4")
    assert _ff(no_scale=True).count("-c:v") == 1


def test_ffmpeg_keyframe_and_crf_are_wired_from_params() -> None:
    joined = " ".join(_ff(crf=40, keyframe_seconds=3))
    assert "-crf 40" in joined
    assert "expr:gte(t,n_forced*3)" in joined


def test_fragment_flush_is_decoupled_from_keyframe_cadence() -> None:
    # Fixed 1s frag-flush INDEPENDENT of the keyframe interval, so an early per-step read hits a decodable fragment.
    for kf in (1, 3, 5, 30):
        joined = " ".join(_ff(keyframe_seconds=kf))
        assert "-frag_duration 1000000" in joined  # fixed 1s flush, regardless of keyframe interval
        assert f"expr:gte(t,n_forced*{kf})" in joined  # keyframe cadence preserved (compression unchanged)


def test_ffmpeg_scale_only_when_capture_differs_from_output() -> None:
    assert "scale=1280:720" not in " ".join(_ff(no_scale=True))
    assert "format=yuv420p" in " ".join(_ff(no_scale=True))
    scaled = " ".join(_ff(no_scale=False))
    assert "scale=1280:720,format=yuv420p" in scaled  # 1920x1080 -> 1280x720


def test_build_bridge_command_targets_the_module_with_geometry_and_profile() -> None:
    argv = bridge.build_bridge_command(
        display=":99",
        output_path="/v/run.mp4",
        input_width=1920,
        input_height=1080,
        output_width=1280,
        output_height=720,
        max_fps=15,
        crf=28,
        keyframe_seconds=5,
        stats_path="/v/run.stats.json",
        python_executable="/usr/bin/python3",
    )
    assert argv[0] == "/usr/bin/python3"
    assert argv[1:3] == ["-m", "skyvern.webeye.display_capture_bridge"]
    joined = " ".join(argv)
    assert "--display :99" in joined
    assert "--size 1920x1080" in joined and "--out-size 1280x720" in joined
    assert "--max-fps 15" in joined and "--crf 28" in joined and "--keyframe-sec 5" in joined
    assert "--out /v/run.mp4" in joined and "--stats /v/run.stats.json" in joined


def test_write_all_handles_short_writes_and_eintr(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"x" * 100
    calls = {"n": 0}

    def fake_write(fd: int, data) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()  # EINTR: must be retried, not counted as progress
        if calls["n"] == 2:
            return 30  # short write
        return len(bytes(data))  # remainder

    monkeypatch.setattr(bridge.os, "write", fake_write)
    written = bridge._write_all(7, payload)
    assert written == 100  # full payload delivered despite EINTR + short write


def test_write_all_reports_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_write(fd: int, data) -> int:
        raise BrokenPipeError()

    monkeypatch.setattr(bridge.os, "write", fake_write)
    assert bridge._write_all(7, b"abc") == -1


def test_shmat_failure_detection() -> None:
    # shmat returns (void*)-1 on failure; ctypes may surface it as None, 0, or the all-ones mask.
    assert bridge._shmat_failed(None) is True
    assert bridge._shmat_failed(0) is True
    assert bridge._shmat_failed(-1) is True
    assert bridge._shmat_failed(0xFFFFFFFFFFFFFFFF) is True
    assert bridge._shmat_failed(0x7F1234560000) is False  # a plausible mapped address


def test_stride_consistency_guard() -> None:
    assert bridge._stride_consistent(1280 * 4, 1280, 32) is True  # deployed depth-24 == 32bpp packed
    assert bridge._stride_consistent(1280 * 3, 1280, 24) is True  # packed 24bpp
    assert bridge._stride_consistent(1280 * 4, 1280, 24) is False  # padded stride -> fail closed
    assert bridge._stride_consistent(1284 * 3, 1280, 24) is False  # row padding -> fail closed


def test_fatal_exit_codes_are_distinct() -> None:
    codes = [
        bridge.EXIT_OPEN_DISPLAY,
        bridge.EXIT_NO_XDAMAGE,
        bridge.EXIT_UNSUPPORTED_BPP,
        bridge.EXIT_NO_XSHM,
        bridge.EXIT_XSHMCREATEIMAGE,
        bridge.EXIT_SHMGET,
        bridge.EXIT_SHMAT,
        bridge.EXIT_XSHMATTACH,
        bridge.EXIT_READINESS,
        bridge.EXIT_STRIDE_MISMATCH,
    ]
    assert len(set(codes)) == len(codes)  # each failure branch is attributable
    assert all(c != 0 for c in codes)


# Two-phase arm capture (SKY-15466): no frames until the parent arms; the arm captures the ready page as frame zero.
@pytest.mark.parametrize(
    "arm_consumed,pending_dirty,interval_ok,expected",
    [
        (False, True, True, False),
        (True, True, True, True),
        (True, False, True, False),
        (True, True, False, False),
    ],
    ids=["pre_arm_no_emit", "post_arm_emits", "no_pending", "interval_not_ok"],
)
def test_should_emit_truth_table(arm_consumed: bool, pending_dirty: bool, interval_ok: bool, expected: bool) -> None:
    assert (
        bridge.should_emit(arm_consumed=arm_consumed, pending_dirty=pending_dirty, interval_ok=interval_ok) is expected
    )


def test_arm_transition_fires_once_then_is_idempotent() -> None:
    assert bridge.arm_transition(armed=True, arm_consumed=False) is True
    assert bridge.arm_transition(armed=True, arm_consumed=True) is False
    assert bridge.arm_transition(armed=False, arm_consumed=False) is False


def test_terminal_frame_only_when_content_captured() -> None:
    assert bridge.should_write_terminal(0) is False
    assert bridge.should_write_terminal(1) is True


def test_zero_frame_unlink_and_return_code_truth() -> None:
    # Zero-frame teardown unlinks the header-only output; rc 0 ONLY for an unarmed clean teardown, else nonzero.
    assert bridge.should_unlink_output(0) is True
    assert bridge.should_unlink_output(1) is False
    assert bridge._finalize_return_code(0, None, arm_consumed=False) == 0  # UNARMED + 0 -> clean
    assert bridge._finalize_return_code(0, 0, arm_consumed=True) == 1  # ARMED + 0 -> capture failure
    assert bridge._finalize_return_code(0, None, arm_consumed=True) == 1
    assert bridge._finalize_return_code(5, 0, arm_consumed=True) == 0
    assert bridge._finalize_return_code(5, 1, arm_consumed=True) == 1
    assert bridge._finalize_return_code(5, None, arm_consumed=True) == 1


def test_unblock_precedes_ffmpeg_spawn_in_source() -> None:
    """S4: the SIGUSR1 handler is installed and unblocked before the ffmpeg Popen, so an early arm can never
    default-kill the bridge and no frame is written before ffmpeg exists."""
    import inspect

    src = inspect.getsource(bridge._run)
    assert "SIG_UNBLOCK" in src and "subprocess.Popen(ff" in src
    assert src.index("SIG_UNBLOCK") < src.index("subprocess.Popen(ff")
    # SIGINT/SIGTERM stop handlers are installed only AFTER the ffmpeg spawn, so a parent death / stop during
    # X/SHM/FFmpeg init keeps default fast termination instead of merely setting a flag while init may hang.
    assert src.index("subprocess.Popen(ff") < src.index("signal.SIGINT")


def test_ready_ack_follows_setup_and_precedes_loop_in_source() -> None:
    """Positive readiness: the parent accepts the recorder only on the bridge's single stdout READY line, so that
    line must be emitted AFTER the FFmpeg spawn and the SIGINT/SIGTERM handler install, and immediately BEFORE the
    main capture loop — every known pre-loop wedge point cleared first. Diagnostics stay on stderr."""
    import inspect

    src = inspect.getsource(bridge._run)
    assert src.count('print("READY"') == 1  # exactly one stdout ACK write
    ready = src.index('print("READY"')
    assert src.index("subprocess.Popen(ff") < ready  # after the FFmpeg spawn
    assert src.index("signal.SIGTERM") < ready  # after the graceful-stop signal handlers
    assert ready < src.index("while not stop")  # immediately before the main capture loop
    assert "flush=True" in src[ready : ready + 40] and "file=" not in src[ready : ready + 40]  # flushed, stdout


def test_xpending_drain_is_direct_child_of_main_loop_source_wiring() -> None:
    # Source-wiring (Codex P2 3951275783): the XPending drain must be a direct child of the main `while not stop`
    # loop (drained after every select return), NOT nested under `if r`, with XDamageSubtract gated by saw_damage.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(bridge._run)))
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.While) and "not stop" in ast.unparse(n.test))

    def _xpending(n: ast.AST) -> bool:
        return isinstance(n, ast.While) and "XPending" in ast.unparse(n.test)

    assert len([s for s in loop.body if _xpending(s)]) == 1  # one unconditional drain; `if r` nesting drops it to 0
    if_r = [s for s in loop.body if isinstance(s, ast.If) and isinstance(s.test, ast.Name)]
    assert not any(_xpending(d) for s in if_r for d in ast.walk(s))  # no bare `if r:` wraps the drain
    saw = [s for s in loop.body if isinstance(s, ast.If) and "saw_damage" in ast.unparse(s.test)]
    assert len(saw) == 1 and "XDamageSubtract" in ast.unparse(saw[0])  # subtract gated by observed damage


def test_rescan_that_arms_new_windows_marks_dirty() -> None:
    """Source-coupled WIRING assertion (Codex 3939710637): a periodic rescan that newly arms window(s) must set
    pending_dirty, or a popup/native dialog that painted before the rescan (no later XDamage) is never captured
    by the FPS-coalesced emit. Loop is a ctypes closure, so the wiring is asserted at the source seam."""
    import inspect

    src = inspect.getsource(bridge._run)
    branch = src[src.index("if scan_and_arm():") : src.index("last_rescan = now")]
    assert "pending_dirty = True" in branch


def test_scan_and_arm_flushes_pending_errors_before_snapshotting() -> None:
    """scan_and_arm drains the prior loop's XDamageSubtract batch (an XSync) BEFORE it snapshots err["n"] and
    calls XDamageCreate, so a since-destroyed window's BadDamage is never charged to a valid window's create."""
    import inspect

    src = inspect.getsource(bridge._run)
    start = src.index("def scan_and_arm")
    body = src[start : src.index("\n    fd = X.XConnectionNumber", start)]
    flush = body.index("X.XSync(dpy, 0)")
    assert flush < body.index('before = err["n"]')
    assert flush < body.index("XD.XDamageCreate(")


def test_pixel_heuristics_are_retired() -> None:
    # Retired pixel/color gate symbols must stay gone (representative function + constant).
    for name in ("should_discard", "_GATE_GRID"):
        assert not hasattr(bridge, name)


# --- SKY-15708: stale per-window XDamage handle pruning ------------------------------------------------------------


class _FakeXD:
    """Records XDamageDestroy calls; can simulate an async X error (BadDamage or unrelated) surfaced on the
    NEXT XSync, mirroring how the real server queues a per-request error the swallowing handler bumps."""

    def __init__(self, *, err: dict | None = None, error_code_on_destroy: int | None = None):
        self._err = err
        self._error_code = error_code_on_destroy
        self.destroyed: list[int] = []

    def XDamageDestroy(self, dpy, dh):
        self.destroyed.append(dh)
        if self._err is not None and self._error_code is not None:
            self._err["n"] += 1
            self._err["code"] = self._error_code


class _FakeXSync:
    def XSync(self, dpy, discard):  # the destroy path flushes to surface a queued error; fakes bump on destroy
        return None


# Requirement 1: prune destroyed AND unmapped child windows by viewability (not mere tree membership), keep root.


def test_windows_to_prune_drops_non_viewable_children_and_keeps_root() -> None:
    root = 0x1
    # 0x222 = destroyed (absent from viewable), 0x333 = unmapped-but-live (present in tree, not viewable): both prune.
    assert bridge.windows_to_prune({root, 0x111, 0x222, 0x333}, viewable={root, 0x111}, root=root) == [0x222, 0x333]


def test_windows_to_prune_never_returns_root_even_if_not_viewable() -> None:
    root = 0x1
    assert bridge.windows_to_prune({root, 0x111}, viewable=set(), root=root) == [0x111]  # root retained, child pruned


def test_prune_unviewable_destroys_and_drops_exactly_once_keeping_root_and_viewable() -> None:
    err = {"n": 0, "code": None}
    root = 0x1
    damaged = {root: 100, 0x111: 101, 0x222: 102, 0x333: 103}  # root + viewable child + destroyed + unmapped
    xd = _FakeXD()
    pruned = bridge.prune_unviewable_windows(
        xd, _FakeXSync(), dpy=1, damaged=damaged, viewable={root, 0x111}, root=root, err=err, bad_damage_code=152
    )
    assert pruned == 2
    assert damaged == {root: 100, 0x111: 101}  # root + viewable child retained; destroyed + unmapped dropped
    assert sorted(xd.destroyed) == [102, 103]  # each non-viewable handle destroyed exactly once


# Requirement 2: a DestroyNotify drops the entry immediately (before any rescan), so a same-XID reuse re-arms fresh.


def test_reconcile_destroyed_drops_entry_immediately_without_reissuing_destroy() -> None:
    root = 0x1
    damaged = {root: 100, 0x555: 101}
    reconciled = bridge.reconcile_destroyed_window(damaged, 0x555, root)
    assert reconciled is True
    assert 0x555 not in damaged  # dropped at DestroyNotify time -> a reused 0x555 fails `w not in damaged` -> re-arms
    assert damaged == {root: 100}


def test_reconcile_destroyed_is_noop_for_root_and_untracked_windows() -> None:
    root = 0x1
    damaged = {root: 100, 0x555: 101}
    assert bridge.reconcile_destroyed_window(damaged, root, root) is False  # never drop the root
    assert bridge.reconcile_destroyed_window(damaged, 0x999, root) is False  # untracked -> no-op
    assert damaged == {root: 100, 0x555: 101}


# Requirement 3: only the exact BadDamage code is tolerated; unknown or unreadable codes keep a scoped diagnostic.


def test_destroy_owned_handle_tolerates_exact_bad_damage_without_warning(capsys: pytest.CaptureFixture[str]) -> None:
    err = {"n": 0, "code": None}
    xd = _FakeXD(err=err, error_code_on_destroy=152)  # server already reaped the window -> BadDamage
    bridge._destroy_owned_handle(xd, _FakeXSync(), 1, 101, 0x222, err, bad_damage_code=152, context="unviewable")
    assert "unexpected X error" not in capsys.readouterr().err  # the one tolerated race


def test_destroy_owned_handle_surfaces_unknown_code(capsys: pytest.CaptureFixture[str]) -> None:
    err = {"n": 0, "code": None}
    xd = _FakeXD(err=err, error_code_on_destroy=9)  # unrelated code -> must not be masked
    bridge._destroy_owned_handle(xd, _FakeXSync(), 1, 101, 0x222, err, bad_damage_code=152, context="unviewable")
    assert "unexpected X error" in capsys.readouterr().err


def test_destroy_owned_handle_surfaces_unreadable_none_code_not_silently_tolerated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A struct-read failure leaves code == None; it must be surfaced, NOT silently tolerated as if it were BadDamage.
    err = {"n": 0, "code": None}

    class _ErrBumpNoCode:
        def XDamageDestroy(self, dpy, dh):
            err["n"] += 1  # an error occurred but its code could not be read (stays None)

    bridge._destroy_owned_handle(_ErrBumpNoCode(), _FakeXSync(), 1, 101, 0x222, err, bad_damage_code=152, context="x")
    assert "unexpected X error" in capsys.readouterr().err


def test_destroy_owned_handle_silent_when_no_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = {"n": 0, "code": None}
    bridge._destroy_owned_handle(_FakeXD(), _FakeXSync(), 1, 101, 0x222, err, bad_damage_code=152, context="x")
    assert capsys.readouterr().err == ""  # a clean destroy emits nothing


def test_destroy_all_handles_destroys_every_owned_handle_once_including_root() -> None:
    damaged = {0x1: 100, 0x111: 101, 0x222: 102}  # root + children
    xd = _FakeXD()
    bridge._destroy_all_handles(xd, _FakeXSync(), dpy=1, damaged=damaged)
    assert damaged == {}  # nothing left to leak
    assert sorted(xd.destroyed) == [100, 101, 102]  # each owned handle destroyed exactly once (pop-before-destroy)


# Wiring guards for the ctypes `_run` closure (not unit-executable without a real X server; the real-X11 churn e2e
# is the executing contract test). Kept minimal — behavior is covered by the helper tests above.


def test_bindings_for_map_state_and_destroy_are_declared() -> None:
    import inspect

    src = inspect.getsource(bridge._load_x)
    assert "XD.XDamageDestroy" in src  # destroy entrypoint bound
    assert "XGetWindowAttributes" in src  # map-state query bound (to prune unmapped windows, not just absent ones)


def test_scan_and_arm_prunes_by_viewability_before_arming() -> None:
    import inspect

    src = inspect.getsource(bridge._run)
    start = src.index("def scan_and_arm")
    body = src[start : src.index("\n    fd = X.XConnectionNumber", start)]
    assert "prune_unviewable_windows" in body  # prune destroyed + unmapped before re-arm
    assert body.index("prune_unviewable_windows") < body.index("XD.XDamageCreate(")


def test_both_xpending_drains_reconcile_destroy_notify() -> None:
    # F1 regression: BOTH event drains — the main loop drain AND the one-shot arm-transition drain — must reconcile
    # DestroyNotify. A destroy queued in the arm window that the arm drain merely discards would leave a stale entry
    # that a same-XID reuse then inherits until the window departs (the exact XID-reuse failure mode).
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(bridge._run)))
    drains = [n for n in ast.walk(tree) if isinstance(n, ast.While) and "XPending" in ast.unparse(n.test)]
    assert len(drains) == 2  # main-loop drain + arm-transition drain
    for drain in drains:
        assert "reconcile_destroyed_window" in ast.unparse(drain)  # every drain path drops destroyed windows


def test_owned_handles_cleaned_in_finally_for_exceptional_exit() -> None:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(bridge._run)))
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and "_destroy_all_handles" in ast.unparse(n.finalbody)]
    assert tries, "_destroy_all_handles must run in a finally so early/exceptional exits still release owned handles"


def test_x_error_handler_captures_error_code() -> None:
    import inspect

    src = inspect.getsource(bridge._run)
    assert 'err["code"]' in src  # the handler records the error code so BadDamage can be told from unrelated errors
    assert hasattr(bridge, "_XErrorEvent")  # a typed error-event struct to read error_code from


class _FakeLibc:
    def __init__(self, *, shmid: int = 42, shmat_addr: int = 0x7F00_0000):
        self._shmid = shmid
        self._shmat_addr = shmat_addr
        self.calls: list[tuple] = []

    def shmget(self, key, size, flags):
        self.calls.append(("shmget", size))
        return self._shmid

    def shmat(self, shmid, addr, flags):
        self.calls.append(("shmat", shmid))
        return self._shmat_addr

    def shmdt(self, addr):
        self.calls.append(("shmdt", addr))

    def shmctl(self, shmid, cmd, buf):
        self.calls.append(("shmctl", shmid, cmd))


class _FakeXext:
    def __init__(self, *, getimage_ok: int = 1):
        self._getimage_ok = getimage_ok
        self.calls: list[str] = []

    def XShmAttach(self, dpy, info):
        self.calls.append("XShmAttach")

    def XShmDetach(self, dpy, info):
        self.calls.append("XShmDetach")

    def XShmGetImage(self, dpy, root, image, x, y, planes):
        self.calls.append("XShmGetImage")
        return self._getimage_ok


class _FakeX:
    def __init__(self, err: dict, *, bump_err_on_sync: bool = False):
        self._err = err
        self._bump = bump_err_on_sync

    def XSync(self, dpy, discard):
        if self._bump:
            self._err["n"] += 1  # simulate an async XShmAttach BadAccess surfacing through the handler


def _shm_image_stub():
    from types import SimpleNamespace

    return SimpleNamespace(contents=SimpleNamespace(data=None))


def _run_attach(libc, xe, x, err):
    info = bridge._XShmSegmentInfo()  # real ctypes struct so ctypes.byref works
    return bridge._allocate_and_attach_shm(x, xe, libc, 1, 2, _shm_image_stub(), info, 1000, err)


def test_shm_attach_shmat_failure_reclaims_and_fails_closed() -> None:
    err = {"n": 0}
    libc = _FakeLibc(shmat_addr=0xFFFFFFFFFFFFFFFF)  # shmat == (void*)-1
    xe = _FakeXext()
    addr, code = _run_attach(libc, xe, _FakeX(err), err)
    assert addr is None and code == bridge.EXIT_SHMAT
    assert ("shmctl", 42, bridge.IPC_RMID) in libc.calls  # created segment reclaimed
    assert "XShmAttach" not in xe.calls  # never attached
    assert not any(c[0] == "shmdt" for c in libc.calls)  # nothing was mapped to detach


def test_shm_attach_failure_detaches_and_removes() -> None:
    err = {"n": 0}
    libc = _FakeLibc()
    xe = _FakeXext()
    addr, code = _run_attach(libc, xe, _FakeX(err, bump_err_on_sync=True), err)  # XShmAttach errors
    assert addr is None and code == bridge.EXIT_XSHMATTACH
    assert "XShmAttach" in xe.calls
    assert ("shmdt", 0x7F00_0000) in libc.calls
    assert ("shmctl", 42, bridge.IPC_RMID) in libc.calls


def test_shm_readiness_failure_detaches() -> None:
    err = {"n": 0}
    libc = _FakeLibc()
    xe = _FakeXext(getimage_ok=0)  # initial XShmGetImage fails
    addr, code = _run_attach(libc, xe, _FakeX(err), err)
    assert addr is None and code == bridge.EXIT_READINESS
    assert "XShmDetach" in xe.calls
    assert ("shmdt", 0x7F00_0000) in libc.calls


def test_shm_attach_success_returns_mapped_address() -> None:
    err = {"n": 0}
    libc = _FakeLibc()
    xe = _FakeXext()
    addr, code = _run_attach(libc, xe, _FakeX(err), err)
    assert code == 0 and addr == 0x7F00_0000
    assert xe.calls == ["XShmAttach", "XShmGetImage"]  # attached then readiness-probed
    # segment IPC_RMID-marked (freed on detach) but NOT detached while healthy
    assert ("shmctl", 42, bridge.IPC_RMID) in libc.calls
    assert not any(c[0] == "shmdt" for c in libc.calls)
