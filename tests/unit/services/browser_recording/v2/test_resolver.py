import asyncio
from collections import defaultdict, deque
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.services.browser_recording.v2.keyfold import fold
from skyvern.services.browser_recording.v2.ledger import Gesture, GestureLedger
from skyvern.services.browser_recording.v2.resolver import Resolver

_UNIQUE_MATCH = {"nodeIds": [1]}


class _ScriptedSession:
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        self.responses = {method: deque(values) for method, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        queue = self.responses.get(method)
        if not queue:
            if method == "DOM.enable":
                return {}
            if method == "DOM.querySelectorAll":
                return _UNIQUE_MATCH
            raise AssertionError(f"unscripted CDP call: {method}")
        response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _gesture(**fields: Any) -> Gesture:
    return Gesture(
        seq=1,
        t_received=1.0,
        kind="mouse_pressed",
        page_key="page-1",
        url="https://example.test/form",
        x=fields.pop("x", 12),
        y=fields.pop("y", 34),
        **fields,
    )


def _input_responses(
    *, input_type: str = "text", node_id: int = 8, frame_id: str = "main", element_id: str = "email"
) -> dict[str, list]:
    return {
        "DOM.getDocument": [{"root": {"nodeId": 1}}],
        "Target.getTargetInfo": [{"targetInfo": {"targetId": "target-1"}}],
        "DOM.getNodeForLocation": [{"backendNodeId": 7, "frameId": frame_id}],
        "DOM.describeNode": [
            {
                "node": {
                    "nodeId": node_id,
                    "backendNodeId": 7,
                    "nodeName": "INPUT",
                    "frameId": frame_id,
                    "attributes": [
                        "id",
                        element_id,
                        "name",
                        "email",
                        "type",
                        input_type,
                        "aria-label",
                        "Email address",
                    ],
                }
            }
        ],
        "Accessibility.getPartialAXTree": [
            {
                "nodes": [
                    {
                        "role": {"value": "textbox"},
                        "name": {"value": "Email address"},
                        "properties": [],
                    }
                ]
            }
        ],
    }


class _Frame:
    """Only the attributes a Playwright ``Frame`` really exposes; notably no frame id."""

    def __init__(self, url: str, name: str, parent_frame: "_Frame | None" = None) -> None:
        self.url = url
        self.name = name
        self.parent_frame = parent_frame


def _page(frames: list[Any] | None = None, new_cdp_session: Any = None) -> SimpleNamespace:
    context = SimpleNamespace(new_cdp_session=new_cdp_session) if new_cdp_session else SimpleNamespace()
    return SimpleNamespace(frames=frames or [], context=context)


async def _drain(resolver: Resolver) -> None:
    await asyncio.gather(*tuple(resolver._tasks))


@pytest.mark.asyncio
async def test_resolves_input_on_existing_session_and_prefers_stable_id() -> None:
    session = _ScriptedSession(_input_responses())
    gesture = _gesture()
    resolver = Resolver(GestureLedger("pbs-resolve"))

    resolver.on_gesture(gesture, session, _page())
    await _drain(resolver)

    assert gesture.backend_node_id == 7
    assert gesture.frame_id == "main"
    assert gesture.target_id == "target-1"
    assert gesture.selector == "#email"
    assert gesture.role == "textbox"
    assert gesture.accessible_name == "Email address"
    assert gesture.tag == "INPUT"
    assert gesture.input_type == "text"
    assert gesture.is_secret is False
    methods = [method for method, _ in session.calls]
    assert methods.index("DOM.enable") < methods.index("DOM.getNodeForLocation")
    assert (
        "DOM.getNodeForLocation",
        {
            "x": 12,
            "y": 34,
            "includeUserAgentShadowDOM": False,
            "ignorePointerEventsNone": False,
        },
    ) in session.calls
    assert ("DOM.querySelectorAll", {"nodeId": 1, "selector": "#email"}) in session.calls
    assert "Accessibility.enable" not in methods


@pytest.mark.asyncio
async def test_password_resolution_redacts_the_folded_run() -> None:
    session = _ScriptedSession(_input_responses(input_type="password"))
    press = _gesture()
    resolver = Resolver(GestureLedger("pbs-secret"))

    resolver.on_gesture(press, session, _page())
    await _drain(resolver)
    facts = fold(
        [
            press,
            Gesture(
                seq=2,
                t_received=2.0,
                kind="key",
                page_key=press.page_key,
                url=press.url,
                key="s",
                text="s",
                key_event_type="keyDown",
            ),
            Gesture(
                seq=3,
                t_received=3.0,
                kind="key",
                page_key=press.page_key,
                url=press.url,
                key="e",
                text="e",
                key_event_type="keyDown",
            ),
        ]
    )

    assert press.is_secret is True
    assert facts[1].kind == "type_text"
    assert facts[1].typed_value is None
    assert facts[1].typed_length == 2
    assert facts[1].selector == "#email"


@pytest.mark.asyncio
async def test_synthetic_id_falls_back_to_name() -> None:
    session = _ScriptedSession(_input_responses(element_id="field-123"))
    gesture = _gesture()
    resolver = Resolver(GestureLedger("pbs-synthetic-id"))

    resolver.on_gesture(gesture, session, _page())
    await _drain(resolver)

    assert gesture.selector == 'input[name="email"]'


@pytest.mark.asyncio
async def test_ambiguous_candidate_falls_through_to_a_unique_one() -> None:
    responses = _input_responses()
    responses["DOM.describeNode"] = [
        {
            "node": {
                "nodeId": 8,
                "backendNodeId": 7,
                "nodeName": "INPUT",
                "frameId": "main",
                "attributes": ["aria-label", "Email address", "data-testid", "email-field", "type", "text"],
            }
        }
    ]
    responses["DOM.querySelectorAll"] = [{"nodeIds": [4, 5]}, {"nodeIds": [4]}]
    session = _ScriptedSession(responses)
    gesture = _gesture()
    resolver = Resolver(GestureLedger("pbs-ambiguous"))

    resolver.on_gesture(gesture, session, _page())
    await _drain(resolver)

    assert [params["selector"] for method, params in session.calls if method == "DOM.querySelectorAll"] == [
        'input[aria-label="Email address"]',
        '[data-testid="email-field"]',
    ]
    assert gesture.selector == '[data-testid="email-field"]'


@pytest.mark.asyncio
async def test_structural_fallback_carries_nth_of_type() -> None:
    responses = _input_responses()
    responses["DOM.describeNode"] = [
        {"node": {"nodeId": 8, "parentId": 9, "backendNodeId": 7, "nodeName": "INPUT", "frameId": "main"}},
        {
            "node": {
                "nodeId": 9,
                "parentId": 11,
                "backendNodeId": 4,
                "nodeType": 1,
                "nodeName": "DIV",
                "children": [
                    {"nodeType": 1, "nodeName": "INPUT", "backendNodeId": 5},
                    {"nodeType": 1, "nodeName": "INPUT", "backendNodeId": 6},
                    {"nodeType": 1, "nodeName": "INPUT", "backendNodeId": 7},
                ],
            }
        },
        {
            "node": {
                "nodeId": 11,
                "backendNodeId": 2,
                "nodeType": 9,
                "nodeName": "#document",
                "children": [
                    {"nodeType": 1, "nodeName": "DIV", "backendNodeId": 3},
                    {"nodeType": 1, "nodeName": "DIV", "backendNodeId": 4},
                ],
            }
        },
    ]
    responses["Accessibility.getFullAXTree"] = [
        {
            "nodes": [
                {"role": {"value": "textbox"}, "name": {"value": "Email address"}, "backendDOMNodeId": 6},
                {"role": {"value": "textbox"}, "name": {"value": "Email address"}, "backendDOMNodeId": 7},
            ]
        }
    ]
    session = _ScriptedSession(responses)
    gesture = _gesture()
    resolver = Resolver(GestureLedger("pbs-structural"))

    resolver.on_gesture(gesture, session, _page())
    await _drain(resolver)

    assert gesture.selector == "div:nth-of-type(2) > input:nth-of-type(3)"


@pytest.mark.asyncio
async def test_same_process_iframe_stays_on_parent_session() -> None:
    session = _ScriptedSession(_input_responses(frame_id="child-frame"))
    opened: list[Any] = []

    async def new_cdp_session(frame: Any) -> Any:
        opened.append(frame)
        raise AssertionError("same-process iframe must stay on the parent session")

    gesture = _gesture(x=110, y=220)
    resolver = Resolver(GestureLedger("pbs-same-process"))

    resolver.on_gesture(gesture, session, _page(new_cdp_session=new_cdp_session))
    await _drain(resolver)

    assert gesture.backend_node_id == 7
    assert gesture.frame_id == "child-frame"
    assert gesture.tag == "INPUT"
    assert opened == []
    assert [method for method, _ in session.calls].count("DOM.getNodeForLocation") == 1


@pytest.mark.asyncio
async def test_oopif_maps_frame_tree_to_one_cached_child_session_and_translates_coordinates() -> None:
    iframe_node = {
        "node": {
            "nodeId": 21,
            "backendNodeId": 20,
            "nodeName": "IFRAME",
            "frameId": "child-frame",
        }
    }
    box_model = {"model": {"content": [100, 200, 300, 200, 300, 400, 100, 400]}}
    frame_tree = {
        "frameTree": {
            "frame": {"id": "main", "url": "https://example.test/form"},
            "childFrames": [{"frame": {"id": "child-frame", "url": "https://pay.test/widget", "name": "pay"}}],
        }
    }
    parent = _ScriptedSession(
        {
            "DOM.getDocument": [{"root": {"nodeId": 1}}],
            "Target.getTargetInfo": [{"targetInfo": {"targetId": "target-1"}}],
            "DOM.getNodeForLocation": [
                {"backendNodeId": 20, "frameId": "child-frame"},
                {"backendNodeId": 20, "frameId": "child-frame"},
            ],
            "DOM.describeNode": [iframe_node, iframe_node],
            "Page.getFrameTree": [frame_tree, frame_tree],
            "DOM.getBoxModel": [box_model, box_model],
        }
    )
    child_responses = _input_responses(frame_id="child-frame")
    child_responses["Target.getTargetInfo"] = []
    child_responses["DOM.getNodeForLocation"] = [
        {"backendNodeId": 7, "frameId": "child-frame"},
        {"backendNodeId": 7, "frameId": "child-frame"},
    ]
    child_responses["DOM.describeNode"] *= 2
    child_responses["DOM.getDocument"] *= 2
    child_responses["Accessibility.getPartialAXTree"] *= 2
    child = _ScriptedSession(child_responses)
    main_frame = _Frame("https://example.test/form", "")
    child_frame = _Frame("https://pay.test/widget", "pay", main_frame)
    opened: defaultdict[int, int] = defaultdict(int)

    async def new_cdp_session(target_frame: Any) -> _ScriptedSession:
        opened[id(target_frame)] += 1
        return child

    page = _page([main_frame, child_frame], new_cdp_session)
    resolver = Resolver(GestureLedger("pbs-oopif"))
    gestures = [_gesture(x=115, y=225), _gesture(x=120, y=230)]

    for gesture in gestures:
        resolver.on_gesture(gesture, parent, page)
        await _drain(resolver)

    assert [gesture.frame_id for gesture in gestures] == ["child-frame", "child-frame"]
    assert [gesture.selector for gesture in gestures] == ["#email", "#email"]
    assert opened == {id(child_frame): 1}
    child_hits = [params for method, params in child.calls if method == "DOM.getNodeForLocation"]
    assert child_hits == [
        {
            "x": 15,
            "y": 25,
            "includeUserAgentShadowDOM": False,
            "ignorePointerEventsNone": False,
        },
        {
            "x": 20,
            "y": 30,
            "includeUserAgentShadowDOM": False,
            "ignorePointerEventsNone": False,
        },
    ]


@pytest.mark.asyncio
async def test_two_same_url_unnamed_oopifs_resolve_in_the_frame_that_was_clicked() -> None:
    """Two identical cross-origin widgets on one page: url+name can't tell them apart, so the
    child session has to confirm its own frame id or the click resolves in the wrong document."""
    iframe_node = {"node": {"nodeId": 21, "backendNodeId": 20, "nodeName": "IFRAME", "frameId": "second-widget"}}
    frame_tree = {
        "frameTree": {
            "frame": {"id": "main", "url": "https://example.test/form"},
            "childFrames": [
                {"frame": {"id": "first-widget", "url": "https://pay.test/widget"}},
                {"frame": {"id": "second-widget", "url": "https://pay.test/widget"}},
            ],
        }
    }
    parent = _ScriptedSession(
        {
            "Target.getTargetInfo": [{"targetInfo": {"targetId": "target-1"}}],
            "DOM.getNodeForLocation": [{"backendNodeId": 20, "frameId": "second-widget"}],
            "DOM.describeNode": [iframe_node],
            "Page.getFrameTree": [frame_tree],
            "DOM.getBoxModel": [{"model": {"content": [100, 200, 300, 200, 300, 400, 100, 400]}}],
        }
    )
    wrong = _ScriptedSession({"Page.getFrameTree": [{"frameTree": {"frame": {"id": "first-widget"}}}]})
    right_responses = _input_responses(frame_id="second-widget", element_id="card")
    right_responses["Target.getTargetInfo"] = []
    right_responses["DOM.getNodeForLocation"] = [{"backendNodeId": 7, "frameId": "second-widget"}]
    right_responses["Page.getFrameTree"] = [{"frameTree": {"frame": {"id": "second-widget"}}}]
    right = _ScriptedSession(right_responses)
    main_frame = _Frame("https://example.test/form", "")
    first = _Frame("https://pay.test/widget", "", main_frame)
    second = _Frame("https://pay.test/widget", "", main_frame)
    sessions = {id(first): wrong, id(second): right}

    async def new_cdp_session(target_frame: Any) -> _ScriptedSession:
        return sessions[id(target_frame)]

    resolver = Resolver(GestureLedger("pbs-twin-oopif"))
    gesture = _gesture(x=115, y=225)

    resolver.on_gesture(gesture, parent, _page([main_frame, first, second], new_cdp_session))
    await _drain(resolver)

    assert gesture.frame_id == "second-widget"
    assert gesture.selector == "#card"


@pytest.mark.asyncio
async def test_a_navigation_on_the_same_session_does_not_reuse_a_stale_document_root() -> None:
    """DOM node ids are invalidated on every document swap; a cached root makes the uniqueness
    check query a node id that no longer exists, and every locator degrades silently."""
    responses = _input_responses()
    responses["DOM.getDocument"] = [{"root": {"nodeId": 1}}, {"root": {"nodeId": 42}}]
    responses["Target.getTargetInfo"] *= 2
    responses["DOM.getNodeForLocation"] *= 2
    responses["DOM.describeNode"] *= 2
    responses["Accessibility.getPartialAXTree"] *= 2
    session = _ScriptedSession(responses)
    resolver = Resolver(GestureLedger("pbs-nav"))

    for _ in range(2):
        resolver.on_gesture(_gesture(), session, _page())
        await _drain(resolver)

    roots = [params["nodeId"] for method, params in session.calls if method == "DOM.querySelectorAll"]
    assert roots == [1, 42]
    assert [method for method, _ in session.calls].count("DOM.enable") == 1


@pytest.mark.asyncio
async def test_closed_shadow_target_uses_host_selector_and_inner_path() -> None:
    responses = _input_responses()
    responses["DOM.describeNode"] = [
        {
            "node": {
                "nodeId": 8,
                "parentId": 9,
                "backendNodeId": 7,
                "nodeName": "INPUT",
                "frameId": "main",
                "attributes": ["id", "inner", "type", "text"],
            }
        },
        {"node": {"nodeId": 9, "parentId": 10, "nodeType": 11, "shadowRootType": "closed"}},
        {
            "node": {
                "nodeId": 10,
                "nodeType": 1,
                "nodeName": "SECRET-FIELD",
                "attributes": ["id", "field-host"],
            }
        },
    ]
    session = _ScriptedSession(responses)
    resolver = Resolver(GestureLedger("pbs-shadow"))
    gesture = _gesture()

    resolver.on_gesture(gesture, session, _page())
    await _drain(resolver)

    assert gesture.selector == "#field-host"
    assert gesture.shadow_path == ["#inner"]
    assert ("DOM.querySelectorAll", {"nodeId": 9, "selector": "#inner"}) in session.calls


@pytest.mark.asyncio
async def test_forget_session_drops_the_per_session_caches() -> None:
    responses = _input_responses()
    responses["DOM.getDocument"] *= 2
    responses["Target.getTargetInfo"] *= 2
    responses["DOM.getNodeForLocation"] *= 2
    responses["DOM.describeNode"] *= 2
    responses["Accessibility.getPartialAXTree"] *= 2
    session = _ScriptedSession(responses)
    resolver = Resolver(GestureLedger("pbs-forget"))

    resolver.on_gesture(_gesture(), session, _page())
    await _drain(resolver)
    resolver.forget_session(session)
    resolver.on_gesture(_gesture(), session, _page())
    await _drain(resolver)

    methods = [method for method, _ in session.calls]
    assert methods.count("DOM.getDocument") == 2
    assert methods.count("Target.getTargetInfo") == 2


@pytest.mark.asyncio
async def test_resolution_failures_are_swallowed_and_close_cancels_pending_tasks() -> None:
    failing = _ScriptedSession({"Target.getTargetInfo": [RuntimeError("send failed")]})
    page = _page()
    resolver = Resolver(GestureLedger("pbs-failure"))
    gesture = _gesture()

    resolver.on_gesture(gesture, failing, page)
    await _drain(resolver)

    started = asyncio.Event()

    class _PendingSession:
        async def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            started.set()
            await asyncio.Event().wait()
            return {}

    pending_gesture = _gesture()
    resolver.on_gesture(pending_gesture, _PendingSession(), page)
    await started.wait()
    tasks = tuple(resolver._tasks)
    resolver.close()
    await asyncio.gather(*tasks, return_exceptions=True)

    facts = fold(
        [
            pending_gesture,
            Gesture(
                seq=2,
                t_received=2.0,
                kind="key",
                page_key=pending_gesture.page_key,
                url=pending_gesture.url,
                key="s",
                text="s",
                key_event_type="keyDown",
            ),
        ]
    )

    assert gesture.selector is None
    assert tasks
    assert all(task.cancelled() for task in tasks)
    assert facts[1].typed_value is None
    assert facts[1].typed_length == 1
