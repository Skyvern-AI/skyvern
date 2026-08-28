from __future__ import annotations

import asyncio
import re
import time
import weakref
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from skyvern.services.browser_recording.v2.ledger import Gesture, GestureLedger

LOG = structlog.get_logger()

_SIMPLE_CSS_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_FRAME_TAGS = frozenset({"IFRAME", "FRAME"})
_ROOT_NODE_TYPES = frozenset({9, 11})
_MAX_ANCESTORS = 8
_MAX_SHADOW_DEPTH = 4
_MAX_STRUCTURAL_SEGMENTS = 4


class Resolver:
    def __init__(self, ledger: GestureLedger) -> None:
        self.ledger = ledger
        self._tasks: set[asyncio.Task[None]] = set()
        self._dom_enabled: weakref.WeakKeyDictionary[Any, None] = weakref.WeakKeyDictionary()
        self._target_ids: weakref.WeakKeyDictionary[Any, str | None] = weakref.WeakKeyDictionary()
        self._child_sessions: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
        self._cache_lock = asyncio.Lock()

    def on_gesture(self, gesture: Gesture, cdp_session: Any, page: Any) -> None:
        gesture.is_secret = True
        coroutine = self._resolve(gesture, cdp_session, page)
        try:
            task = asyncio.create_task(coroutine)
        except (RuntimeError, TypeError) as error:
            coroutine.close()
            LOG.debug("Target resolution was not scheduled", error_type=type(error).__name__)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def forget_session(self, cdp_session: Any) -> None:
        self._dom_enabled.pop(cdp_session, None)
        self._target_ids.pop(cdp_session, None)

    async def _resolve(self, gesture: Gesture, cdp_session: Any, page: Any) -> None:
        started = time.monotonic()
        tag: str | None = None
        role: str | None = None
        try:
            await asyncio.sleep(0)
            x, y = gesture.x, gesture.y
            if x is None or y is None:
                return

            target_id = await self._target_id(cdp_session)
            session = cdp_session
            hit, node = await self._resolve_node(session, x, y)
            tag = _tag(node)
            if tag in _FRAME_TAGS and not node.get("contentDocument"):
                child_session = await self._session_for_frame(session, page, _frame_id(hit, node))
                if child_session is None:
                    return
                x, y = await self._translate_coordinates(session, node, x, y)
                session = child_session
                hit, node = await self._resolve_node(session, x, y)
                tag = _tag(node)

            backend_node_id = _integer(hit.get("backendNodeId")) or _integer(node.get("backendNodeId"))
            if backend_node_id is None:
                return
            attributes = _attributes(node)
            frame_id = _frame_id(hit, node)
            role, accessible_name, ax_properties = await self._ax_metadata(session, backend_node_id)
            selector, shadow_path = await self._locator(session, node, role, accessible_name, frame_id)
            input_type = attributes.get("type")

            gesture.target_id = target_id
            gesture.frame_id = frame_id
            gesture.backend_node_id = backend_node_id
            gesture.selector = selector
            gesture.role = role
            gesture.accessible_name = accessible_name
            gesture.tag = tag
            gesture.input_type = input_type
            gesture.shadow_path = shadow_path
            gesture.is_secret = _is_secret(tag, input_type, role, ax_properties)
            LOG.debug(
                "Target resolved",
                tag=tag,
                role=role,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            LOG.debug(
                "Target resolution failed",
                tag=tag,
                role=role,
                duration_ms=(time.monotonic() - started) * 1000,
                error_type=type(error).__name__,
            )

    async def _resolve_node(self, session: Any, x: float, y: float) -> tuple[dict, dict]:
        await self._enable_dom(session)
        hit = await session.send(
            "DOM.getNodeForLocation",
            {
                "x": x,
                "y": y,
                "includeUserAgentShadowDOM": False,
                "ignorePointerEventsNone": False,
            },
        )
        backend_node_id = hit.get("backendNodeId")
        described = await session.send("DOM.describeNode", {"backendNodeId": backend_node_id, "depth": 0})
        return hit, described.get("node") or {}

    async def _enable_dom(self, session: Any) -> None:
        async with self._cache_lock:
            if session not in self._dom_enabled:
                await session.send("DOM.enable", {})
                self._dom_enabled[session] = None

    async def _document_root(self, session: Any) -> int | None:
        """Never cached: a same-session navigation invalidates every nodeId, and this one is
        handed straight to DOM.querySelectorAll for the uniqueness check."""
        await self._enable_dom(session)
        response = await session.send("DOM.getDocument", {"depth": 0})
        return _integer((response.get("root") or {}).get("nodeId"))

    async def _target_id(self, session: Any) -> str | None:
        async with self._cache_lock:
            if session not in self._target_ids:
                response = await session.send("Target.getTargetInfo", {})
                target_id = (response.get("targetInfo") or {}).get("targetId")
                self._target_ids[session] = target_id if isinstance(target_id, str) else None
            return self._target_ids[session]

    async def _session_for_frame(self, session: Any, page: Any, frame_id: str | None) -> Any | None:
        if frame_id is None:
            return None
        tree = await session.send("Page.getFrameTree", {})
        descriptor = _find_frame(tree.get("frameTree") or {}, frame_id)
        if descriptor is None:
            return None
        url, name = descriptor.get("url"), descriptor.get("name")
        candidates = [frame for frame in page.frames if frame.url == url and (not name or frame.name == name)]
        if not candidates:
            return None
        if len(candidates) == 1:
            return await self._child_session(page, candidates[0])
        # Same url and no distinguishing name: ask each out-of-process frame's own session which
        # frame it is, rather than picking the first match and silently resolving in the wrong one.
        for frame in candidates:
            child_session = await self._child_session(page, frame)
            tree = await child_session.send("Page.getFrameTree", {})
            if ((tree.get("frameTree") or {}).get("frame") or {}).get("id") == frame_id:
                return child_session
        return None

    async def _child_session(self, page: Any, frame: Any) -> Any:
        async with self._cache_lock:
            if frame not in self._child_sessions:
                self._child_sessions[frame] = await page.context.new_cdp_session(frame)
            return self._child_sessions[frame]

    async def _translate_coordinates(self, session: Any, node: dict, x: int, y: int) -> tuple[int, int]:
        backend_node_id = node.get("backendNodeId")
        response = await session.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})
        quad = (response.get("model") or {}).get("content") or []
        if len(quad) < 8:
            return x, y
        return int(x - min(quad[0::2])), int(y - min(quad[1::2]))

    async def _ax_metadata(self, session: Any, backend_node_id: int) -> tuple[str | None, str | None, dict]:
        response = await session.send(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": backend_node_id, "fetchRelatives": False},
        )
        nodes = response.get("nodes") or []
        node = nodes[0] if nodes else {}
        role = _ax_value(node.get("role"))
        name = _ax_value(node.get("name"))
        properties = {
            prop.get("name"): _ax_value(prop.get("value"))
            for prop in node.get("properties") or []
            if isinstance(prop, dict) and isinstance(prop.get("name"), str)
        }
        return role, name, properties

    async def _locator(
        self,
        session: Any,
        node: dict,
        role: str | None,
        accessible_name: str | None,
        frame_id: str | None,
    ) -> tuple[str, list[str] | None]:
        shadow_path: list[str] = []
        current = node
        selector = ""
        for _ in range(_MAX_SHADOW_DEPTH):
            root, structural = await self._within_root(session, current)
            selector = await self._unique_selector(session, current, structural, root, role, accessible_name, frame_id)
            if _integer(root.get("nodeType")) != 11:
                return selector, shadow_path or None
            host_id = _integer(root.get("parentId"))
            if host_id is None:
                break
            shadow_path.insert(0, selector)
            role, accessible_name = None, None
            response = await session.send("DOM.describeNode", {"nodeId": host_id, "depth": 0})
            current = response.get("node") or {}
        return selector, shadow_path or None

    async def _within_root(self, session: Any, node: dict) -> tuple[dict, list[str]]:
        segments: list[str] = []
        current = node
        for _ in range(_MAX_ANCESTORS):
            parent_id = _integer(current.get("parentId"))
            if parent_id is None:
                break
            response = await session.send("DOM.describeNode", {"nodeId": parent_id, "depth": 1})
            parent = response.get("node") or {}
            segments.insert(0, _structural_segment(current, parent))
            if _integer(parent.get("nodeType")) in _ROOT_NODE_TYPES:
                return parent, segments
            current = parent
        return {}, segments

    async def _unique_selector(
        self,
        session: Any,
        node: dict,
        structural: list[str],
        root: dict,
        role: str | None,
        accessible_name: str | None,
        frame_id: str | None,
    ) -> str:
        root_node_id = _integer(root.get("nodeId"))
        if root_node_id is None:
            root_node_id = await self._document_root(session)
        for candidate in _attribute_candidates(_tag(node), _attributes(node)):
            if await self._match_count(session, root_node_id, candidate) == 1:
                return candidate
        if role and accessible_name and await self._ax_match_count(session, frame_id, role, accessible_name) == 1:
            return f'role={role}[name="{_quoted(accessible_name)}"]'
        return " > ".join(structural[-_MAX_STRUCTURAL_SEGMENTS:]) or (_tag(node) or "*").lower()

    async def _match_count(self, session: Any, root_node_id: int | None, selector: str) -> int:
        if root_node_id is None:
            return 0
        try:
            response = await session.send("DOM.querySelectorAll", {"nodeId": root_node_id, "selector": selector})
        except Exception:  # noqa: BLE001
            return 0
        return len(response.get("nodeIds") or [])

    async def _ax_match_count(self, session: Any, frame_id: str | None, role: str, name: str) -> int:
        try:
            response = await session.send("Accessibility.getFullAXTree", {"frameId": frame_id} if frame_id else {})
        except Exception:  # noqa: BLE001
            return 0
        return sum(
            1
            for ax_node in response.get("nodes") or []
            if not ax_node.get("ignored")
            and _ax_value(ax_node.get("role")) == role
            and _ax_value(ax_node.get("name")) == name
            and ax_node.get("backendDOMNodeId") is not None
        )

    def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _tag(node: dict) -> str | None:
    value = node.get("localName") or node.get("nodeName")
    return value.upper() if isinstance(value, str) else None


def _attributes(node: dict) -> dict[str, str]:
    values = node.get("attributes") or []
    return {
        values[index]: values[index + 1]
        for index in range(0, len(values) - 1, 2)
        if isinstance(values[index], str) and isinstance(values[index + 1], str)
    }


def _ax_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else None


def _frame_id(hit: dict, node: dict) -> str | None:
    value = node.get("frameId") or hit.get("frameId")
    return value if isinstance(value, str) else None


def _find_frame(tree: dict, frame_id: str) -> dict | None:
    frame = tree.get("frame") or {}
    if frame.get("id") == frame_id:
        return frame
    for child in tree.get("childFrames") or []:
        found = _find_frame(child, frame_id)
        if found is not None:
            return found
    return None


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _attribute_candidates(tag: str | None, attributes: dict[str, str]) -> list[str]:
    html_tag = (tag or "*").lower()
    candidates: list[str] = []
    element_id = attributes.get("id")
    if element_id and not _is_synthetic_id(element_id):
        candidates.append(f"#{element_id}" if _SIMPLE_CSS_ID.match(element_id) else f'[id="{_quoted(element_id)}"]')
    if name := attributes.get("name"):
        candidates.append(f'{html_tag}[name="{_quoted(name)}"]')
    if aria_label := attributes.get("aria-label"):
        candidates.append(f'{html_tag}[aria-label="{_quoted(aria_label)}"]')
    if test_id := attributes.get("data-testid"):
        candidates.append(f'[data-testid="{_quoted(test_id)}"]')
    return candidates


def _is_synthetic_id(value: str) -> bool:
    if ":" in value or re.search(r"\d{3,}", value):
        return True
    if re.match(r"^[a-zA-Z]{1,2}[-_]?\d+$", value):
        return True
    return any(
        len(token) >= 6 and re.search(r"\d", token) and re.search(r"[a-zA-Z]", token)
        for token in re.split(r"[-_:.]", value)
    )


def _structural_segment(node: dict, parent: dict) -> str:
    node_name = node.get("nodeName")
    siblings = [
        child
        for child in parent.get("children") or []
        if _integer(child.get("nodeType")) == 1 and child.get("nodeName") == node_name
    ]
    position = next(
        (index for index, child in enumerate(siblings, 1) if child.get("backendNodeId") == node.get("backendNodeId")),
        1,
    )
    return f"{(_tag(node) or '*').lower()}:nth-of-type({position})"


def _is_secret(
    tag: str | None,
    input_type: str | None,
    role: str | None,
    ax_properties: dict,
) -> bool:
    if tag == "INPUT" and (input_type or "").lower() == "password":
        return True
    return role == "textbox" and any(bool(ax_properties.get(name)) for name in ("secure", "password"))


_resolvers: dict[str, Resolver] = {}


def start_resolver(browser_session_id: str, ledger: GestureLedger) -> Resolver:
    return _resolvers.get(browser_session_id) or _resolvers.setdefault(browser_session_id, Resolver(ledger))


def get_resolver(browser_session_id: str) -> Resolver | None:
    return _resolvers.get(browser_session_id)


def stop_resolver(browser_session_id: str) -> Resolver | None:
    resolver = _resolvers.pop(browser_session_id, None)
    if resolver is not None:
        resolver.close()
    return resolver
