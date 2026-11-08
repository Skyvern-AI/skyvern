from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit

import structlog
from playwright.async_api import BrowserContext, CDPSession, Frame, Page

from skyvern.forge.sdk.copilot.composition_browser_expressions import scout_pre_action_packet_js

WORLD = "skyvern-demonstration"
TAP_SOURCE = scout_pre_action_packet_js() + Path(__file__).with_name("demonstration_tap.js").read_text()
# A restarted tap reuses the same isolated world, where the prelude's top-level declarations already exist.
TAP_PROGRAM = f"(() => {{\n{TAP_SOURCE}\n}})()"
USER_NAVIGATION_TRANSITIONS = {"typed", "address_bar", "generated", "auto_bookmark", "keyword"}
DEMONSTRATION_MAX_EVENTS = 1000
CLICK_NAVIGATION_REASONS = {"anchorClick", "formSubmissionGet", "formSubmissionPost"}
PRESS_RECOVERY_WAIT_SECONDS = 4
CDP_READ_TIMEOUT_SECONDS = 3
# A click or key read this close to a link/form navigation request is taken as the one that caused it.
ACTIVATION_EXPLAINS_NAVIGATION_SECONDS = 2

LOG = structlog.get_logger()


@dataclass(frozen=True)
class TapEvent:
    page: Page
    session_key: str
    frame_id: str
    is_main_frame: bool
    frame_name: str
    payload: dict[str, Any]


class PendingPress(TypedDict):
    stream: str
    payload: dict[str, Any]


class DocumentActivity(TypedDict):
    # (target URL, monotonic time the click was read)
    click_targets: list[tuple[str, float]]
    last_activation_at: float | None
    last_ts: float | None
    url: str


class TapSessionState(TypedDict):
    session: CDPSession
    page: Page
    target_frame: Frame | None
    is_page_session: bool
    script_id: str | None
    frames: dict[str, dict[str, Any]]
    main_frame_id: str
    seen_navigation_entry_ids: set[str]
    main_frame_navigations: int
    drain_acks: dict[str, tuple[str | None, int]]
    seen_event_sequences: dict[str, dict[str, int]]
    worlds: dict[str, int]
    # Press bookkeeping is keyed by (frame id, document generation): one entry per page load of a frame.
    pending_presses: dict[tuple[str, int], PendingPress]
    clicked_presses: dict[tuple[str, int], dict[str, int]]
    click_navigations: dict[str, tuple[int, str, float]]
    document_activity: dict[tuple[str, int], DocumentActivity]
    recovering_documents: set[tuple[str, int]]
    document_generations: dict[str, int]
    inflight_drains: dict[tuple[str, int], int]


class PageTap:
    def __init__(self, context: BrowserContext, *, max_events: int = DEMONSTRATION_MAX_EVENTS) -> None:
        self.context = context
        self._events: asyncio.Queue[TapEvent] = asyncio.Queue(maxsize=max_events)
        self._event_limit_reached = False
        self._sessions: dict[str, TapSessionState] = {}
        self._page_sessions: dict[int, str] = {}
        self._oopif_sessions: dict[Frame, str] = {}
        self._refused_frames: set[tuple[Frame, str]] = set()
        self._poll_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._installing_pages: set[int] = set()
        self._session_ids = count(1)
        self._scan_task: asyncio.Task[None] | None = None
        self._page_listener: Any = None
        self._intercept_file_chooser = False
        self._control_navigation_depth = 0
        self._stopped = False
        self._recovery_tasks: set[asyncio.Task[Any]] = set()

    @property
    def event_limit_reached(self) -> bool:
        return self._event_limit_reached

    def _enqueue_event(self, event: TapEvent) -> bool:
        if self._event_limit_reached:
            return False
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self._event_limit_reached = True
            return False
        return True

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def start(self, pages: Any) -> None:
        if self._stopped:
            return
        if self._page_listener is None:

            def on_page(page: Page) -> None:
                self._spawn(self._install_page(page))

            self._page_listener = on_page
            self.context.on("page", on_page)
        await asyncio.gather(*(self._install_page(page) for page in pages))
        await self._scan_once()
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(self.poll_new_frames())

    async def _install_page(self, page: Page) -> None:
        page_key = id(page)
        if self._stopped or page_key in self._page_sessions or page_key in self._installing_pages:
            return
        self._installing_pages.add(page_key)
        session: CDPSession | None = None
        key: str | None = None
        try:
            session = await self.context.new_cdp_session(page)
            key = await self._install_session(session, page, target_frame=None, is_page_session=True)
            if key is not None:
                self._page_sessions[page_key] = key
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return
        finally:
            if session is not None and key is None:
                with contextlib.suppress(Exception):
                    await session.detach()
            self._installing_pages.discard(page_key)

    async def _install_oopif(self, page: Page, frame: Frame) -> None:
        refused_key = (frame, frame.url)
        if self._stopped or frame in self._oopif_sessions or refused_key in self._refused_frames:
            return
        self._refused_frames = {key for key in self._refused_frames if key[0] is not frame}
        session: CDPSession | None = None
        key: str | None = None
        try:
            session = await self.context.new_cdp_session(frame)
            key = await self._install_session(session, page, target_frame=frame, is_page_session=False)
            if key is not None:
                self._oopif_sessions[frame] = key
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._refused_frames.add(refused_key)
            return
        finally:
            if session is not None and key is None:
                with contextlib.suppress(Exception):
                    await session.detach()

    async def _install_session(
        self,
        session: CDPSession,
        page: Page,
        *,
        target_frame: Frame | None,
        is_page_session: bool,
    ) -> str | None:
        try:
            await session.send("Page.enable")
            if self._intercept_file_chooser:
                with contextlib.suppress(Exception):
                    await session.send("Page.setInterceptFileChooserDialog", {"enabled": True})
            installed = await session.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": TAP_PROGRAM, "worldName": WORLD},
            )
            tree = await session.send("Page.getFrameTree")
            # The new-document script covers later documents; already loaded frames of this session are installed here.
            pending = [tree["frameTree"]]
            while pending:
                node = pending.pop()
                pending.extend(node.get("childFrames") or [])
                world = await session.send(
                    "Page.createIsolatedWorld",
                    {"frameId": str(node["frame"]["id"]), "worldName": WORLD, "grantUniveralAccess": False},
                )
                evaluated = await session.send(
                    "Runtime.evaluate",
                    {"expression": TAP_PROGRAM, "contextId": world["executionContextId"], "returnByValue": True},
                )
                if evaluated.get("exceptionDetails"):
                    LOG.warning(
                        "Demonstration tap install failed in a loaded frame", details=evaluated["exceptionDetails"]
                    )
                    raise RuntimeError("demonstration tap install failed")
            seen_navigation_entry_ids: set[str] = set()
            if is_page_session:
                entries = (await session.send("Page.getNavigationHistory")).get("entries")
                if isinstance(entries, list):
                    seen_navigation_entry_ids.update(str(entry["id"]) for entry in entries)
        except Exception:  # noqa: BLE001
            return None
        if self._stopped:
            with contextlib.suppress(Exception):
                await session.send(
                    "Page.removeScriptToEvaluateOnNewDocument", {"identifier": installed.get("identifier")}
                )
            return None
        key = f"tap-{next(self._session_ids)}"
        self._sessions[key] = {
            "session": session,
            "page": page,
            "target_frame": target_frame,
            "is_page_session": is_page_session,
            "script_id": installed.get("identifier"),
            "frames": {},
            "main_frame_id": str(tree["frameTree"]["frame"]["id"]),
            "seen_navigation_entry_ids": seen_navigation_entry_ids,
            "main_frame_navigations": 0,
            "drain_acks": {},
            "seen_event_sequences": {},
            "worlds": {},
            "pending_presses": {},
            "clicked_presses": {},
            "click_navigations": {},
            "recovering_documents": set(),
            "document_activity": {},
            "document_generations": {},
            "inflight_drains": {},
        }
        self._reconcile_frames(key, tree)
        session.on("Page.frameNavigated", lambda event: self._frame_navigated(key, event))
        session.on("Page.frameRequestedNavigation", lambda event: self._frame_requested_navigation(key, event))
        session.on("Page.fileChooserOpened", lambda event: self._file_chooser_opened(key, event))
        return key

    def _file_chooser_opened(self, session_key: str, event: dict[str, Any]) -> None:
        state = self._sessions.get(session_key)
        backend_node_id = event.get("backendNodeId")
        if state is None or not isinstance(backend_node_id, int):
            return
        frame_id = str(event.get("frameId") or "")
        self._enqueue_event(
            TapEvent(
                page=state["page"],
                session_key=session_key,
                frame_id=frame_id,
                is_main_frame=bool(state["is_page_session"] and frame_id == state["main_frame_id"]),
                frame_name=str(state["frames"].get(frame_id, {}).get("name") or ""),
                payload={"kind": "file_chooser", "ts": time.time() * 1000, "backend_node_id": backend_node_id},
            )
        )

    async def call_on_file_input(self, event: TapEvent, declaration: str, *args: Any) -> Any:
        state = self._sessions.get(event.session_key)
        if state is None:
            return None
        session = state["session"]
        try:
            world = await session.send(
                "Page.createIsolatedWorld",
                {"frameId": event.frame_id, "worldName": WORLD, "grantUniveralAccess": False},
            )
            resolved = await session.send(
                "DOM.resolveNode",
                {
                    "backendNodeId": event.payload["backend_node_id"],
                    "executionContextId": world["executionContextId"],
                },
            )
            result = await session.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": declaration,
                    "objectId": resolved["object"]["objectId"],
                    "arguments": [{"value": arg} for arg in args],
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
        except Exception:
            LOG.info("copilot_demonstration_file_input_call_failed", exc_info=True)
            return None
        return result.get("result", {}).get("value")

    def _frame_requested_navigation(self, session_key: str, event: dict[str, Any]) -> None:
        state = self._sessions.get(session_key)
        frame_id = str(event.get("frameId") or "")
        if state is not None and frame_id in state["frames"] and event.get("disposition") == "currentTab":
            if event.get("reason") in CLICK_NAVIGATION_REASONS:
                state["click_navigations"][frame_id] = (
                    state["document_generations"].get(frame_id, 0),
                    str(event.get("url") or ""),
                    time.monotonic(),
                )
            self._spawn(
                self._drain_frame(
                    session_key,
                    frame_id,
                    max_wait_ms=0,
                    context_id=state["worlds"].get(frame_id),
                )
            )

    def _frame_navigated(self, session_key: str, event: dict[str, Any]) -> None:
        state = self._sessions.get(session_key)
        frame = event.get("frame")
        if state is None or not isinstance(frame, dict):
            return
        frame_id = str(frame.get("id") or "")
        previous_url = str(state["frames"].get(frame_id, {}).get("url") or "")
        state["frames"][frame_id] = {**state["frames"].get(frame_id, {}), **frame}
        state["worlds"].pop(frame_id, None)
        generation = state["document_generations"].get(frame_id, 0)
        state["document_generations"][frame_id] = generation + 1
        document = (frame_id, generation)
        click_navigation = state["click_navigations"].pop(frame_id, None)
        if click_navigation is not None and click_navigation[0] == generation and not self._stopped:
            state["recovering_documents"].add(document)
            task = self._spawn(
                self._recover_click_for_document(session_key, document, click_navigation[1], click_navigation[2])
            )
            # A done-callback also runs for a task cancelled before its first step, so the hold always lifts.
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)
        else:
            self._forget_document(state, document)
        if not state["is_page_session"] or frame_id != state["main_frame_id"]:
            return
        state["main_frame_navigations"] += 1
        self._spawn(
            self._enqueue_navigation_event(
                session_key,
                state,
                frame_id,
                committed_url=str(
                    frame.get("unreachableUrl") or f"{frame.get('url') or ''}{frame.get('urlFragment') or ''}"
                ),
                navigation_seq=state["main_frame_navigations"],
                previous_url=previous_url,
                timestamp_ms=time.time() * 1000,
                suppressed=bool(self._control_navigation_depth),
            )
        )

    async def _enqueue_navigation_event(
        self,
        session_key: str,
        state: TapSessionState,
        frame_id: str,
        *,
        committed_url: str,
        navigation_seq: int,
        previous_url: str,
        timestamp_ms: float,
        suppressed: bool,
    ) -> None:
        session = state["session"]
        seen = state["seen_navigation_entry_ids"]
        entry: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            current_index, entries = await _navigation_history(session)
            entry = entries[current_index]
        if (
            entry is None
            or entry.get("url") != committed_url
            or _entry_id(entry) in seen
            or state["main_frame_navigations"] != navigation_seq
        ):
            # The first read can be stale or fail because the renderer reports frameNavigated before the browser
            # records the entry, or hold a later navigation's entry. Chromium holds renderer-bound commands like
            # getFrameTree until pending navigations finish.
            try:
                await asyncio.wait_for(session.send("Page.getFrameTree"), timeout=3)
                current_index, entries = await _navigation_history(session)
            except Exception:  # noqa: BLE001
                return
            entry = entries[current_index]
            # Without a later navigation the current entry is this one, even after a same-document URL change.
            # Otherwise take the oldest unseen entry with this URL, since a later navigation to it owns any newer one.
            if state["main_frame_navigations"] != navigation_seq:
                entry = next(
                    (
                        candidate
                        for candidate in entries
                        if _entry_id(candidate) not in seen and _is_navigation_entry(candidate, committed_url)
                    ),
                    None,
                )
        entry_id = _entry_id(entry) if entry is not None else ""
        if entry is None or not entry_id or entry_id in seen:
            LOG.debug(
                "copilot_demonstration_navigation_entry_unmatched",
                session_key=session_key,
                navigation_seq=navigation_seq,
                latest_navigation_seq=state["main_frame_navigations"],
                entry_id=entry_id or None,
            )
            return
        seen.add(entry_id)
        if suppressed:
            return
        transition_type = entry.get("transitionType")
        url = entry.get("url")
        if transition_type not in USER_NAVIGATION_TRANSITIONS or not isinstance(url, str) or not url:
            return
        self._enqueue_event(
            TapEvent(
                page=state["page"],
                session_key=session_key,
                frame_id=frame_id,
                is_main_frame=True,
                frame_name="",
                payload={
                    "kind": "navigate",
                    "ts": timestamp_ms,
                    "url": url,
                    "source_url": previous_url,
                    "transition_type": transition_type,
                },
            )
        )

    def begin_control_navigation(self) -> None:
        self._control_navigation_depth += 1

    def end_control_navigation(self) -> None:
        self._control_navigation_depth = max(0, self._control_navigation_depth - 1)

    @staticmethod
    def _frame_records(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}

        def visit(node: dict[str, Any]) -> None:
            frame = node["frame"]
            records[str(frame["id"])] = frame
            for child in node.get("childFrames", []):
                visit(child)

        visit(tree["frameTree"])
        return records

    def _reconcile_frames(self, session_key: str, tree: dict[str, Any]) -> None:
        state = self._sessions.get(session_key)
        if state is None:
            return
        records = self._frame_records(tree)
        state["frames"] = records
        state["main_frame_id"] = str(tree["frameTree"]["frame"]["id"])
        for frame_id in records:
            task_key = (session_key, frame_id)
            if task_key not in self._poll_tasks or self._poll_tasks[task_key].done():
                task = asyncio.create_task(self._poll_frame(session_key, frame_id))
                self._poll_tasks[task_key] = task
        for task_key, task in list(self._poll_tasks.items()):
            if task_key[0] == session_key and task_key[1] not in records:
                task.cancel()
                self._poll_tasks.pop(task_key, None)
        known_frames = (
            set(state["document_generations"])
            | set(state["click_navigations"])
            | {document[0] for document in state["pending_presses"]}
            | {document[0] for document in state["clicked_presses"]}
            | {document[0] for document in state["document_activity"]}
        )
        for frame_id in known_frames - records.keys():
            self._forget_frame(state, frame_id)

    async def _scan_once(self) -> None:
        for page in list(self.context.pages):
            await self._install_page(page)
        for key in list(self._sessions):
            state = self._sessions.get(key)
            if state is None:
                continue
            try:
                tree = await asyncio.wait_for(state["session"].send("Page.getFrameTree"), timeout=3)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                if _is_detached_session_error(exc):
                    await self._drop_session(key)
                continue
            self._reconcile_frames(key, tree)
        for page in list(self.context.pages):
            for frame in list(page.frames):
                if frame.parent_frame is not None:
                    await self._install_oopif(page, frame)

    async def poll_new_frames(self) -> None:
        while not self._stopped:
            await asyncio.sleep(0.5)
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("copilot_demonstration_frame_scan_failed")

    async def _poll_frame(self, session_key: str, frame_id: str) -> None:
        while not self._stopped:
            state = self._sessions.get(session_key)
            if state is None or frame_id not in state["frames"]:
                return
            if not await self._drain_frame(session_key, frame_id, max_wait_ms=1000):
                await asyncio.sleep(0.1)

    async def _drain_frame(
        self,
        session_key: str,
        frame_id: str,
        *,
        max_wait_ms: int,
        flush: bool = False,
        context_id: int | None = None,
    ) -> bool:
        state = self._sessions.get(session_key)
        if state is None or frame_id not in state["frames"]:
            return False
        acknowledged_stream, acknowledged_seq = state["drain_acks"].get(frame_id, (None, 0))
        drain = (
            f"globalThis.__skyvernDemoTap.drain({max_wait_ms}, {json.dumps(acknowledged_stream)}, {acknowledged_seq})"
        )
        expression = (
            f"globalThis.__skyvernDemoTap ? (globalThis.__skyvernDemoTap.flush(), {drain}) : null"
            if flush
            else f"globalThis.__skyvernDemoTap ? {drain} : null"
        )
        drain_key = (frame_id, state["document_generations"].get(frame_id, 0))
        state["inflight_drains"][drain_key] = state["inflight_drains"].get(drain_key, 0) + 1
        try:
            if context_id is None:
                # Chromium can hold renderer-bound commands while a navigation is pending, so every read is bounded.
                world = await asyncio.wait_for(
                    state["session"].send(
                        "Page.createIsolatedWorld",
                        {"frameId": frame_id, "worldName": WORLD, "grantUniveralAccess": False},
                    ),
                    timeout=CDP_READ_TIMEOUT_SECONDS,
                )
                context_id = world["executionContextId"]
                state["worlds"][frame_id] = context_id
                # The world belongs to the document the frame had when Chromium answered, which can be the next page
                # (its frameNavigated is delivered first): read and file the result under that page load.
                created_in = (frame_id, state["document_generations"].get(frame_id, 0))
                if created_in != drain_key:
                    self._end_inflight_drain(state, drain_key)
                    drain_key = created_in
                    state["inflight_drains"][drain_key] = state["inflight_drains"].get(drain_key, 0) + 1
            result = await asyncio.wait_for(
                state["session"].send(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "contextId": context_id,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                ),
                timeout=CDP_READ_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._end_inflight_drain(state, drain_key)
        if not self._stopped:
            self._enqueue_events(
                session_key, state, frame_id, result.get("result", {}).get("value"), generation=drain_key[1]
            )
        return True

    async def _recover_click_for_document(
        self, session_key: str, document: tuple[str, int], requested_url: str, requested_at: float
    ) -> None:
        state = self._sessions.get(session_key)
        if state is None:
            return
        frame_id = document[0]
        # Reads of the old document still in flight may carry the press, or the click itself.
        deadline = time.monotonic() + PRESS_RECOVERY_WAIT_SECONDS
        try:
            while state["inflight_drains"].get(document) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        finally:
            pending = state["pending_presses"].get(document)
            clicked = state["clicked_presses"].get(document, {})
            activity = state["document_activity"].get(document)
            self._forget_document(state, document)
        if self._stopped:
            return
        if activity is not None and any(
            _same_navigation_target(target, requested_url)
            and read_at >= requested_at - ACTIVATION_EXPLAINS_NAVIGATION_SECONDS
            for target, read_at in activity["click_targets"]
        ):
            return
        # Chromium reports script submits and scripted link clicks (SSO auto-posts, interstitials) with the same
        # reasons as a user's click, so only a press whose click was never read is evidence of a lost click.
        if pending is None or clicked.get(pending["stream"], 0) >= pending["payload"]["id"]:
            return
        if _same_navigation_target(str(pending["payload"].get("target_url") or ""), requested_url):
            payload = {key: value for key, value in pending["payload"].items() if key not in {"id", "target_url"}}
            payload.update(kind="click", press=pending["payload"]["id"])
            self._enqueue_recovered(state, session_key, frame_id, payload)
            return
        last_activation_at = activity["last_activation_at"] if activity is not None else None
        if (
            last_activation_at is not None
            and last_activation_at >= requested_at - ACTIVATION_EXPLAINS_NAVIGATION_SECONDS
        ):
            return
        # The press led somewhere its target did not name, and its click was never read: say so in order.
        last_ts = activity["last_ts"] if activity is not None else None
        if last_ts is None:
            last_ts = float(pending["payload"].get("ts") or 0)
        source_url = activity["url"] if activity is not None and activity["url"] else ""
        if not source_url:
            source_url = str(pending["payload"].get("url") or "")
        gap = {
            "kind": "click_navigation_unconfirmed",
            "ts": last_ts + 1,
            "url": source_url,
        }
        self._enqueue_recovered(state, session_key, frame_id, gap)

    def _enqueue_recovered(
        self, state: TapSessionState, session_key: str, frame_id: str, payload: dict[str, Any]
    ) -> None:
        self._enqueue_event(
            TapEvent(
                page=state["page"],
                session_key=session_key,
                frame_id=frame_id,
                is_main_frame=bool(state["is_page_session"] and frame_id == state["main_frame_id"]),
                frame_name=str(state["frames"].get(frame_id, {}).get("name") or ""),
                payload=payload,
            )
        )

    def _enqueue_events(
        self,
        session_key: str,
        state: TapSessionState,
        frame_id: str,
        result: Any,
        *,
        generation: int | None = None,
    ) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("stream"), str):
            return
        stream = result["stream"]
        payloads = result.get("events")
        if not isinstance(payloads, list):
            return
        seen_sequences = state["seen_event_sequences"].setdefault(frame_id, {})
        last_seen = seen_sequences.get(stream, 0)
        record = state["frames"].get(frame_id, {})
        current = state["document_generations"].get(frame_id, 0)
        document = (frame_id, current if generation is None else generation)
        # A read of an unloaded document still counts while that document's click may need recovering.
        tracked = document[1] == current or document in state["recovering_documents"]
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("seq"), int) and payload["seq"] > last_seen:
                queued = self._enqueue_event(
                    TapEvent(
                        page=state["page"],
                        session_key=session_key,
                        frame_id=frame_id,
                        is_main_frame=bool(state["is_page_session"] and frame_id == state["main_frame_id"]),
                        frame_name=str(record.get("name") or ""),
                        payload=payload,
                    )
                )
                if not queued:
                    break
                last_seen = payload["seq"]
                if isinstance(payload.get("press"), int) and tracked:
                    clicked = state["clicked_presses"].setdefault(document, {})
                    clicked[stream] = max(clicked.get(stream, 0), payload["press"])
                if tracked:
                    self._note_activity(state, document, payload)
        if tracked:
            self._track_pending_press(state, document, stream, result.get("press"))
        seen_sequences[stream] = last_seen
        state["drain_acks"][frame_id] = (stream, last_seen)
        while len(seen_sequences) > 2:
            seen_sequences.pop(next(iter(seen_sequences)))

    @staticmethod
    def _end_inflight_drain(state: TapSessionState, drain_key: tuple[str, int]) -> None:
        remaining = state["inflight_drains"].get(drain_key, 1) - 1
        if remaining > 0:
            state["inflight_drains"][drain_key] = remaining
        else:
            state["inflight_drains"].pop(drain_key, None)

    @staticmethod
    def _track_pending_press(state: TapSessionState, document: tuple[str, int], stream: str, press: Any) -> None:
        pending = state["pending_presses"].get(document)
        if not isinstance(press, dict) or not isinstance(press.get("id"), int):
            if pending is not None and pending["stream"] == stream:
                state["pending_presses"].pop(document, None)
            return
        if state["clicked_presses"].get(document, {}).get(stream, 0) >= press["id"]:
            return
        if pending is not None and pending["stream"] == stream and pending["payload"]["id"] > press["id"]:
            return
        state["pending_presses"][document] = {"stream": stream, "payload": press}

    @staticmethod
    def _note_activity(state: TapSessionState, document: tuple[str, int], payload: dict[str, Any]) -> None:
        activity = state["document_activity"].setdefault(
            document, {"click_targets": [], "last_activation_at": None, "last_ts": None, "url": ""}
        )
        kind = payload.get("kind")
        if kind in {"click", "key"}:
            activity["last_activation_at"] = time.monotonic()
        if kind == "click" and isinstance(payload.get("target_url"), str) and payload["target_url"]:
            activity["click_targets"] = [*activity["click_targets"][-9:], (payload["target_url"], time.monotonic())]
        if isinstance(payload.get("ts"), (int, float)):
            activity["last_ts"] = max(activity["last_ts"] or 0, float(payload["ts"]))
        if isinstance(payload.get("url"), str):
            activity["url"] = payload["url"]

    @staticmethod
    def _forget_document(state: TapSessionState, document: tuple[str, int]) -> None:
        state["pending_presses"].pop(document, None)
        state["document_activity"].pop(document, None)
        state["clicked_presses"].pop(document, None)
        state["recovering_documents"].discard(document)

    @staticmethod
    def _forget_frame(state: TapSessionState, frame_id: str) -> None:
        for document in [key for key in state["pending_presses"] if key[0] == frame_id]:
            state["pending_presses"].pop(document, None)
        for document in [key for key in state["clicked_presses"] if key[0] == frame_id]:
            state["clicked_presses"].pop(document, None)
        for document in [key for key in state["document_activity"] if key[0] == frame_id]:
            state["document_activity"].pop(document, None)
        state["recovering_documents"] -= {key for key in state["recovering_documents"] if key[0] == frame_id}
        state["click_navigations"].pop(frame_id, None)
        state["document_generations"].pop(frame_id, None)

    async def _drop_session(self, session_key: str) -> None:
        state = self._sessions.pop(session_key, None)
        if state is None:
            return
        for task_key, task in list(self._poll_tasks.items()):
            if task_key[0] == session_key:
                task.cancel()
                self._poll_tasks.pop(task_key, None)
        for page_key, key in list(self._page_sessions.items()):
            if key == session_key:
                self._page_sessions.pop(page_key, None)
        for frame_key, key in list(self._oopif_sessions.items()):
            if key == session_key:
                self._oopif_sessions.pop(frame_key, None)
        with contextlib.suppress(Exception):
            await state["session"].detach()

    def drain_events(self) -> list[TapEvent]:
        # Hold events back while a click is being recovered, so it is not stored after the next page's actions.
        if self._recovery_tasks:
            return []
        events = [self._events.get_nowait() for _ in range(self._events.qsize())]
        return sorted(events, key=lambda event: event.payload.get("ts", 0))

    async def flush(self) -> None:
        for session_key, state in list(self._sessions.items()):
            for frame_id in list(state["frames"]):
                with contextlib.suppress(Exception):
                    await self._drain_frame(session_key, frame_id, max_wait_ms=0, flush=True)
        await asyncio.sleep(0)

    async def set_intercept_file_chooser_dialog(self, enabled: bool) -> None:
        self._intercept_file_chooser = enabled
        await asyncio.gather(
            *(
                state["session"].send("Page.setInterceptFileChooserDialog", {"enabled": enabled})
                for state in self._sessions.values()
            )
        )

    async def frame_owner_selector(self, event: TapEvent) -> str | None:
        if event.is_main_frame:
            return None
        event_state = self._sessions.get(event.session_key)
        if event_state is None:
            return None
        frame_record = event_state["frames"].get(event.frame_id, {})
        parent_id = str(frame_record.get("parentId") or "")
        page_key = self._page_sessions.get(id(event.page))
        page_state = self._sessions.get(page_key or "")
        if page_state is None or parent_id != page_state["main_frame_id"]:
            return None
        return await self._frame_owner_selector_on_session(page_state, event.frame_id, parent_id)

    async def frame_path(self, event: TapEvent) -> list[dict[str, str]] | None:
        if event.is_main_frame:
            return []
        event_state = self._sessions.get(event.session_key)
        page_key = self._page_sessions.get(id(event.page))
        page_state = self._sessions.get(page_key or "")
        if event_state is None or page_state is None:
            return None
        frame_id = event.frame_id
        reverse_path: list[dict[str, str]] = []
        while frame_id != page_state["main_frame_id"]:
            state = event_state if frame_id in event_state["frames"] else page_state
            frame_record = state["frames"].get(frame_id)
            if frame_record is None:
                return None
            parent_id = str(frame_record.get("parentId") or "")
            if not parent_id:
                return None
            owner_state = page_state if parent_id == page_state["main_frame_id"] else state
            owner = await self._frame_owner_metadata_on_session(owner_state, frame_id, parent_id)
            if owner is None:
                return None
            name = owner["name"]
            if name:
                reverse_path.append({"name": name})
            else:
                reverse_path.append({"owner_selector": owner["selector"]})
            frame_id = parent_id
        return list(reversed(reverse_path))

    async def _frame_owner_selector_on_session(
        self, state: TapSessionState, frame_id: str, parent_id: str
    ) -> str | None:
        metadata = await self._frame_owner_metadata_on_session(state, frame_id, parent_id)
        return metadata["selector"] if metadata is not None else None

    async def _frame_owner_metadata_on_session(
        self, state: TapSessionState, frame_id: str, parent_id: str
    ) -> dict[str, str] | None:
        session = state["session"]
        try:
            try:
                owner = await session.send("DOM.getFrameOwner", {"frameId": frame_id})
            except Exception:  # noqa: BLE001
                await session.send("DOM.enable")
                owner = await session.send("DOM.getFrameOwner", {"frameId": frame_id})
            world = await session.send(
                "Page.createIsolatedWorld",
                {"frameId": parent_id, "worldName": WORLD, "grantUniveralAccess": False},
            )
            resolved = await session.send(
                "DOM.resolveNode",
                {
                    "backendNodeId": owner["backendNodeId"],
                    "executionContextId": world["executionContextId"],
                },
            )
            result = await session.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": (
                        "function() { if (!globalThis.__skyvernDemoTap) return null; "
                        "return {selector: globalThis.__skyvernDemoTap.selectorFor(this), "
                        "name: this.getAttribute('name') || ''}; }"
                    ),
                    "objectId": resolved["object"]["objectId"],
                    "returnByValue": True,
                },
            )
        except Exception:  # noqa: BLE001
            return None
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict) or not isinstance(value.get("selector"), str):
            return None
        return {"selector": value["selector"], "name": str(value.get("name") or "")}

    async def stop(self) -> None:
        if self._stopped:
            return
        await self.flush()
        # A click that unloaded its page just before Stop is still being recovered; let it finish first.
        if self._recovery_tasks:
            await asyncio.wait(set(self._recovery_tasks), timeout=PRESS_RECOVERY_WAIT_SECONDS + 1)
        self._stopped = True
        if self._page_listener is not None:
            with contextlib.suppress(Exception):
                self.context.remove_listener("page", self._page_listener)
        sessions = list(self._sessions.items())

        async def stop_session(session_key: str, state: TapSessionState) -> None:
            session = state["session"]
            for frame_id in list(state["frames"]):
                with contextlib.suppress(Exception):
                    world = await asyncio.wait_for(
                        session.send(
                            "Page.createIsolatedWorld",
                            {"frameId": frame_id, "worldName": WORLD, "grantUniveralAccess": False},
                        ),
                        timeout=CDP_READ_TIMEOUT_SECONDS,
                    )
                    result = await asyncio.wait_for(
                        session.send(
                            "Runtime.evaluate",
                            {
                                "expression": ("globalThis.__skyvernDemoTap && globalThis.__skyvernDemoTap.stop()"),
                                "contextId": world["executionContextId"],
                                "returnByValue": True,
                            },
                        ),
                        timeout=CDP_READ_TIMEOUT_SECONDS,
                    )
                    self._enqueue_events(session_key, state, frame_id, result.get("result", {}).get("value"))
            if state["script_id"]:
                with contextlib.suppress(Exception):
                    await session.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": state["script_id"]})

        await asyncio.gather(*(stop_session(key, state) for key, state in sessions))
        tasks = list(self._poll_tasks.values()) + list(self._background_tasks)
        if self._scan_task is not None:
            tasks.append(self._scan_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for _, state in sessions:
            with contextlib.suppress(Exception):
                await state["session"].detach()
        self._sessions.clear()
        self._poll_tasks.clear()
        self._page_sessions.clear()
        self._oopif_sessions.clear()
        self._refused_frames.clear()


def _same_navigation_target(target_url: str, requested_url: str) -> bool:
    # A form submission adds its fields as a query, so compare without query and fragment.
    if not target_url or not requested_url:
        return False
    target = urlsplit(target_url)
    requested = urlsplit(requested_url)
    return (target.scheme, target.netloc, target.path) == (requested.scheme, requested.netloc, requested.path)


def _entry_id(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or "")


def _is_navigation_entry(entry: dict[str, Any], committed_url: str) -> bool:
    # userTypedURL survives a page rewriting its own URL with replaceState; pushState entries inherit it, so only
    # user navigations match on it.
    return entry.get("url") == committed_url or (
        entry.get("userTypedURL") == committed_url and entry.get("transitionType") in USER_NAVIGATION_TRANSITIONS
    )


async def _navigation_history(session: CDPSession) -> tuple[int, list[dict[str, Any]]]:
    history = await session.send("Page.getNavigationHistory")
    current_index = history.get("currentIndex")
    entries = history.get("entries")
    if not isinstance(current_index, int) or not isinstance(entries, list) or not 0 <= current_index < len(entries):
        raise ValueError("navigation history has no current entry")
    return current_index, entries


def _is_detached_session_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "target closed",
            "session closed",
            "session is detached",
            "connection closed",
            "browser has been closed",
        )
    )
