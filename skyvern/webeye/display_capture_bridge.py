"""Change-driven whole-display capture source for the exclusive display recorder.

Watches XDamage across the window tree, captures the root image via MIT-SHM (zero-copy) at most ``--max-fps``/s
(coalescing bursts), and pipes raw frames to a single FFmpeg encoding a wall-clock VFR fragmented H.264 MP4 in
one pass (no second transcode). A static screen emits no frames (~0 CPU); PTS gaps + a terminal frame preserve
the wall-clock timeline. Invoked as a subprocess (``python -m skyvern.webeye.display_capture_bridge ...``) that
owns its FFmpeg child; ctypes loads inside functions so this imports cleanly on non-Linux (OSS surface).

Reliability: NO ``+faststart`` + ``-flush_packets 1`` so fragments hit disk as they close (an abrupt kill leaves
an MP4 decodable to the last fragment); a SIGINT/SIGTERM terminal frame keeps the finalized duration truthful;
the SysV shm segment is IPC_RMID'd at attach and detached on exit (no leak).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ZPixmap = 2
AllPlanes = 0xFFFFFFFF
XDamageReportRawRectangles = 0
SubstructureNotifyMask = 1 << 19
IsViewable = 2  # XWindowAttributes.map_state: mapped AND all ancestors mapped -> contributes visible pixels
DestroyNotify = 17  # X core event type; a destroyed window's damage object is auto-freed by the server
IPC_CREAT = 0o1000
IPC_RMID = 0

# Distinct FATAL exit codes so the readiness gate fails closed with an attributable reason — a nonzero exit in
# the startup window aborts acquisition (Playwright suppression already happened), beating a zero-frame file.
EXIT_OPEN_DISPLAY = 2
EXIT_NO_XDAMAGE = 3
EXIT_UNSUPPORTED_BPP = 5
EXIT_NO_XSHM = 6
EXIT_XSHMCREATEIMAGE = 7
EXIT_SHMGET = 8
EXIT_SHMAT = 9
EXIT_XSHMATTACH = 10
EXIT_READINESS = 11
EXIT_STRIDE_MISMATCH = 12

# Fragment flush cadence (s), DECOUPLED from the larger keyframe interval so an early per-step read sees a
# decodable fragment (~1s) not a 0-frame header until the second keyframe. Cost +3.4% size; crash loss ~1s.
FRAGMENT_FLUSH_SECONDS = 1


def _shmat_failed(addr: int | None) -> bool:
    """shmat returns (void*)-1 on failure (and ctypes may surface it as None or the all-ones mask)."""
    if addr is None:
        return True
    return addr in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF, -1)


def _stride_consistent(bytes_per_line: int, width: int, bpp: int) -> bool:
    """FFmpeg gets -video_size WxH + bytes_per_line*height bytes, so require a packed stride (no row padding)."""
    return bpp % 8 == 0 and bytes_per_line == width * (bpp // 8)


def _allocate_and_attach_shm(
    X: Any, XE: Any, LIBC: Any, dpy: Any, root: int, shm_image: Any, shm_info: Any, frame_bytes: int, err: dict
) -> tuple[int | None, int]:
    """Allocate + attach the MIT-SHM segment and prove readiness, failing closed with an attributable code and
    NO leaked segment/attach (each branch reclaims exactly what it created). Returns (mapped_address, 0) or
    (None, EXIT_*). Ordering: shmget<0 -> EXIT_SHMGET; shmat==(void*)-1 -> EXIT_SHMAT; XShmAttach error ->
    EXIT_XSHMATTACH; initial XShmGetImage fails -> EXIT_READINESS. Extracted for direct failure-branch testing."""
    shmid = LIBC.shmget(0, frame_bytes, IPC_CREAT | 0o600)
    if shmid < 0:
        return None, EXIT_SHMGET
    addr = LIBC.shmat(shmid, None, 0)
    if _shmat_failed(addr):
        LIBC.shmctl(shmid, IPC_RMID, None)  # reclaim the segment we just created
        return None, EXIT_SHMAT
    shm_info.shmid = shmid
    shm_info.shmaddr = addr
    shm_image.contents.data = addr  # XShmGetImage writes into the shm via the XImage data pointer
    shm_info.readOnly = 0
    err_before_attach = err["n"]
    XE.XShmAttach(dpy, ctypes.byref(shm_info))
    X.XSync(dpy, 0)  # flush so a failed XShmAttach reaches the swallowing error handler and bumps err["n"]
    if err["n"] != err_before_attach:
        LIBC.shmdt(addr)
        LIBC.shmctl(shmid, IPC_RMID, None)
        return None, EXIT_XSHMATTACH
    LIBC.shmctl(shmid, IPC_RMID, None)  # free on detach; segment persists while attached (no leak)
    # Readiness receipt: the FIRST XShmGetImage must succeed before FFmpeg is spawned — otherwise the
    # recorder would suppress Playwright and then silently produce a zero-frame recording.
    if not XE.XShmGetImage(dpy, root, shm_image, 0, 0, AllPlanes):
        XE.XShmDetach(dpy, ctypes.byref(shm_info))
        LIBC.shmdt(addr)
        return None, EXIT_READINESS
    return addr, 0


_SHM_FATAL_MESSAGE = {
    EXIT_SHMGET: "shmget failed",
    EXIT_SHMAT: "shmat failed",
    EXIT_XSHMATTACH: "XShmAttach failed",
    EXIT_READINESS: "initial XShmGetImage failed",
}


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


class _XShmSegmentInfo(ctypes.Structure):
    _fields_ = [
        ("shmseg", ctypes.c_ulong),
        ("shmid", ctypes.c_int),
        ("shmaddr", ctypes.c_void_p),
        ("readOnly", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("pad", ctypes.c_long * 24)]  # noqa: RUF012 - ctypes field spec


class _XErrorEvent(ctypes.Structure):
    # Leading prefix of Xlib's XErrorEvent; only error_code is read, to tell a tolerable BadDamage race on a
    # since-reaped window from an unrelated X error that must not be silently swallowed.
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class _XWindowAttributes(ctypes.Structure):
    # Full Xlib XWindowAttributes: XGetWindowAttributes writes the WHOLE struct, so every field must be present to
    # size the buffer correctly (a short struct would be a buffer overrun). Only map_state is read.
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("win_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XSubstructureEvent(ctypes.Structure):
    # Shared leading layout of XDestroyWindowEvent/XUnmapEvent delivered via SubstructureNotifyMask; ``window`` is
    # the affected child (``event`` is the parent the mask was selected on). Only ``window`` is read.
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("event", ctypes.c_ulong),
        ("window", ctypes.c_ulong),
    ]


def windows_to_prune(tracked: set[int] | dict[int, int], viewable: set[int], root: int) -> list[int]:
    """XIDs whose damage handle must be dropped: any tracked window that is no longer viewable — destroyed (absent
    from the tree) OR unmapped/unviewable (present but not IsViewable). The root is always retained."""
    return [w for w in tracked if w != root and w not in viewable]


def _destroy_owned_handle(
    XD: Any, X: Any, dpy: Any, dh: int, w: int, err: dict, bad_damage_code: int, context: str
) -> None:
    """Destroy one owned XDamage handle, bracketed by XSync so a queued error is attributable to this request.
    ONLY the exact BadDamage code (a window the server already reaped) is tolerated silently; any other code — or
    an unreadable/unknown code (``None``) — keeps a scoped stderr diagnostic rather than being masked as BadDamage."""
    before = err["n"]
    XD.XDamageDestroy(dpy, dh)
    X.XSync(dpy, 0)  # flush so a queued destroy error reaches the swallowing handler and bumps err["n"]/err["code"]
    if err["n"] != before and err.get("code") != bad_damage_code:
        print(
            f"WARN: unexpected X error code {err.get('code')} destroying {context} damage handle for 0x{w:x}",
            file=sys.stderr,
            flush=True,
        )


def prune_unviewable_windows(
    XD: Any, X: Any, dpy: Any, damaged: dict[int, int], viewable: set[int], root: int, err: dict, bad_damage_code: int
) -> int:
    """Destroy and drop the handles of tracked windows that are no longer viewable (destroyed OR unmapped), never
    the root. Pop-before-destroy makes each owned handle destroyed at most once, so a re-mapped or XID-reused
    window re-arms fresh instead of inheriting stale state. Returns the count pruned."""
    stale = windows_to_prune(damaged, viewable, root)
    for w in stale:
        _destroy_owned_handle(XD, X, dpy, damaged.pop(w), w, err, bad_damage_code, "unviewable")
    return len(stale)


def reconcile_destroyed_window(damaged: dict[int, int], w: int, root: int) -> bool:
    """Event-driven: on a DestroyNotify, drop the destroyed window's entry immediately (before any rescan) so a
    same-XID reuse in the sub-rescan interval cannot inherit its now-invalid handle. The server auto-frees the
    damage object with the window, so no XDamageDestroy is issued here (it would only queue a guaranteed
    BadDamage). No-op for the root or an untracked window. Returns True if an entry was dropped."""
    if w == root or w not in damaged:
        return False
    damaged.pop(w)
    return True


def _destroy_all_handles(XD: Any, X: Any, dpy: Any, damaged: dict[int, int]) -> None:
    """Teardown: destroy every remaining owned XDamage handle (child + root) exactly once. Best-effort — a
    handle whose window is already gone raises BadDamage into the swallowing handler, not out of teardown."""
    while damaged:
        _w, dh = damaged.popitem()
        try:
            XD.XDamageDestroy(dpy, dh)
        except Exception:  # noqa: BLE001,S110 - teardown must not raise; leftover handles die with the connection
            pass
    try:
        X.XSync(dpy, 0)
    except Exception:  # noqa: BLE001,S110 - best-effort flush at teardown
        pass


def build_bridge_command(
    *,
    display: str,
    output_path: str,
    input_width: int,
    input_height: int,
    output_width: int,
    output_height: int,
    max_fps: int,
    crf: int,
    keyframe_seconds: int,
    stats_path: str | None = None,
    python_executable: str | None = None,
) -> list[str]:
    """Argv running this bridge as a subprocess — the single narrow seam a future native-C source can replace."""
    argv = [
        python_executable or sys.executable,
        "-m",
        "skyvern.webeye.display_capture_bridge",
        "--display",
        display,
        "--size",
        f"{input_width}x{input_height}",
        "--out-size",
        f"{output_width}x{output_height}",
        "--max-fps",
        str(max_fps),
        "--crf",
        str(crf),
        "--keyframe-sec",
        str(keyframe_seconds),
        "--out",
        output_path,
    ]
    if stats_path:
        argv += ["--stats", stats_path]
    return argv


def _load_x() -> tuple[ctypes.CDLL, ctypes.CDLL, ctypes.CDLL, ctypes.CDLL]:
    X = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    XD = ctypes.CDLL(ctypes.util.find_library("Xdamage") or "libXdamage.so.1")
    XE = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
    LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
    D = ctypes.c_void_p
    W = ctypes.c_ulong
    X.XOpenDisplay.restype = D
    X.XOpenDisplay.argtypes = [ctypes.c_char_p]
    X.XDefaultRootWindow.restype = W
    X.XDefaultRootWindow.argtypes = [D]
    X.XDefaultScreen.restype = ctypes.c_int
    X.XDefaultScreen.argtypes = [D]
    X.XDefaultVisual.restype = D
    X.XDefaultVisual.argtypes = [D, ctypes.c_int]
    X.XDefaultDepth.restype = ctypes.c_int
    X.XDefaultDepth.argtypes = [D, ctypes.c_int]
    X.XConnectionNumber.restype = ctypes.c_int
    X.XConnectionNumber.argtypes = [D]
    X.XPending.restype = ctypes.c_int
    X.XPending.argtypes = [D]
    X.XNextEvent.argtypes = [D, ctypes.POINTER(_XEvent)]
    X.XSync.argtypes = [D, ctypes.c_int]
    X.XSelectInput.argtypes = [D, W, ctypes.c_long]
    X.XGetWindowAttributes.restype = ctypes.c_int
    X.XGetWindowAttributes.argtypes = [D, W, ctypes.POINTER(_XWindowAttributes)]
    X.XQueryTree.restype = ctypes.c_int
    X.XQueryTree.argtypes = [
        D,
        W,
        ctypes.POINTER(W),
        ctypes.POINTER(W),
        ctypes.POINTER(ctypes.POINTER(W)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    X.XFree.argtypes = [ctypes.c_void_p]
    X.XSetErrorHandler.restype = ctypes.c_void_p
    X.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    XD.XDamageQueryExtension.restype = ctypes.c_int
    XD.XDamageQueryExtension.argtypes = [D, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    XD.XDamageCreate.restype = W
    XD.XDamageCreate.argtypes = [D, W, ctypes.c_int]
    XD.XDamageSubtract.argtypes = [D, W, ctypes.c_ulong, ctypes.c_ulong]
    XD.XDamageDestroy.argtypes = [D, W]
    XE.XShmQueryExtension.restype = ctypes.c_int
    XE.XShmQueryExtension.argtypes = [D]
    XE.XShmCreateImage.restype = ctypes.POINTER(_XImage)
    XE.XShmCreateImage.argtypes = [
        D,
        D,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_XShmSegmentInfo),
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    XE.XShmAttach.argtypes = [D, ctypes.POINTER(_XShmSegmentInfo)]
    XE.XShmDetach.argtypes = [D, ctypes.POINTER(_XShmSegmentInfo)]
    XE.XShmGetImage.restype = ctypes.c_int
    XE.XShmGetImage.argtypes = [D, W, ctypes.POINTER(_XImage), ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    LIBC.shmget.restype = ctypes.c_int
    LIBC.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    LIBC.shmat.restype = ctypes.c_void_p
    LIBC.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    LIBC.shmdt.argtypes = [ctypes.c_void_p]
    LIBC.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    return X, XD, XE, LIBC


def _enumerate_windows(X: ctypes.CDLL, dpy: Any, w: int, depth: int, acc: list[int], maxdepth: int = 6) -> None:
    acc.append(w)
    if depth >= maxdepth:
        return
    rr = ctypes.c_ulong()
    pr = ctypes.c_ulong()
    ch = ctypes.POINTER(ctypes.c_ulong)()
    n = ctypes.c_uint()
    if X.XQueryTree(dpy, w, ctypes.byref(rr), ctypes.byref(pr), ctypes.byref(ch), ctypes.byref(n)):
        for i in range(n.value):
            _enumerate_windows(X, dpy, ch[i], depth + 1, acc, maxdepth)
        if ch:
            X.XFree(ch)


def _write_all(fd: int, buf: Any) -> int:
    """Write the whole frame, tolerating short writes and EINTR. Returns bytes written, or -1 on EPIPE."""
    mv = memoryview(buf)
    total = 0
    n = len(mv)
    while total < n:
        try:
            w = os.write(fd, mv[total:])
        except InterruptedError:
            continue
        except BrokenPipeError:
            return -1
        if w == 0:
            return -1
        total += w
    return total


def build_ffmpeg_command(
    *,
    pix_fmt: str,
    input_width: int,
    input_height: int,
    output_width: int,
    output_height: int,
    max_fps: int,
    crf: int,
    keyframe_seconds: int,
    output_path: str,
) -> list[str]:
    no_scale = (input_width, input_height) == (output_width, output_height)
    vf = "format=yuv420p" if no_scale else f"scale={output_width}:{output_height},format=yuv420p"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        pix_fmt,
        "-video_size",
        f"{input_width}x{input_height}",
        "-framerate",
        str(max_fps),
        "-use_wallclock_as_timestamps",
        "1",
        "-i",
        "-",
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{keyframe_seconds})",
        "-flush_packets",
        "1",
        # NO +faststart (a close-time moov rewrite an abrupt kill would leave unusable); empty_moov streams a
        # header up front, frag_keyframe/frag_duration + flush_packets close+flush fragments (see FRAGMENT_FLUSH_SECONDS).
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration",
        str(FRAGMENT_FLUSH_SECONDS * 1_000_000),
        "-fps_mode",
        "vfr",
        output_path,
    ]


# Two-phase capture (SKY-15466): writing the first XDamage before the browser context exists would hold the empty
# (about:blank/black) window as a long startup head, so NO frames write until arm; frame zero = ready page (PTS 0).
def arm_transition(armed: bool, arm_consumed: bool) -> bool:
    return armed and not arm_consumed


def should_emit(arm_consumed: bool, pending_dirty: bool, interval_ok: bool) -> bool:
    """Steady-state: emit a frame only after arm, on pending damage, once the min-interval has elapsed."""
    return arm_consumed and pending_dirty and interval_ok


def should_write_terminal(frames_written: int) -> bool:
    return frames_written > 0


def should_unlink_output(frames_written: int) -> bool:
    return frames_written == 0


def _finalize_return_code(frames_written: int, ffmpeg_returncode: int | None, arm_consumed: bool) -> int:
    """frames>0: preserve the ffmpeg result. frames==0: clean (0) only for an UNARMED teardown, else nonzero."""
    if frames_written > 0:
        return 0 if ffmpeg_returncode == 0 else 1
    return 1 if arm_consumed else 0


def _run(args: argparse.Namespace) -> int:
    gw, gh = (int(v) for v in args.size.split("x"))
    ow, oh = (int(v) for v in args.out_size.split("x"))
    # Install ONLY the SIGUSR1 arm handler before startup, then unblock it (the PDEATHSIG trampoline blocked it
    # pre-exec, so an arm in the exec window stays pending, not default-killing the bridge). A failed unblock must
    # propagate so the bridge startup-fails and acquisition falls back rather than run with SIGUSR1 blocked.
    armed = {"v": False}
    signal.signal(signal.SIGUSR1, lambda *a: armed.__setitem__("v", True))
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})
    X, XD, XE, LIBC = _load_x()
    dpy = X.XOpenDisplay(args.display.encode())
    if not dpy:
        print("FATAL: XOpenDisplay failed", file=sys.stderr, flush=True)
        return EXIT_OPEN_DISPLAY
    scr = X.XDefaultScreen(dpy)
    root = X.XDefaultRootWindow(dpy)
    eb = ctypes.c_int(0)
    erb = ctypes.c_int(0)
    if not XD.XDamageQueryExtension(dpy, ctypes.byref(eb), ctypes.byref(erb)):
        print("FATAL: XDamage unavailable", file=sys.stderr, flush=True)
        return EXIT_NO_XDAMAGE
    if not XE.XShmQueryExtension(dpy):
        print("FATAL: MIT-SHM unavailable", file=sys.stderr, flush=True)
        return EXIT_NO_XSHM
    damage_notify = eb.value
    bad_damage_code = erb.value  # XDamage's first (only) error code; a destroy on a reaped window raises it

    # Chromium creates InputOnly windows; XDamageCreate on them throws BadMatch, fatal under the default
    # handler. Swallow X errors and only keep damage handles that create cleanly.
    ERRH = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    err: dict = {"n": 0, "code": None}

    def _on_x_error(_display: Any, event: Any) -> int:
        err["n"] += 1
        try:
            err["code"] = ctypes.cast(event, ctypes.POINTER(_XErrorEvent)).contents.error_code
        except Exception:  # noqa: BLE001 - the error handler must never raise back into Xlib
            err["code"] = None
        return 0

    _eh = ERRH(_on_x_error)
    X.XSetErrorHandler(ctypes.cast(_eh, ctypes.c_void_p))
    X.XSelectInput(dpy, root, SubstructureNotifyMask)

    damaged: dict[int, int] = {}
    windows_pruned = 0
    windows_reconciled = 0
    attrs = _XWindowAttributes()

    def is_viewable(w: int) -> bool:
        # A window contributes visible pixels only when mapped with all ancestors mapped (IsViewable). A window
        # that vanished between enumeration and this query returns 0 here -> treated as gone (not viewable).
        if not X.XGetWindowAttributes(dpy, w, ctypes.byref(attrs)):
            return False
        return attrs.map_state == IsViewable

    def scan_and_arm() -> int:
        nonlocal windows_pruned
        acc: list[int] = []
        _enumerate_windows(X, dpy, root, 0, acc)
        added = 0
        # Drain BadDamage queued from the prior loop's XDamageSubtract on since-destroyed windows BEFORE
        # snapshotting err["n"]; otherwise the first per-window XSync charges those stale errors to this scan's
        # XDamageCreate, dropping a valid new window from ``damaged`` and leaking its handle every 1s rescan.
        X.XSync(dpy, 0)
        # Arm/keep only viewable windows. Pruning by viewability (not mere tree membership) drops BOTH destroyed
        # windows (absent from the tree) AND unmapped-but-live windows, so a remap re-arms fresh with no retained
        # hidden-window state; the root is always viewable and is never pruned.
        viewable = [w for w in acc if w == root or is_viewable(w)]
        windows_pruned += prune_unviewable_windows(XD, X, dpy, damaged, set(viewable), root, err, bad_damage_code)
        for w in viewable:
            if w not in damaged:
                before = err["n"]
                dh = XD.XDamageCreate(dpy, w, XDamageReportRawRectangles)
                X.XSync(dpy, 0)
                if err["n"] == before:
                    damaged[w] = dh
                    # Select SubstructureNotify so this window's parent reports its DestroyNotify; armed top-down,
                    # every armed window's parent is armed too, so destroy events propagate up to the root handler.
                    X.XSelectInput(dpy, w, SubstructureNotifyMask)
                    added += 1
        return added

    fd = X.XConnectionNumber(dpy)

    visual = X.XDefaultVisual(dpy, scr)
    depth = X.XDefaultDepth(dpy, scr)
    shm_info = _XShmSegmentInfo()
    shm_image = XE.XShmCreateImage(dpy, visual, depth, ZPixmap, None, ctypes.byref(shm_info), gw, gh)
    if not shm_image:
        print("FATAL: XShmCreateImage failed", file=sys.stderr, flush=True)
        return EXIT_XSHMCREATEIMAGE
    img = shm_image.contents
    frame_bytes = img.bytes_per_line * img.height
    bpp = img.bits_per_pixel
    pix_fmt = "bgra" if bpp == 32 else ("bgr24" if bpp == 24 else "")
    if not pix_fmt:
        print(f"FATAL: unsupported bpp={bpp}", file=sys.stderr, flush=True)
        return EXIT_UNSUPPORTED_BPP
    if not _stride_consistent(img.bytes_per_line, gw, bpp):
        # Fail closed rather than stream padded rows as garbage (deployed depth-24 Xvfb is 32bpp / packed).
        print(
            f"FATAL: row-stride mismatch bytes_per_line={img.bytes_per_line} width={gw} bpp={bpp}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_STRIDE_MISMATCH
    addr, shm_code = _allocate_and_attach_shm(X, XE, LIBC, dpy, root, shm_image, shm_info, frame_bytes, err)
    if shm_code != 0:
        print(f"FATAL: {_SHM_FATAL_MESSAGE.get(shm_code, 'shm setup failed')}", file=sys.stderr, flush=True)
        return shm_code

    ff = build_ffmpeg_command(
        pix_fmt=pix_fmt,
        input_width=gw,
        input_height=gh,
        output_width=ow,
        output_height=oh,
        max_fps=args.max_fps,
        crf=args.crf,
        keyframe_seconds=args.keyframe_sec,
        output_path=args.out,
    )
    # Kept open for the FFmpeg child's whole lifetime (a context manager would close it before finalize).
    ff_log = open(str(Path(args.out).with_suffix(".ffmpeg.log")), "wb")  # noqa: SIM115
    ffp = subprocess.Popen(ff, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=ff_log)
    assert ffp.stdin is not None  # stdin=PIPE always yields a writable pipe
    wfd = ffp.stdin.fileno()

    # Now that X/SHM/FFmpeg are up, replace the default SIGINT/SIGTERM disposition with the graceful-stop flag
    # (a terminal frame is injected on stop). Kept AFTER init so a death/stop during a hung init still fast-kills.
    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__("v", True))
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))

    frames = {"n": 0, "bytes": 0, "short_writes": 0, "grab_fail": 0}
    first_frame_epoch: float | None = None

    def capture_and_write() -> None:
        nonlocal first_frame_epoch
        if not XE.XShmGetImage(dpy, root, shm_image, 0, 0, AllPlanes):
            frames["grab_fail"] += 1
            return
        view = (ctypes.c_char * frame_bytes).from_address(addr)
        w = _write_all(wfd, view)
        if w < 0:
            stop["v"] = True
            return
        if w != frame_bytes:
            frames["short_writes"] += 1
        if frames["n"] == 0:
            first_frame_epoch = time.time()
            if args.stats:
                Path(args.stats).write_text(json.dumps({"first_frame_epoch": first_frame_epoch, "frames_written": 1}))
        frames["n"] += 1
        frames["bytes"] += w

    min_interval = 1.0 / args.max_fps if args.max_fps > 0 else 0.0
    last_capture = time.monotonic()
    last_rescan = time.monotonic()
    windows_armed = 0
    pending_dirty = False
    arm_consumed = False

    # Own the XDamage handles inside try/finally: the initial arm happens only after the FATAL-returning X/SHM/FFmpeg
    # setup above (which owned no handles, so its early returns cannot leak), and _destroy_all_handles then runs on
    # EVERY capture-phase exit — normal stop, SIGINT/SIGTERM, or an exception — within the handles' owning lifetime.
    try:
        scan_and_arm()
        windows_armed = len(damaged)
        # Positive readiness: X/XDamage/XShm are up, the initial XShmGetImage receipt passed, FFmpeg is spawned, and
        # the graceful-stop signal handlers are installed — every known pre-loop wedge point is cleared. Emit exactly
        # one READY line on stdout so the parent accepts the recorder only now; all diagnostics stay on stderr.
        print("READY", flush=True)

        while not stop["v"]:
            try:
                select.select([fd], [], [], 0.2)  # blocking optimization + ~5fps floor; readability is not consulted
            except (InterruptedError, OSError):
                pass
            # Drain Xlib's queue after EVERY select return, not only when the socket was readable: the periodic
            # scan_and_arm() XSync below can pull socket bytes into Xlib's internal event queue, leaving a transient
            # damage event queued with no later socket readability. XPending sees both the socket and that queue.
            saw_damage = False
            while X.XPending(dpy) > 0:
                ev = _XEvent()
                X.XNextEvent(dpy, ctypes.byref(ev))
                if ev.type == damage_notify:
                    saw_damage = True
                elif ev.type == DestroyNotify:
                    # Reconcile a destroyed window immediately (before any rescan): drop its entry so a same-XID
                    # reuse in the sub-rescan interval re-arms fresh instead of inheriting the auto-freed handle.
                    gone = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(_XSubstructureEvent)).contents.window
                    if reconcile_destroyed_window(damaged, gone, root):
                        windows_reconciled += 1
            if saw_damage:
                pending_dirty = True
                for _w, dh in list(damaged.items()):
                    XD.XDamageSubtract(dpy, dh, 0, 0)
            now = time.monotonic()
            if now - last_rescan >= 1.0:
                if scan_and_arm():
                    # A window that finished its initial paint before this rescan emits no later XDamage, so mark
                    # dirty here or the next FPS-coalesced emit would never capture it (e.g. a popup/native dialog).
                    pending_dirty = True
                windows_armed = len(damaged)  # reflects newly armed windows and pruned/reconciled departures
                last_rescan = now
            if arm_transition(armed["v"], arm_consumed):
                # Flush EVERY pre-arm (about:blank / pre-nav) damage without capturing, then IMMEDIATELY grab the
                # current ready (navigated) page as frame zero. Do not wait for a later damage. A DestroyNotify
                # queued in this arm window must STILL be reconciled here (only the pre-arm DAMAGE is discarded), or
                # a window destroyed just before arm keeps a stale entry a same-XID reuse would then inherit.
                X.XSync(dpy, 0)
                while X.XPending(dpy) > 0:
                    ev = _XEvent()
                    X.XNextEvent(dpy, ctypes.byref(ev))
                    if ev.type == DestroyNotify:
                        gone = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(_XSubstructureEvent)).contents.window
                        if reconcile_destroyed_window(damaged, gone, root):
                            windows_reconciled += 1
                for _w, dh in list(damaged.items()):
                    XD.XDamageSubtract(dpy, dh, 0, 0)
                arm_consumed = True
                pending_dirty = False
                capture_and_write()  # frame zero = the ready page
                last_capture = time.monotonic()
                continue
            if should_emit(arm_consumed, pending_dirty, (now - last_capture) >= min_interval):
                capture_and_write()
                last_capture = now
                pending_dirty = False

        if should_write_terminal(frames["n"]):
            capture_and_write()  # terminal frame -> finalized duration reflects real elapsed time
        try:
            if ffp.stdin is not None:
                ffp.stdin.close()
        except OSError:
            pass
        try:
            ffp.wait(timeout=90)
        except subprocess.TimeoutExpired:
            ffp.kill()
    finally:
        # Exactly-once release of every owned XDamage handle (root + survivors); reached on normal stop,
        # cancellation, or an exception, so no owned handle outlives its capture phase.
        _destroy_all_handles(XD, X, dpy, damaged)

    try:
        XE.XShmDetach(dpy, ctypes.byref(shm_info))
        LIBC.shmdt(addr)
    except Exception:  # noqa: BLE001,S110 - best-effort shm detach at process exit; segment is IPC_RMID-marked
        pass

    # Zero-frame truth: remove the header-only output so the parent publishes no bogus recording. UNARMED+0 is a
    # clean pre-page teardown (rc 0); a consumed OR delivered-but-not-yet-consumed arm (SIGUSR1 latched in the
    # stop's loop interval) with zero frames is a capture failure (nonzero + unlink), not a graceful teardown.
    arm_effective = armed["v"] or arm_consumed
    rc = _finalize_return_code(frames["n"], ffp.returncode, arm_effective)
    if should_unlink_output(frames["n"]):
        try:
            os.unlink(args.out)
        except OSError:
            pass

    stats = {
        "frames_written": frames["n"],
        "bytes_written": frames["bytes"],
        "short_writes": frames["short_writes"],
        "grab_fail": frames["grab_fail"],
        "armed": arm_effective,
        "expected_frame_bytes": frame_bytes,
        "bytes_consistent": frames["short_writes"] == 0 and frames["bytes"] == frames["n"] * frame_bytes,
        "pix_fmt": pix_fmt,
        "bpp": bpp,
        "windows_armed": windows_armed,
        "windows_pruned": windows_pruned,
        "windows_reconciled": windows_reconciled,
        "damage_err_count": err["n"],
        "first_frame_epoch": first_frame_epoch,
        "last_frame_epoch": time.time(),
        "ffmpeg_returncode": ffp.returncode,
        "codec": "h264",
        "container": "mp4",
    }
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats))
    return rc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="skyvern.webeye.display_capture_bridge")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":99"))
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--out-size", default="1280x720")
    ap.add_argument("--max-fps", type=int, default=15)
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--keyframe-sec", type=int, default=5)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats", default="")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return _run(_parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
