from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any

from skyvern.services.browser_recording.v2.ledger import Effect, Gesture, GestureLedger

CLICK_NAVIGATION_WINDOW_MS = 3000
SETTLE_QUIET_MS = 500
SETTLE_MAX_MS = 15000
BOOKKEEPING_CAPACITY = 4096


def _str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


class Observer:
    def __init__(self, ledger: GestureLedger) -> None:
        self.ledger = ledger
        self._attached_page_sessions: set[int] = set()
        self._attached_browser_sessions: set[int] = set()
        self._listeners: list[tuple[Any, str, Callable[[dict[str, Any]], None]]] = []
        self._frame_pages: dict[str, str] = {}
        self._pending_navigations: dict[str, Effect] = {}
        self._last_activity: dict[str, float] = {}
        self._settle_timers: dict[str, asyncio.TimerHandle] = {}
        self._settled_gestures: dict[tuple[str, int], None] = {}
        self._target_urls: dict[str, str | None] = {}
        self._main_frames: dict[str, str] = {}

    async def attach_page_session(self, page_key: str, cdp_session: Any) -> None:
        session_key = id(cdp_session)
        if session_key in self._attached_page_sessions:
            return

        self._remove_listeners(self._attached_page_sessions)
        self._attached_page_sessions.clear()

        def frame_started(payload: dict[str, Any]) -> None:
            frame_id = _str(payload, "frameId")
            self._on_navigation(page_key, frame_id, _str(payload, "url"), True, self._is_main_frame(page_key, frame_id))

        def frame_navigated(payload: dict[str, Any]) -> None:
            frame = payload.get("frame")
            if not isinstance(frame, dict):
                return
            frame_id = _str(frame, "id")
            is_main_frame = _str(frame, "parentId") is None
            if is_main_frame and frame_id is not None:
                self._main_frames[page_key] = frame_id
            self._on_navigation(page_key, frame_id, _str(frame, "url"), False, is_main_frame)

        def within_document(payload: dict[str, Any]) -> None:
            frame_id = _str(payload, "frameId")
            if frame_id is not None:
                self._remember_frame_page(frame_id, page_key)
            self._append_navigation(
                page_key, frame_id, _str(payload, "url"), monotonic(), self._is_main_frame(page_key, frame_id)
            )

        listeners: dict[str, Callable[[dict[str, Any]], None]] = {
            "Page.frameStartedNavigating": frame_started,
            "Page.frameNavigated": frame_navigated,
            "Page.navigatedWithinDocument": within_document,
            "Network.requestWillBeSent": partial(self._on_network_activity, page_key),
            "Network.loadingFinished": partial(self._on_network_activity, page_key),
            "Network.loadingFailed": partial(self._on_network_activity, page_key),
            # Chrome emits fileChooserOpened only while Page.setInterceptFileChooserDialog is enabled; v2
            # deliberately does not enable interception until PR-9, so this row is silent until then.
            "Page.fileChooserOpened": partial(self._on_file_chooser, page_key),
            "Page.javascriptDialogOpening": partial(self._on_dialog, page_key),
        }
        self._register_listeners(cdp_session, listeners)
        try:
            await asyncio.gather(
                cdp_session.send("Page.enable", {}),
                cdp_session.send("Network.enable", {}),
            )
        except Exception:
            self._remove_listeners({session_key})
            raise
        await self._remember_main_frame(page_key, cdp_session)
        self._attached_page_sessions.add(session_key)

    async def attach_browser_session(self, cdp_session: Any) -> None:
        session_key = id(cdp_session)
        if session_key in self._attached_browser_sessions:
            return

        self._register_listeners(
            cdp_session,
            {
                "Target.targetCreated": partial(self._on_target, True),
                "Target.targetInfoChanged": partial(self._on_target, False),
            },
        )
        try:
            await cdp_session.send("Target.setDiscoverTargets", {"discover": True})
        except Exception:
            self._remove_listeners({session_key})
            raise
        self._attached_browser_sessions.add(session_key)

    def detach(self) -> None:
        for timer in self._settle_timers.values():
            timer.cancel()
        self._settle_timers.clear()
        for session, event, callback in self._listeners:
            with contextlib.suppress(Exception):
                session.remove_listener(event, callback)
        self._listeners.clear()
        self._attached_page_sessions.clear()
        self._attached_browser_sessions.clear()
        self._frame_pages.clear()
        self._pending_navigations.clear()
        self._last_activity.clear()
        self._settled_gestures.clear()
        self._target_urls.clear()
        self._main_frames.clear()

    def _register_listeners(
        self,
        cdp_session: Any,
        listeners: dict[str, Callable[[dict[str, Any]], None]],
    ) -> None:
        for event, callback in listeners.items():
            cdp_session.on(event, callback)
            self._listeners.append((cdp_session, event, callback))

    def _remove_listeners(self, session_keys: set[int]) -> None:
        retained = []
        for session, event, callback in self._listeners:
            if id(session) not in session_keys:
                retained.append((session, event, callback))
                continue
            with contextlib.suppress(Exception):
                session.remove_listener(event, callback)
        self._listeners = retained

    async def _remember_main_frame(self, page_key: str, cdp_session: Any) -> None:
        # Only main-frame navigations become steps, and frameStartedNavigating/navigatedWithinDocument
        # do not say which frame is the main one.
        frame_id: Any = None
        with contextlib.suppress(Exception):
            frame_tree = await cdp_session.send("Page.getFrameTree", {})
            frame_id = frame_tree["frameTree"]["frame"]["id"]
        if isinstance(frame_id, str):
            self._main_frames[page_key] = frame_id

    def _is_main_frame(self, page_key: str, frame_id: str | None) -> bool:
        return frame_id is not None and self._main_frames.get(page_key) == frame_id

    def _remember_frame_page(self, frame_id: str, page_key: str) -> None:
        self._frame_pages.pop(frame_id, None)
        self._frame_pages[frame_id] = page_key
        if len(self._frame_pages) > BOOKKEEPING_CAPACITY:
            self._frame_pages.pop(next(iter(self._frame_pages)))

    def _on_navigation(
        self, page_key: str, frame_id: str | None, url: str | None, is_start: bool, is_main_frame: bool
    ) -> None:
        if frame_id is not None:
            self._remember_frame_page(frame_id, page_key)
        received_at = monotonic()
        if not is_start and frame_id is not None:
            pending = self._pending_navigations.pop(frame_id, None)
            if (
                pending is not None
                and not self.ledger.paused
                and 0 <= received_at - pending.t_received <= CLICK_NAVIGATION_WINDOW_MS / 1000
            ):
                pending.url = url
                pending.is_main_frame = pending.is_main_frame or is_main_frame
                return
        effect = self._append_navigation(page_key, frame_id, url, received_at, is_main_frame)
        if is_start and frame_id is not None and effect is not None:
            self._pending_navigations[frame_id] = effect

    def _append_navigation(
        self,
        page_key: str,
        frame_id: str | None,
        url: str | None,
        received_at: float,
        is_main_frame: bool,
    ) -> Effect | None:
        gesture = self._recent_navigation_gesture(page_key, received_at)
        return self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=received_at,
                kind="navigation",
                page_key=page_key,
                url=url,
                frame_id=frame_id,
                caused_by_seq=gesture.seq if gesture is not None else None,
                is_main_frame=is_main_frame,
            )
        )

    def _recent_navigation_gesture(self, page_key: str, received_at: float) -> Gesture | None:
        for gesture in self.ledger.iter_recent():
            if gesture.page_key != page_key:
                continue
            if gesture.kind != "mouse_pressed" and not (gesture.kind == "key" and gesture.key == "Enter"):
                continue
            elapsed = received_at - gesture.t_received
            if 0 <= elapsed <= CLICK_NAVIGATION_WINDOW_MS / 1000:
                return gesture
            return None
        return None

    def _on_network_activity(self, page_key: str, payload: dict[str, Any]) -> None:
        frame_id = _str(payload, "frameId")
        if frame_id is not None:
            self._remember_frame_page(frame_id, page_key)
        received_at = monotonic()
        self._last_activity[page_key] = received_at
        timer = self._settle_timers.pop(page_key, None)
        if timer is not None:
            timer.cancel()
        gesture = self._recent_gesture(page_key, received_at)
        if gesture is None or (page_key, gesture.seq) in self._settled_gestures:
            return
        elapsed = received_at - gesture.t_received
        if elapsed >= SETTLE_MAX_MS / 1000:
            self._append_network_settle(page_key, gesture.seq, gesture.t_received)
            return
        loop = asyncio.get_running_loop()
        self._settle_timers[page_key] = loop.call_later(
            min(SETTLE_QUIET_MS / 1000, SETTLE_MAX_MS / 1000 - elapsed),
            self._append_network_settle,
            page_key,
            gesture.seq,
            gesture.t_received,
        )

    def _recent_gesture(self, page_key: str, received_at: float) -> Gesture | None:
        for gesture in self.ledger.iter_recent():
            if gesture.kind in {"mouse_moved", "wheel", "mouse_released"}:
                continue
            if gesture.page_key == page_key and gesture.t_received <= received_at:
                return gesture
        return None

    def _append_network_settle(self, page_key: str, gesture_seq: int, gesture_received_at: float) -> None:
        self._settle_timers.pop(page_key, None)
        if (page_key, gesture_seq) in self._settled_gestures:
            return
        last_activity = self._last_activity[page_key]
        capped = monotonic() - gesture_received_at >= SETTLE_MAX_MS / 1000
        self._settled_gestures[(page_key, gesture_seq)] = None
        if len(self._settled_gestures) > BOOKKEEPING_CAPACITY:
            self._settled_gestures.pop(next(iter(self._settled_gestures)))
        self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=monotonic(),
                kind="network_settle",
                page_key=page_key,
                caused_by_seq=gesture_seq,
                busy_ms=SETTLE_MAX_MS if capped else max(0, round((last_activity - gesture_received_at) * 1000)),
                detail={"capped": True} if capped else {},
            )
        )

    def _on_file_chooser(self, page_key: str, payload: dict[str, Any]) -> None:
        frame_id = _str(payload, "frameId")
        if frame_id is not None:
            self._remember_frame_page(frame_id, page_key)
        detail: dict[str, str | int | bool] = {}
        mode = _str(payload, "mode")
        if mode is not None:
            detail["mode"] = mode
        if isinstance(payload.get("backendNodeId"), int):
            detail["backendNodeId"] = payload["backendNodeId"]
        self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=monotonic(),
                kind="file_chooser",
                page_key=page_key,
                frame_id=frame_id,
                detail=detail,
            )
        )

    def _on_dialog(self, page_key: str, payload: dict[str, Any]) -> None:
        detail: dict[str, str | int | bool] = {}
        dialog_type = _str(payload, "type")
        if dialog_type is not None:
            detail["type"] = dialog_type
        self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=monotonic(),
                kind="dialog",
                page_key=page_key,
                detail=detail,
            )
        )

    def _on_target(self, is_created: bool, payload: dict[str, Any]) -> None:
        target_info = payload.get("targetInfo")
        if not isinstance(target_info, dict):
            return
        target_id = _str(target_info, "targetId")
        target_type = _str(target_info, "type")
        url = _str(target_info, "url")
        if target_id is None or target_type != "page":
            return
        if is_created and target_id in self._target_urls:
            return
        if not is_created and target_id in self._target_urls and self._target_urls[target_id] == url:
            return
        self._target_urls.pop(target_id, None)
        self._target_urls[target_id] = url
        if len(self._target_urls) > BOOKKEEPING_CAPACITY:
            self._target_urls.pop(next(iter(self._target_urls)))
        self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=monotonic(),
                kind="target",
                page_key=target_id,
                url=url,
                target_id=target_id,
                detail={"type": target_type},
            )
        )


observers: dict[str, Observer] = {}


def _unregister_observer(browser_session_id: str, observer: Observer) -> None:
    if observers.get(browser_session_id) is observer:
        observers.pop(browser_session_id)


def start_observer(browser_session_id: str, ledger: GestureLedger) -> Observer:
    observer = observers.get(browser_session_id)
    if observer is None:
        observer = Observer(ledger)
        observers[browser_session_id] = observer
        ledger.on_stop(observer.detach)
        ledger.on_stop(partial(_unregister_observer, browser_session_id, observer))
    return observer


def get_observer(browser_session_id: str) -> Observer | None:
    return observers.get(browser_session_id)


def stop_observer(browser_session_id: str) -> Observer | None:
    observer = observers.pop(browser_session_id, None)
    if observer is not None:
        observer.detach()
    return observer
