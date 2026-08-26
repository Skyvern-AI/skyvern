import functools
from collections.abc import Callable
from typing import Any, ParamSpec

import structlog

from skyvern.services.browser_recording.v2.ledger import Gesture, GestureKind, GestureLedger, get_ledger
from skyvern.services.browser_recording.v2.resolver import get_resolver

LOG = structlog.get_logger(__name__)
P = ParamSpec("P")


def _never_raises(tap: Callable[P, None]) -> Callable[P, None]:
    # The taps sit on the live input path; a recording bug must never close the user's input channel.
    @functools.wraps(tap)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            tap(*args, **kwargs)
        except Exception:
            LOG.warning("Record Browser v2 tap failed; gesture dropped", tap=tap.__name__, exc_info=True)

    return guarded


_MOUSE_KINDS: dict[str, GestureKind] = {
    "mousePressed": "mouse_pressed",
    "mouseReleased": "mouse_released",
    "mouseMoved": "mouse_moved",
}
_NAVIGATION_KINDS: dict[str, GestureKind] = {
    "navigateEvent": "navigate",
    "goBackEvent": "go_back",
    "goForwardEvent": "go_forward",
    "reloadEvent": "reload",
}


def _ledger_for(browser_session_id: str | None) -> GestureLedger | None:
    if browser_session_id is None:
        return None
    return get_ledger(browser_session_id)


@_never_raises
def tap_pipelined(
    browser_session_id: str | None,
    kind: str,
    validated: dict,
    received_at: float,
    page: Any,
    cdp_session: Any,
) -> None:
    if (ledger := _ledger_for(browser_session_id)) is None:
        return

    gesture_kind: GestureKind | None
    if kind == "mouseEvent":
        gesture_kind = _MOUSE_KINDS.get(validated.get("type") or "")
    elif kind == "wheelEvent":
        gesture_kind = "wheel"
    elif kind == "keyEvent":
        gesture_kind = "key"
    else:
        gesture_kind = None
    if gesture_kind is None:
        return

    gesture = ledger.append(
        Gesture(
            seq=0,
            t_received=received_at,
            kind=gesture_kind,
            page_key=str(id(page)),
            url=getattr(page, "url", ""),
            x=validated.get("x"),
            y=validated.get("y"),
            button=validated.get("button"),
            click_count=validated.get("clickCount"),
            modifiers=validated.get("modifiers", 0),
            key=validated.get("key"),
            code=validated.get("code"),
            text=validated.get("text"),
            windows_virtual_key_code=validated.get("windowsVirtualKeyCode"),
            key_event_type=validated.get("type") if kind == "keyEvent" else None,
            delta_x=validated.get("deltaX"),
            delta_y=validated.get("deltaY"),
        )
    )
    if (
        gesture is not None
        and gesture_kind == "mouse_pressed"
        and (resolver := get_resolver(ledger.browser_session_id)) is not None
    ):
        resolver.on_gesture(gesture, cdp_session, page)


@_never_raises
def tap_navigation(
    browser_session_id: str | None,
    kind: str,
    msg: dict,
    received_at: float,
    page: Any,
) -> None:
    if (ledger := _ledger_for(browser_session_id)) is None or (gesture_kind := _NAVIGATION_KINDS.get(kind)) is None:
        return

    ledger.append(
        Gesture(
            seq=0,
            t_received=received_at,
            kind=gesture_kind,
            page_key=str(id(page)),
            url=getattr(page, "url", ""),
            target_url=msg.get("url") if kind == "navigateEvent" else None,
        )
    )


@_never_raises
def tap_paste(browser_session_id: str | None, text: str, received_at: float) -> None:
    if (ledger := _ledger_for(browser_session_id)) is None:
        return
    rows = ledger.rows()
    previous = rows[-1] if rows else None

    ledger.append(
        Gesture(
            seq=0,
            t_received=received_at,
            kind="paste",
            page_key=previous.page_key if previous else "",
            url=previous.url if previous else "",
            text=text,
        )
    )
