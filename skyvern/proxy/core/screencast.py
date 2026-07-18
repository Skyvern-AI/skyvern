"""Screencast protection at the proxy boundary (SKY-12502), as data plus two pure rules.

Chrome's screencast is ACK-DRIVEN: the browser holds a small number of frames in flight
and emits the next one only once an earlier one comes back acked. The ack, not the event,
is the flow-control signal — which is what makes a rate cap here different in kind from
every other rule in this package. A frame the proxy withholds is a frame the client never
sees and so never acks, so once the in-flight allowance is spent the browser stops
emitting altogether: an unacked cap does not throttle the stream, it stalls it to 0 FPS
and never recovers. The proxy therefore owes the browser an ack for every frame it
withholds (`screencast_frame_ack`), and the driving adapter must not withhold a frame it
cannot ack.

The two halves bound different legs, and neither covers the other:

- The rate cap bounds what the CLIENT is delivered. It cannot make the browser cheaper:
  acking a withheld frame immediately is what keeps the stream alive, and that same ack
  releases the browser to encode the next frame at once.
- `bound_start_screencast` bounds what the BROWSER produces, by clamping the capture
  params the client asks for. It is the only lever here that makes the upstream leg
  cheaper, which is why the cap alone is not the whole ticket.

Scope is the EXTERNAL proxied path: clients driving Page.startScreencast through the
proxy. Cloud live-view is VNC + Page.captureScreenshot and never touches this.
"""

from __future__ import annotations

from skyvern.proxy.core.frames import CdpCommand, CdpEvent, CdpFrame
from skyvern.proxy.core.pipeline import Direction, MiddlewarePipeline
from skyvern.proxy.core.policy import EventPolicyConfig, RateRule
from skyvern.proxy.core.session import ProxySession

SCREENCAST_FRAME_EVENT = "Page.screencastFrame"
SCREENCAST_FRAME_ACK_METHOD = "Page.screencastFrameAck"
START_SCREENCAST_METHOD = "Page.startScreencast"

# Frames delivered per session per second. A remote client reads a screencast to watch a
# page, not to sample every composited frame, and the frontend consumers of a screencast
# already tolerate frame loss. Deliberately a ceiling on the pathological case rather
# than a target anyone should feel.
SCREENCAST_MAX_FRAMES_PER_SECOND = 10

# Ceiling on the frame the browser encodes. Chrome scales to fit maxWidth/maxHeight and
# still reports the true page size in each frame's metadata, so a client mapping frame
# coordinates back to the page can. Applied when absent too: Chrome reads a missing bound
# as no bound and encodes at full viewport size, which is the expensive default this
# exists to stop.
SCREENCAST_MAX_DIMENSION = 1280

# Ceiling on JPEG quality, which is what most directly decides a frame's size. Bounds
# bytes where the dimension bound cannot: a full-size-but-cheap frame and a
# small-but-pristine one cost very differently. 60 is what this product's own live-view
# asks for, so it is a known-legible picture rather than a guessed number. `format` is
# deliberately NOT rewritten: quality trades fidelity within a codec the client already
# chose, whereas swapping the codec changes what its decoder is handed.
SCREENCAST_MAX_QUALITY = 60

# Floor on everyNthFrame: the browser skips capture entirely for the frames in between,
# so this is what actually spends less CPU upstream. Coarse on purpose — it divides the
# page's own composite rate, which the proxy cannot see, so it cannot be solved for a
# target FPS. A page composing below twice the cap is delivered under the cap as a
# result; the cap is a ceiling, not a promise, and this is the knob to raise if the
# upstream leg needs to be cheaper still.
SCREENCAST_MIN_EVERY_NTH_FRAME = 2

# Keyed on the method alone, so the budget is per PROXY SESSION and a client screencasting
# several pages shares one budget across them (AC: cap delivered frame volume per session).
# The engine keys windows on params, and a frame's only distinguishing param is its
# ever-changing frame number, so per-page budgets would need the engine to key on the
# frame's CDP session — worth doing if one busy page is ever seen starving a quiet one.
SCREENCAST_PACK_V1 = EventPolicyConfig(
    version=1,
    rules=(
        RateRule.throttle(
            SCREENCAST_FRAME_EVENT,
            max_per_window=SCREENCAST_MAX_FRAMES_PER_SECOND,
            window_seconds=1.0,
        ),
    ),
)

# For the driving adapter's metric-label allowlist, derived from the rules so the two
# cannot drift (SKY-12510 bounds labels; this list is static, not per-request).
SCREENCAST_PACK_EVENT_METHODS = tuple(rule.method for rule in SCREENCAST_PACK_V1.rules)


def screencast_frame_ack(frame: CdpFrame | None) -> CdpCommand | None:
    """The ack `frame` owes the browser, or None if it owes none — because it is not a
    screencast frame at all, or is one carrying no frame number to name.

    Total on purpose, so one question ("what ack does this frame owe?") can be asked of
    any frame. Asking it of the frame that arrived and of the frame actually delivered,
    and comparing, is what tells a caller whether the CLIENT's ack will cover the
    original or whether the proxy still owes it (equal commands mean the delivered frame
    names the same frame number on the same session, so the client's own ack settles it).

    `params.sessionId` on a screencast frame is a FRAME NUMBER, not a CDP session id — the
    screencast domain reuses the name for an unrelated integer. The session this ack
    belongs to is the frame's OWN session_id. Reading params.sessionId as a session id
    (`params_session_id`, which requires a string) returns None for every real frame and
    would ack nothing at all.

    `id` is a placeholder: the caller allocates the real one when it remaps the command
    onto the proxy's own lane (`RequestIdRemapper.to_upstream_as_proxy`). It is fixed
    rather than incidental so that two acks for the same frame compare equal.
    """
    if not isinstance(frame, CdpEvent) or frame.method != SCREENCAST_FRAME_EVENT:
        return None
    number = (frame.params or {}).get("sessionId")
    # bool is an int in Python, and True would ack frame 1 — a frame the browser may
    # genuinely still be waiting on.
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return CdpCommand(
        id=0,
        method=SCREENCAST_FRAME_ACK_METHOD,
        params={"sessionId": number},
        session_id=frame.session_id,
    )


def is_screencast_frame(frame: CdpFrame | None) -> bool:
    """Whether the browser is waiting on an ack for this frame.

    Distinct from `screencast_frame_ack(frame) is not None`: a screencast frame with no
    usable frame number owes an ack that cannot be built, and the two cases have opposite
    dispositions — one is withheld once paid, the other must never be withheld at all.
    """
    return isinstance(frame, CdpEvent) and frame.method == SCREENCAST_FRAME_EVENT


def _bounded_dimension(value: object) -> int:
    # A non-positive or non-integer value is not a modest request: Chrome ignores it and
    # encodes unbounded, so it lands on the ceiling rather than being honoured as-is.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, SCREENCAST_MAX_DIMENSION)
    return SCREENCAST_MAX_DIMENSION


def _bounded_every_nth_frame(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return max(value, SCREENCAST_MIN_EVERY_NTH_FRAME)
    return SCREENCAST_MIN_EVERY_NTH_FRAME


def _bounded_quality(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
        return min(value, SCREENCAST_MAX_QUALITY)
    return SCREENCAST_MAX_QUALITY


def bound_start_screencast(command: CdpCommand) -> CdpCommand:
    """Clamp a client's Page.startScreencast into the range the proxy will serve.

    Every bound is applied whether or not the client asked, because each param's absent
    default is its expensive one: no dimension bound means full viewport, no
    everyNthFrame means every frame, and no quality means the browser's own high default.
    Absent is the common case, so it cannot be the unbounded one. `format` rides through
    untouched (see SCREENCAST_MAX_QUALITY).
    """
    params = dict(command.params or {})
    params["maxWidth"] = _bounded_dimension(params.get("maxWidth"))
    params["maxHeight"] = _bounded_dimension(params.get("maxHeight"))
    params["everyNthFrame"] = _bounded_every_nth_frame(params.get("everyNthFrame"))
    params["quality"] = _bounded_quality(params.get("quality"))
    return CdpCommand(id=command.id, method=command.method, params=params, session_id=command.session_id)


async def bound_screencast_middleware(frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame:
    if (
        direction is Direction.CLIENT_TO_UPSTREAM
        and isinstance(frame, CdpCommand)
        and frame.method == START_SCREENCAST_METHOD
    ):
        return bound_start_screencast(frame)
    return frame


def screencast_pipeline() -> MiddlewarePipeline:
    """The command half of the screencast policy.

    Param bounding is a command rewrite, and EventPolicyPort decides events only, so the
    two halves of this one policy are wired through different seams and a deployment must
    turn both on together (see `skyvern.proxy.__main__`). Rewriting commands in general is
    the interception seam's job (SKY-12535); this is the one rule that cannot wait for it.
    """
    return MiddlewarePipeline([bound_screencast_middleware])


__all__ = [
    "SCREENCAST_FRAME_ACK_METHOD",
    "SCREENCAST_FRAME_EVENT",
    "SCREENCAST_MAX_DIMENSION",
    "SCREENCAST_MAX_FRAMES_PER_SECOND",
    "SCREENCAST_MAX_QUALITY",
    "SCREENCAST_MIN_EVERY_NTH_FRAME",
    "SCREENCAST_PACK_EVENT_METHODS",
    "SCREENCAST_PACK_V1",
    "START_SCREENCAST_METHOD",
    "bound_screencast_middleware",
    "bound_start_screencast",
    "is_screencast_frame",
    "screencast_frame_ack",
    "screencast_pipeline",
]
