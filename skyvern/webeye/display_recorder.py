from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import re
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import structlog

from skyvern.config import settings
from skyvern.webeye import attach_only
from skyvern.webeye.browser_artifacts import VideoArtifact

try:
    import fcntl
except ImportError:
    # fcntl is POSIX-only. This module is part of the OSS `skyvern/` import surface, which must import
    # cleanly on non-POSIX platforms; whole-display recording only ever runs on the Linux worker family,
    # so acquisition fails closed when fcntl is unavailable.
    fcntl = None  # type: ignore[assignment]

LOG = structlog.get_logger()

SIGINT_TIMEOUT = 5.0
SIGTERM_TIMEOUT = 2.0
STARTUP_READINESS_TIMEOUT = 0.5

# x11grab captures the browser window rectangle at the display origin (--window-position=0,0). The Xvfb
# screen and the base --window-size are both 1920x1080, so an absent/unparseable/larger-than-screen window
# falls back to capturing the full screen. The output is bounded within 1280x720, aspect-preserved.
CAPTURE_DISPLAY_SIZE = (1920, 1080)
CAPTURE_OUTPUT_BOUND = (1280, 720)
CaptureSizes = tuple[tuple[int, int], tuple[int, int]]

_REGISTRY: dict[tuple[str, str], DisplayRecorder] = {}

# Parent-death via an exec'd python trampoline, NOT a fork-time preexec_fn (prctl/ctypes in the multithreaded
# worker's fork child can inherit locks it never owns and hang before exec). The parent passes its pid (argv[1]);
# the trampoline exits unless the live parent still matches it before AND after prctl (so a parent that dies
# pre-exec never binds PDEATHSIG to the reaper), then execvp's ffmpeg (pid kept; PDEATHSIG survives non-setuid exec).
_PDEATHSIG_TRAMPOLINE = (
    "import ctypes,os,signal,sys\n"
    "e=int(sys.argv[1])\n"
    "os.getppid()==e or os._exit(0)\n"
    "try:ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGTERM)\n"  # 1 == PR_SET_PDEATHSIG
    "except Exception:pass\n"
    "os.getppid()==e or os._exit(0)\n"
    "os.execvp(sys.argv[2],sys.argv[2:])\n"
)


def normalize_display(display: str) -> str:
    # Strip only the ".<screen>" that trails the ":<display>" segment; never split on a dot inside a
    # hostname (e.g. "host.example.com:0.0" -> "host.example.com:0", ":99.0" -> ":99").
    display = display.strip()
    head, sep, tail = display.rpartition(":")
    if not sep:
        return display
    return f"{head}:{tail.split('.', 1)[0]}"


def resolve_owner_id(
    *,
    owner_id_override: str | None = None,
    workflow_run_id: str | None = None,
    task_id: str | None = None,
    script_id: str | None = None,
    browser_session_id: str | None = None,
) -> str | None:
    # owner_id_override is FALLBACK-ONLY (used only when no canonical owner is present, e.g. a standalone
    # reconnect), so a stray/hostile override can never displace an explicit canonical owner and weaken isolation.
    return workflow_run_id or task_id or script_id or browser_session_id or owner_id_override


def configure_local_display_recording(
    browser_args: dict[str, object],
    *,
    owner_id_override: str | None = None,
    workflow_run_id: str | None = None,
    task_id: str | None = None,
    script_id: str | None = None,
    browser_session_id: str | None = None,
) -> bool:
    eligible = bool(
        platform.system() == "Linux"
        and settings.EXCLUSIVE_DISPLAY_RECORDING
        and os.environ.get("DISPLAY")
        and not attach_only.is_enforcing()
        and resolve_owner_id(
            owner_id_override=owner_id_override,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            script_id=script_id,
            browser_session_id=browser_session_id,
        )
    )
    if eligible:
        browser_args.pop("record_video_dir", None)
        browser_args.pop("record_video_size", None)
    return eligible


def unlink_recordings_for_owner(owner_id: str) -> int:
    """Best-effort, synchronous, never-raising removal of this owner's whole-display recordings.

    Recordings are written to ``VIDEO_PATH/<YYYY-MM-DD>/<_safe_owner_id(owner_id)>.webm``; the owner
    digest makes the filename derivable from the run's own identity, so a glob across date dirs is
    owner-exact — a sibling owner's digest and Playwright random-name videos never match — and survives
    a midnight date-dir rollover. Returns the number of files removed.
    """
    root = settings.VIDEO_PATH
    if not root:
        return 0
    removed = 0
    try:
        for path in Path(root).glob(f"*/{_safe_owner_id(owner_id)}.webm"):
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                LOG.warning("Failed to unlink whole-display recording", path=str(path), exc_info=True)
    except OSError:
        LOG.warning("Failed to scan for whole-display recordings to unlink", owner_id=owner_id, exc_info=True)
    return removed


def _safe_owner_id(owner_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", owner_id).strip("._")
    prefix = (sanitized or "run")[:48]
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _parse_window_size(browser_args: dict[str, object]) -> tuple[int, int] | None:
    args = browser_args.get("args")
    if not isinstance(args, list):
        return None
    resolved: str | None = None
    for arg in args:
        text = str(arg)
        if text.startswith("--window-size="):
            resolved = text  # the launch keeps one entry; take the last so a rewrite always wins
    if resolved is None:
        return None
    try:
        width_str, height_str = resolved.split("=", 1)[1].split(",", 1)
        return int(width_str), int(height_str)
    except (ValueError, IndexError):
        return None


def _even_floor(value: float) -> int:
    floored = int(value)
    return floored - (floored % 2)


def _bounded_even_output(width: int, height: int) -> tuple[int, int]:
    # Fit within CAPTURE_OUTPUT_BOUND, aspect-preserved and never upscaled. The binding dimension is chosen
    # by integer cross-multiplication (so it lands exactly on the bound, no float drift), the other derived
    # and floored to even — libvpx needs even dimensions and flooring never pads, crops, or distorts.
    max_w, max_h = CAPTURE_OUTPUT_BOUND
    if width * max_h >= height * max_w:
        out_w: float = min(width, max_w)
        out_h: float = out_w * height / width
    else:
        out_h = min(height, max_h)
        out_w = out_h * width / height
    return _even_floor(out_w), _even_floor(out_h)


def resolve_display_capture_sizes(browser_args: dict[str, object]) -> CaptureSizes:
    """Derive the immutable ((input_w, input_h), (output_w, output_h)) for x11grab from the FINAL, validated
    ``--window-size`` in ``browser_args`` (set at the launch seam, after any dynamic viewport override).

    The input is the browser window captured at the display origin; the output is that rectangle scaled to
    fit within 1280x720 with even dimensions and no padding, crop, or distortion. A missing, unparseable, or
    larger-than-display window falls back to the fixed 1920x1080 capture + 1280x720 output of the base launch.
    """
    window = _parse_window_size(browser_args)
    if window is None:
        return CAPTURE_DISPLAY_SIZE, CAPTURE_OUTPUT_BOUND
    width, height = window
    if not (0 < width <= CAPTURE_DISPLAY_SIZE[0] and 0 < height <= CAPTURE_DISPLAY_SIZE[1]):
        return CAPTURE_DISPLAY_SIZE, CAPTURE_OUTPUT_BOUND
    return (width, height), _bounded_even_output(width, height)


def build_ffmpeg_command(
    display: str, owner_id: str, video_dir: Path, capture_sizes: CaptureSizes | None = None
) -> tuple[list[str], Path]:
    output_path = video_dir / f"{_safe_owner_id(owner_id)}.webm"
    (input_w, input_h), (output_w, output_h) = capture_sizes or (CAPTURE_DISPLAY_SIZE, CAPTURE_OUTPUT_BOUND)
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "x11grab",
        "-video_size",
        f"{input_w}x{input_h}",
        "-framerate",
        "15",
        "-i",
        normalize_display(display),
        "-vf",
        f"scale={output_w}:{output_h}",
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
    return command, output_path


async def _reap_surviving_cancellation(process: asyncio.subprocess.Process) -> bool:
    """Await the child's reaping to completion even under repeated cancellation.

    ``asyncio.shield`` protects the wait from a single cancel, but a second cancel delivered while the
    shielded await is pending would still abort it and skip lock release. We loop on the SAME wait future,
    absorbing every cancel, and report whether any arrived so the caller re-raises exactly once — after it
    has released its resources.
    """
    waiter = asyncio.ensure_future(process.wait())
    cancelled = False
    while True:
        try:
            await asyncio.shield(waiter)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True


@dataclass(frozen=True)
class DisplayRecorderAcquisition:
    recorder: DisplayRecorder | None
    video_artifact: VideoArtifact | None
    started: bool


class DisplayRecorder:
    def __init__(
        self,
        *,
        display: str,
        owner_id: str,
        process: asyncio.subprocess.Process,
        lock_fd: int,
        video_artifact: VideoArtifact,
    ) -> None:
        self.display = normalize_display(display)
        self.owner_id = owner_id
        self.process = process
        self.lock_fd = lock_fd
        self.video_artifact = video_artifact
        self._stop_lock = asyncio.Lock()
        self._stop_result: bool | None = None
        # Set when the ffmpeg is stopped for upload but the display must stay fenced to this owner until a
        # later, separate teardown (a deferred live-view browser close). Keeps a stopped recorder from being
        # pruned/reaped as a plain dead entry, so a different owner cannot acquire the display in between.
        self._reserved = False

    @property
    def is_stopped(self) -> bool:
        """True once ``stop()`` has run to completion, so the WebM on disk is finalized and uploadable."""
        return self._stop_result is not None

    async def finalize_keeping_reservation(self) -> bool:
        """Stop the ffmpeg so the WebM is complete and uploadable, but keep the display lock and registry
        reservation. Used when a live-view close is deferred: the run's browser is still mapped on the
        display, so releasing the reservation now would let a different-owner run acquire it and capture the
        prior tenant's window. The reservation is released only at real browser teardown
        (``release_display_recorder``) or process death."""
        self._reserved = True
        return await self.stop(release_lock=False)

    @staticmethod
    def lock_path_for_display(display: str) -> str:
        safe_display = re.sub(r"[^A-Za-z0-9_-]", "_", normalize_display(display))
        return f"/tmp/skyvern-display-recording-{safe_display}.lock"

    def _release_lock(self) -> None:
        if self.lock_fd is not None and self.lock_fd >= 0:
            with suppress(OSError):
                os.close(self.lock_fd)
        self.lock_fd = -1

    async def stop(self, release_lock: bool = True) -> bool:
        async with self._stop_lock:
            if self._stop_result is not None:
                return self._stop_result
            graceful = self.process.returncode == 0
            pending_cancel = False
            try:
                graceful = await self._escalate_shutdown(graceful)
            except asyncio.CancelledError:
                pending_cancel = True
            finally:
                # Reap the child and (unless the display stays reserved) release the lock even under repeated
                # cancellation, so a cancelled stop never leaks ffmpeg or blocks the next run. The reap loop
                # absorbs further cancels; we re-raise exactly once, after lock release. A reserved recorder
                # keeps its lock — release_display_recorder frees it at the real browser teardown.
                if self.process.returncode is None:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    if await _reap_surviving_cancellation(self.process):
                        pending_cancel = True
                    graceful = False
                if release_lock:
                    self._release_lock()
                self._stop_result = graceful
            if pending_cancel:
                raise asyncio.CancelledError()
            return graceful

    async def _escalate_shutdown(self, graceful: bool) -> bool:
        if self.process.returncode is not None:
            return graceful
        self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=SIGINT_TIMEOUT)
            return True
        except TimeoutError:
            pass
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=SIGTERM_TIMEOUT)
        except TimeoutError:
            return False
        return graceful


def _prune_dead_entries(normalized_display: str, owner_id: str) -> None:
    # Drop exited recorders of a DIFFERENT owner on this display so a previous run's dead recorder never
    # fail-closes the next one. Same-owner is handled by reuse above; a live owner is left entirely alone.
    dead_keys = [
        key
        for key, rec in _REGISTRY.items()
        if key[0] == normalized_display
        and key[1] != owner_id
        and rec.process.returncode is not None
        and not rec._reserved
    ]
    for key in dead_keys:
        dead = _REGISTRY.pop(key)
        dead._release_lock()


async def acquire_display_recorder(
    display: str,
    owner_id: str,
    video_dir: Path,
    owner_id_override: str | None = None,
    capture_sizes: CaptureSizes | None = None,
) -> DisplayRecorderAcquisition:
    if fcntl is None:
        LOG.warning("Whole-display recording unavailable: fcntl not present on this platform", display=display)
        return DisplayRecorderAcquisition(None, None, False)

    normalized_display = normalize_display(display)
    key = (normalized_display, owner_id)

    existing = _REGISTRY.get(key)
    if existing is not None:
        # Same (display, owner): reuse the exact recorder + VideoArtifact (never restart a dead one — a fresh
        # ffmpeg -y would overwrite the partial WebM and mint a second RECORDING row); the partial uploads as-is.
        return DisplayRecorderAcquisition(existing, existing.video_artifact, False)

    _prune_dead_entries(normalized_display, owner_id)

    conflicting = next((rec for reg_key, rec in _REGISTRY.items() if reg_key[0] == normalized_display), None)
    if conflicting is not None:
        if owner_id_override is not None and conflicting.owner_id == owner_id_override:
            # A shared-state reconnect rebuilds under the aliased child id but carries its own live recorder's
            # owner as the override; re-adopt that exact recorder + artifact (same object, no restart).
            return DisplayRecorderAcquisition(conflicting, conflicting.video_artifact, False)
        LOG.error(
            "Whole-display recording refused because the display has a live owner",
            display=normalized_display,
            owner_id=owner_id,
            holder_owner_id=conflicting.owner_id,
        )
        return DisplayRecorderAcquisition(None, None, False)

    # Any failure below must fail closed WITHOUT leaking the lock fd and WITHOUT propagating into the
    # browser-creation path — a run whose recorder cannot start simply continues unrecorded.
    lock_fd: int | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        lock_fd = os.open(DisplayRecorder.lock_path_for_display(normalized_display), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            LOG.error("Whole-display recording refused because the display lock is held", display=normalized_display)
            return DisplayRecorderAcquisition(None, None, False)

        video_dir.mkdir(parents=True, exist_ok=True)
        command, output_path = build_ffmpeg_command(normalized_display, owner_id, video_dir, capture_sizes)
        # Capture intentionally stays WebM so interrupted recordings remain repairable/finalizable;
        # the existing upload preparation path handles MP4 conversion.
        prefix = [sys.executable, "-c", _PDEATHSIG_TRAMPOLINE, str(os.getpid())] if os.name == "posix" else []
        process = await asyncio.create_subprocess_exec(
            *prefix,
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Give ffmpeg a bounded window to fail its x11grab init before we seed a VideoArtifact and suppress
        # Playwright recording; wait_for returns the instant the child exits and times out (healthy) otherwise.
        with suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=STARTUP_READINESS_TIMEOUT)
        if process.returncode is not None:
            os.close(lock_fd)
            LOG.warning(
                "Whole-display recorder exited during startup",
                display=normalized_display,
                returncode=process.returncode,
            )
            return DisplayRecorderAcquisition(None, None, False)
    except BaseException as exc:
        cancelled_during_reap = False
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            cancelled_during_reap = await _reap_surviving_cancellation(process)
        if lock_fd is not None:
            with suppress(OSError):
                os.close(lock_fd)
        if not isinstance(exc, Exception):
            # A BaseException entry (the unwind was itself cancelled): cleanup is done, propagate it.
            raise
        if cancelled_during_reap:
            # A plain-exception unwind was cancelled while reaping; cleanup is complete, so surface the
            # cancellation instead of returning a fail-closed acquisition that would swallow it.
            raise asyncio.CancelledError()
        LOG.warning("Failed to start whole-display recorder", display=normalized_display, exc_info=True)
        return DisplayRecorderAcquisition(None, None, False)

    video_artifact = VideoArtifact(video_path=str(output_path))
    recorder = DisplayRecorder(
        display=normalized_display,
        owner_id=owner_id,
        process=process,
        lock_fd=lock_fd,
        video_artifact=video_artifact,
    )
    _REGISTRY[key] = recorder
    return DisplayRecorderAcquisition(recorder, video_artifact, True)


async def release_display_recorder(recorder: DisplayRecorder | None) -> bool:
    if recorder is None:
        return True
    # One stop task per release, awaited through repeated caller cancellation: the reservation/lock/registry
    # entry must never be freed until the serialized stop is terminal. A caller cancelled while parked behind
    # another release's _stop_lock would otherwise release mid-shutdown and expose the display to a new owner.
    stop_task = asyncio.ensure_future(recorder.stop())
    cancelled = False
    try:
        while True:
            try:
                return await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                cancelled = True
                if stop_task.done():
                    # The stop is terminal; the finally releases and then re-raises the cancellation, so this
                    # returned value is discarded — it only proves to the type checker that no path falls through.
                    return stop_task.result()
    finally:
        # This is the real browser-teardown release, so drop the reservation and free the display lock — but
        # only once the stop is terminal, even when ``stop()`` short-circuited on an already-finalized recorder.
        if recorder.is_stopped:
            recorder._reserved = False
            recorder._release_lock()
            key = (recorder.display, recorder.owner_id)
            # Only drop OUR entry: a delayed release of a superseded recorder must never evict a replacement
            # that a later same-owner acquisition registered under the same key.
            if _REGISTRY.get(key) is recorder:
                del _REGISTRY[key]
        if cancelled:
            raise asyncio.CancelledError()


async def stop_display_recorders_for_owner(owner_id: str) -> int:
    """Stop and de-register every live whole-display recorder this owner still holds.

    A run whose teardown never reached ``release_display_recorder`` (a Temporal ``CancelledError`` bypassing
    the ``except Exception`` cleanup, or a mid-run crash) leaves its ffmpeg, ``_REGISTRY`` entry, and lock
    live. Call this BEFORE unlinking so they are reaped inside ``stop()`` instead of orphaned into the next
    activity; a cancellation re-raised by ``stop()`` is absorbed here (resources already freed).
    """
    released = 0
    for key, recorder in list(_REGISTRY.items()):
        if key[1] != owner_id:
            continue
        if recorder._reserved:
            # Reserved (deferred live-view close): reaping here would free a display still mapped to this
            # owner's browser. Only browser teardown or process death releases the reservation.
            continue
        try:
            await release_display_recorder(recorder)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.warning("Failed to stop whole-display recorder during owner cleanup", owner_id=owner_id, exc_info=True)
        released += 1
    return released
