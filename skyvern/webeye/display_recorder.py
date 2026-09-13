"""Exclusive whole-display recorder (default OFF, cohort-gated).

Replaces per-page Playwright recording (only when ``EXCLUSIVE_DISPLAY_RECORDING`` is on and an owner is present)
with ONE change-driven whole-display MP4 covering every tab, browser chrome, and native dialogs. Capture/encode
is delegated to a replaceable source (``build_capture_command``); the default is the ctypes XDamage+XShm →
single-pass libx264 fragmented-MP4 bridge (:mod:`skyvern.webeye.display_capture_bridge`). No second transcode —
the MP4 on disk is the upload artifact (``video_utils.prepare_recording_for_upload`` skips ``.mp4``). Lifecycle
(flock fencing, owner registry + reservation, PDEATHSIG trampoline, SIGINT→SIGTERM→kill escalation,
cancellation-safe reap, owner-exact unlink) is preserved from the reviewed prior seam.
"""

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
from datetime import datetime
from pathlib import Path

import structlog

from skyvern.config import settings
from skyvern.webeye import attach_only
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.display_capture_bridge import build_bridge_command

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
# Deadline for the bridge's positive `READY` stdout ACK (emitted only after full X/XShm/FFmpeg/signal setup).
# A miss is a startup REFUSAL (kill + fall back to Playwright), so it must cover trampoline re-exec + interpreter
# start + ctypes CDLL loads + X handshake + FFmpeg Popen on a loaded worker; the 5s is paid only when wedged.
STARTUP_ACK_TIMEOUT = 5.0

# The Xvfb screen / base window is 1920x1080; an absent/unparseable/larger-than-screen window falls back to
# capturing the full screen. Output is bounded by the configured recording profile, aspect-preserved.
CAPTURE_DISPLAY_SIZE = (1920, 1080)
CaptureSizes = tuple[tuple[int, int], tuple[int, int]]

# Private browser_args carrier for the RAW (uncapped) recording output bound the caller selected for
# this run — the BROWSER_RECORDING_RESOLUTION profile or an operator override, NOT the viewport-capped
# Playwright record_video_size. Set only on the local-new-launch path and consumed+removed by
# prepare_local_display_recording before browser launch, so it never reaches Playwright/Patchright
# launch kwargs, remote/CDP/vendor contexts, reconnect, retry, storage, or cleanup.
DISPLAY_RECORDING_OUTPUT_BOUND_KEY = "_display_recording_output_bound"

_REGISTRY: dict[tuple[str, str], DisplayRecorder] = {}

# Parent-death via an exec'd python trampoline, NOT a fork-time preexec_fn (prctl/ctypes in the multithreaded
# worker's fork child can inherit locks and hang before exec). The parent passes its pid (argv[1]); the
# trampoline exits unless the live parent still matches before AND after prctl, then execvp's the bridge (pid
# kept; PDEATHSIG survives non-setuid exec), which owns and finalizes its own ffmpeg child on SIGTERM/EOF.
_PDEATHSIG_TRAMPOLINE = (
    "import ctypes,os,signal,sys\n"
    "e=int(sys.argv[1])\n"
    "os.getppid()==e or os._exit(0)\n"
    "try:ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGTERM)\n"  # 1 == PR_SET_PDEATHSIG
    "except Exception:pass\n"
    "signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGUSR1})\n"  # hold arm pending until bridge installs handler
    "os.getppid()==e or os._exit(0)\n"
    "os.execvp(sys.argv[2],sys.argv[2:])\n"
)


@dataclass(frozen=True)
class RecordingProfile:
    """Narrow, validated whole-display recording controls. ``resolve`` clamps/falls back explicitly so an
    invalid operator value can never produce an out-of-range or odd-dimension encode."""

    output_width: int
    output_height: int
    max_fps: int
    crf: int
    keyframe_seconds: int

    @staticmethod
    def _even_clamp(value: int, lo: int, hi: int, default: int) -> int:
        if not isinstance(value, int) or value < lo or value > hi:
            value = default
        return value - (value % 2)  # libx264/libvpx need even dimensions

    @classmethod
    def resolve(cls) -> RecordingProfile:
        max_w, max_h = CAPTURE_DISPLAY_SIZE

        def clamp(value: int, lo: int, hi: int, default: int) -> int:
            return value if isinstance(value, int) and lo <= value <= hi else default

        return cls(
            output_width=cls._even_clamp(settings.DISPLAY_RECORDING_OUTPUT_WIDTH, 2, max_w, 1280),
            output_height=cls._even_clamp(settings.DISPLAY_RECORDING_OUTPUT_HEIGHT, 2, max_h, 720),
            max_fps=clamp(settings.DISPLAY_RECORDING_MAX_FPS, 1, 60, 15),
            crf=clamp(settings.DISPLAY_RECORDING_CRF, 0, 51, 28),
            keyframe_seconds=clamp(settings.DISPLAY_RECORDING_KEYFRAME_SECONDS, 1, 30, 5),
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
    # Eligibility ONLY — the Playwright record_video_* pop is deferred to ``prepare_local_display_recording`` and
    # happens only after a live recorder is acquired, so a refusal leaves per-page recording intact.
    return bool(
        platform.system() == "Linux"
        and settings.EXCLUSIVE_DISPLAY_RECORDING
        # Display-ownership guard: the flock blocks a second recorder, not a second browser sharing the display.
        # Above concurrency 1 the whole-display capture could catch another run's browser, so fail closed and keep
        # per-page recording. Only concurrency == 1 (one browser per display) is ownership-safe.
        and settings.BROWSER_WORKER_MAX_CONCURRENT_ACTIVITIES == 1
        # VIDEO_PATH is required up front (acquire_display_recorder needs it too): a falsy path must NOT enable
        # whole-display recording, or the run would end up with neither per-page nor whole-display recording.
        and settings.VIDEO_PATH
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


async def prepare_local_display_recording(
    browser_args: dict[str, object],
    browser_artifacts: BrowserArtifacts,
    *,
    owner_id_override: str | None = None,
    workflow_run_id: str | None = None,
    task_id: str | None = None,
    script_id: str | None = None,
    browser_session_id: str | None = None,
) -> None:
    """Pre-context acquire seam shared by every local browser creator: decide eligibility, acquire the
    whole-display recorder BEFORE the browser context is created, and pop the Playwright record_video_* args
    ONLY if a live recorder was obtained. Every outcome yields exactly one recording:
      * ineligible / no owner-display-path -> args intact, Playwright records;
      * refusal or startup failure (recorder is None) -> args intact, Playwright records (fallback preserved);
      * success / reuse / adoption (recorder present) -> args popped, the display recorder owns the single MP4.
    The acquisition is stashed on ``browser_artifacts`` so the shared consumer can register a NEWLY-started
    recorder for outer cleanup (reused/adopted recorders are left live). Never raises into browser creation.
    """
    eligible = configure_local_display_recording(
        browser_args,
        owner_id_override=owner_id_override,
        workflow_run_id=workflow_run_id,
        task_id=task_id,
        script_id=script_id,
        browser_session_id=browser_session_id,
    )
    # Consume the raw-output-bound carrier on EVERY path (before the eligibility short-circuit) so it
    # never survives into the browser launch kwargs — eligible, ineligible, and refusal alike.
    output_bound = browser_args.pop(DISPLAY_RECORDING_OUTPUT_BOUND_KEY, None)
    browser_artifacts.local_display_recording_eligible = eligible
    if not eligible:
        return
    capture_sizes = resolve_display_capture_sizes(browser_args, output_bound=output_bound)
    browser_artifacts._display_capture_sizes = capture_sizes
    owner_id = resolve_owner_id(
        owner_id_override=owner_id_override,
        workflow_run_id=workflow_run_id,
        task_id=task_id,
        script_id=script_id,
        browser_session_id=browser_session_id,
    )
    display = os.environ.get("DISPLAY")
    if not (owner_id and display and settings.VIDEO_PATH):
        return
    video_dir = Path(settings.VIDEO_PATH) / datetime.utcnow().strftime("%Y-%m-%d")
    acquisition = await acquire_display_recorder(display, owner_id, video_dir, owner_id_override, capture_sizes)
    if acquisition.recorder is not None and acquisition.video_artifact is not None:
        # A live recorder exists (started, reused, or adopted): suppress Playwright per-page recording now.
        browser_args.pop("record_video_dir", None)
        browser_args.pop("record_video_size", None)
        browser_artifacts._display_recorder = acquisition.recorder
        browser_artifacts.video_artifacts = [acquisition.video_artifact]
        browser_artifacts._display_recorder_acquisition = acquisition


def carry_display_recording(src: BrowserArtifacts, dst: BrowserArtifacts) -> None:
    """Carry a prepared recording onto a rebuilt BrowserArtifacts (profile-corruption fallback rebuilds the
    artifacts after the recorder already started) so the run keeps its single recorder instead of orphaning it.
    The Playwright record_video_* args were already popped from the shared browser_args, so the fallback
    re-launch will not double-record."""
    dst.local_display_recording_eligible = src.local_display_recording_eligible
    dst._display_capture_sizes = src._display_capture_sizes
    dst._display_recorder = src._display_recorder
    dst.video_artifacts = src.video_artifacts
    dst._display_recorder_acquisition = src._display_recorder_acquisition


async def release_started_display_recording(browser_artifacts: BrowserArtifacts) -> None:
    """Release ONLY a freshly-started recorder stashed by ``prepare_local_display_recording`` — call from a
    creator's launch-failure path before re-raising so a newly-started recorder never leaks. A reused/adopted
    recorder (started=False) belongs to a live owner and is deliberately left running."""
    acquisition = browser_artifacts._display_recorder_acquisition
    if isinstance(acquisition, DisplayRecorderAcquisition) and acquisition.started and acquisition.recorder is not None:
        with suppress(Exception):
            await release_display_recorder(acquisition.recorder)
    browser_artifacts._display_recorder_acquisition = None
    browser_artifacts._display_recorder = None
    browser_artifacts.video_artifacts = []


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


def _bounded_even_output(width: int, height: int, bound: tuple[int, int]) -> tuple[int, int]:
    # Fit within `bound`, aspect-preserved and never upscaled; binding dimension chosen by integer
    # cross-multiplication (lands exactly on the bound, no float drift), the other floored to even.
    max_w, max_h = bound
    if width * max_h >= height * max_w:
        out_w: float = min(width, max_w)
        out_h: float = out_w * height / width
    else:
        out_h = min(height, max_h)
        out_w = out_h * width / height
    return _even_floor(out_w), _even_floor(out_h)


def _resolve_output_bound(profile: RecordingProfile, output_bound: object) -> tuple[int, int]:
    # The raw recording selection (BROWSER_RECORDING_RESOLUTION profile or operator override) is the
    # output bound ONLY when the whole pair is valid together: both width and height present, real
    # (non-bool) ints, and within the capture display. Any missing/partial/mixed-validity carrier
    # falls back to the COMPLETE static profile pair — never a carrier axis mixed with a profile axis,
    # which would distort the aspect. Each accepted axis is floored even for libx264/libvpx.
    fallback = (profile.output_width, profile.output_height)
    if not isinstance(output_bound, dict):
        return fallback
    width, height = output_bound.get("width"), output_bound.get("height")
    max_w, max_h = CAPTURE_DISPLAY_SIZE
    if (
        isinstance(width, int)
        and not isinstance(width, bool)
        and isinstance(height, int)
        and not isinstance(height, bool)
        and 2 <= width <= max_w
        and 2 <= height <= max_h
    ):
        return width - (width % 2), height - (height % 2)
    return fallback


def resolve_display_capture_sizes(
    browser_args: dict[str, object],
    profile: RecordingProfile | None = None,
    output_bound: object = None,
) -> CaptureSizes:
    """Derive immutable ((input_w, input_h), (output_w, output_h)) from the FINAL validated ``--window-size``
    and the recording output bound. ``output_bound`` (the raw, uncapped recording selection carried from the
    browser-args seam) takes precedence over the static profile output; it is NEVER the viewport-capped
    Playwright ``record_video_size``, which would fit the window twice. Input is the browser window at the
    display origin; output is that rectangle fit within the bound, even-dimensioned, no pad/crop/distortion. A
    missing/unparseable/larger-than-display window falls back to the fixed full-screen capture at the bound.
    When input equals output the capture bridge skips scaling entirely (the X4 no-scale profile)."""
    profile = profile or RecordingProfile.resolve()
    bound = _resolve_output_bound(profile, output_bound)
    window = _parse_window_size(browser_args)
    if window is None:
        return CAPTURE_DISPLAY_SIZE, _bounded_even_output(*CAPTURE_DISPLAY_SIZE, bound)
    width, height = window
    if not (0 < width <= CAPTURE_DISPLAY_SIZE[0] and 0 < height <= CAPTURE_DISPLAY_SIZE[1]):
        return CAPTURE_DISPLAY_SIZE, _bounded_even_output(*CAPTURE_DISPLAY_SIZE, bound)
    return (width, height), _bounded_even_output(width, height, bound)


def build_capture_command(
    display: str,
    owner_id: str,
    video_dir: Path,
    capture_sizes: CaptureSizes | None = None,
    profile: RecordingProfile | None = None,
    stats_path: str | None = None,
) -> tuple[list[str], Path]:
    """Narrow capture-source seam: returns (argv, output_path) for the default XDamage+XShm bridge.
    Swapping in a native-C source means replacing this one function."""
    profile = profile or RecordingProfile.resolve()
    output_path = video_dir / f"{_safe_owner_id(owner_id)}.mp4"
    (input_w, input_h), (output_w, output_h) = capture_sizes or (
        CAPTURE_DISPLAY_SIZE,
        _bounded_even_output(*CAPTURE_DISPLAY_SIZE, (profile.output_width, profile.output_height)),
    )
    command = build_bridge_command(
        display=normalize_display(display),
        output_path=str(output_path),
        input_width=input_w,
        input_height=input_h,
        output_width=output_w,
        output_height=output_h,
        max_fps=profile.max_fps,
        crf=profile.crf,
        keyframe_seconds=profile.keyframe_seconds,
        stats_path=stats_path,
    )
    return command, output_path


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the bridge AND its ffmpeg child together. The bridge is spawned in its own session
    (``start_new_session``), so its pid is the process-group leader; killing the group prevents an orphaned
    ffmpeg when a wedged bridge cannot finalize on SIGINT/SIGTERM. Falls back to a single-process kill."""
    pid = process.pid
    if pid is None:
        return
    killed_group = False
    with suppress(ProcessLookupError, PermissionError, OSError):
        pgid = os.getpgid(pid)
        # Only kill the group when the bridge is its OWN group leader (start_new_session); never risk
        # SIGKILLing the worker's own process group.
        if pgid == pid:
            os.killpg(pgid, signal.SIGKILL)
            killed_group = True
    if not killed_group:
        with suppress(ProcessLookupError):
            process.kill()


async def _reap_surviving_cancellation(process: asyncio.subprocess.Process) -> bool:
    """Await the child's reaping to completion even under repeated cancellation. Reports whether any cancel
    arrived so the caller re-raises exactly once — after it has released its resources."""
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
        # Set when the recorder is stopped for upload but the display must stay fenced to this owner until a
        # later, separate teardown (a deferred live-view browser close).
        self._reserved = False
        # Latched once the arm signal is successfully delivered to the bridge (parent-side exactly-once).
        self._armed = False

    def arm_capture(self) -> bool:
        """Signal the bridge to begin its timeline at the current ready page (SIGUSR1). Parent-side
        EXACTLY-ONCE: the first successful send latches, later calls are idempotent no-ops; a FAILED send does
        NOT latch, so a later call may retry. Returns True iff the bridge is/was armed, False if the signal
        could not be delivered (bridge already exited or send raised). Never touches the registry, display
        flock, reservation, or ownership."""
        if self._armed:
            return True
        if self.process.returncode is not None:
            return False
        try:
            self.process.send_signal(signal.SIGUSR1)
        except Exception:  # noqa: BLE001 - a failed send must stay retryable, so do not latch
            return False
        self._armed = True
        return True

    @property
    def is_stopped(self) -> bool:
        """True once ``stop()`` has run to completion, so the MP4 on disk is finalized and uploadable."""
        return self._stop_result is not None

    async def finalize_keeping_reservation(self) -> bool:
        """Stop the capture so the MP4 is complete and uploadable, but keep the display lock and registry
        reservation (deferred live-view close: the run's browser is still mapped on the display)."""
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
                # Reap the child and (unless reserved) release the lock even under repeated cancellation, so a
                # cancelled stop never leaks the bridge/ffmpeg or blocks the next run.
                if self.process.returncode is None:
                    _kill_process_group(self.process)
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
        # SIGINT: the bridge injects a terminal frame, closes ffmpeg stdin, and lets ffmpeg finalize the MP4.
        self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=SIGINT_TIMEOUT)
            # Report the truth: a nonzero bridge exit (failed finalize) is NOT graceful, so callers relying
            # on stop() -> False can surface it rather than treating any timely exit as success.
            return self.process.returncode == 0
        except TimeoutError:
            pass
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=SIGTERM_TIMEOUT)
        except TimeoutError:
            return False
        return self.process.returncode == 0


def _prune_dead_entries(normalized_display: str, owner_id: str) -> None:
    # Drop exited recorders of a DIFFERENT owner on this display so a previous run's dead recorder never
    # fail-closes the next one. Same-owner is handled by reuse; a live owner is left entirely alone.
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
    profile: RecordingProfile | None = None,
) -> DisplayRecorderAcquisition:
    if fcntl is None:
        LOG.warning("Whole-display recording unavailable: fcntl not present on this platform", display=display)
        return DisplayRecorderAcquisition(None, None, False)

    normalized_display = normalize_display(display)
    key = (normalized_display, owner_id)

    existing = _REGISTRY.get(key)
    if existing is not None:
        # Same (display, owner): reuse the exact recorder + VideoArtifact (never restart a dead one — a fresh
        # capture would overwrite the partial MP4 and mint a second RECORDING row); the partial uploads as-is.
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
        stats_path = str(video_dir / f"{_safe_owner_id(owner_id)}.stats.json")
        command, output_path = build_capture_command(
            normalized_display, owner_id, video_dir, capture_sizes, profile, stats_path=stats_path
        )
        # The bridge emits a directly-usable fragmented MP4 (no second transcode); the existing upload path
        # consumes .mp4 without recompression.
        prefix = [sys.executable, "-c", _PDEATHSIG_TRAMPOLINE, str(os.getpid())] if os.name == "posix" else []
        process = await asyncio.create_subprocess_exec(
            *prefix,
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Own process group so a force-kill reaps the bridge AND its ffmpeg child together (no orphan);
            # PDEATHSIG still binds the bridge to worker death independently of the group.
            start_new_session=(os.name == "posix"),
        )
        # Positive readiness: accept the recorder ONLY on the bridge's single exact `READY\n` stdout line from a
        # still-live child. EOF / wrong line / early exit / a timeout (wedged pre-loop) all fail closed — the
        # timeout raises and the mismatch raises, both funneling through the BaseException path below (kill group,
        # cancellation-safe reap, close lock, no recorder) so Playwright's own record_video_* args survive.
        assert process.stdout is not None  # stdout=PIPE always yields a readable stream
        ack = await asyncio.wait_for(process.stdout.readline(), timeout=STARTUP_ACK_TIMEOUT)
        if ack != b"READY\n" or process.returncode is not None:
            raise RuntimeError(f"whole-display recorder did not signal readiness (ack={ack!r})")
    except BaseException as exc:
        cancelled_during_reap = False
        if process is not None and process.returncode is None:
            _kill_process_group(process)
            cancelled_during_reap = await _reap_surviving_cancellation(process)
        if lock_fd is not None:
            with suppress(OSError):
                os.close(lock_fd)
        if not isinstance(exc, Exception):
            raise
        if cancelled_during_reap:
            raise asyncio.CancelledError()
        LOG.warning("Failed to start whole-display recorder", display=normalized_display, exc_info=True)
        return DisplayRecorderAcquisition(None, None, False)

    # Advertise the container up front: the bridge always writes a fragmented MP4, so the in-progress
    # artifact ArtifactManager creates on step 0 must be `.mp4`, not the default `.webm`, before any bytes
    # exist on disk (SKY-15466).
    video_artifact = VideoArtifact(video_path=str(output_path), video_file_extension="mp4")
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
    # entry must never be freed until the serialized stop is terminal.
    stop_task = asyncio.ensure_future(recorder.stop())
    cancelled = False
    try:
        while True:
            try:
                return await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                cancelled = True
                if stop_task.done():
                    return stop_task.result()
    finally:
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
    """Stop and de-register every live whole-display recorder this owner still holds. Call BEFORE unlinking
    so a run whose teardown never reached ``release_display_recorder`` (Temporal cancel / mid-run crash) is
    reaped inside ``stop()`` instead of orphaned into the next activity."""
    released = 0
    cancelled = False
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
            # release_display_recorder finishes the shielded stop (lock/registry freed) THEN re-raises the
            # caller's cancellation. Record it, keep sweeping the remaining owners, and re-raise after the sweep
            # so the cancellation is never discarded (finish-then-re-raise, as in stop()/_reap/_reclaim).
            cancelled = True
        except Exception:
            LOG.warning("Failed to stop whole-display recorder during owner cleanup", owner_id=owner_id, exc_info=True)
        released += 1
    if cancelled:
        raise asyncio.CancelledError()
    return released


async def release_all_display_recorders() -> int:
    """Release EVERY whole-display recorder in this process, **including reserved ones**.

    Call ONLY from the single-activity (``BROWSER_WORKER_MAX_CONCURRENT_ACTIVITIES==1``) run-teardown boundary,
    AFTER that boundary has killed this run's browsers. Under concurrency==1 the just-finished run is the sole
    display owner, so once its browser is gone the retained fence (flock + registry) is safe to free — freeing it
    here lets the next sequential run acquire the display instead of being starved until worker-process death
    (SKY-15807). ``_reserved`` is intentionally NOT skipped (unlike the in-run ``stop_display_recorders_for_owner``
    sweep, whose skip guards a browser that may still be live). Idempotent and cancellation-safe: it delegates to
    ``release_display_recorder`` (serialized terminal stop, then flock close + registry drop), and finishes the
    sweep before re-raising any cancellation so no entry is orphaned."""
    released = 0
    cancelled = False
    for _key, recorder in list(_REGISTRY.items()):
        try:
            await release_display_recorder(recorder)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            LOG.warning("Failed to release whole-display recorder at run-teardown boundary", exc_info=True)
        released += 1
    if cancelled:
        raise asyncio.CancelledError()
    return released
