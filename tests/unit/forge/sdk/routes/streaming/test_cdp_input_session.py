"""A transient page-resolution failure must not drop an already-bound CDP session: the dispatch loop
treats None as "no active page" and silently skips the event while the channel stays open, so the
user keeps interacting with a surface that no longer receives input."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from skyvern.forge.sdk.routes.streaming import cdp_input


class _FakeSession:
    def __init__(self, name: str, history: dict | None = None) -> None:
        self.name = name
        self.detached = False
        self.sent: list[tuple[str, dict]] = []
        self.history = history

    async def detach(self) -> None:
        self.detached = True

    async def send(self, method: str, params: dict) -> dict | None:
        self.sent.append((method, params))
        if method == "Page.getNavigationHistory":
            return self.history
        return None


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def new_cdp_session(self, page: object) -> _FakeSession:
        return self._session


class _FakePage:
    def __init__(self, session: _FakeSession, url: str = "https://example.test/") -> None:
        self.context = _FakeContext(session)
        self.url = url


def _build(monkeypatch: pytest.MonkeyPatch, page: Any) -> tuple[cdp_input.ActivePageCdpInputSession, list[Any]]:
    resolved: list[Any] = [page]

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        return resolved[0]

    monkeypatch.setattr(cdp_input, "_resolve_working_page", _resolve)
    input_session = cdp_input.ActivePageCdpInputSession(
        browser_state=object(),  # type: ignore[arg-type]
        entity_id="wr_test",
        entity_type="workflow_run",
    )
    return input_session, resolved


@pytest.mark.asyncio
async def test_bound_session_survives_a_transient_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession("first")
    input_session, resolved = _build(monkeypatch, _FakePage(session))

    assert await input_session.get_session() is session

    resolved[0] = None

    assert await input_session.get_session(force_refresh=True) is session
    assert input_session.page_resolution_failed is False
    assert session.detached is False


@pytest.mark.asyncio
async def test_resolution_failure_before_any_bind_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    input_session, _ = _build(monkeypatch, None)

    assert await input_session.get_session() is None
    assert input_session.page_resolution_failed is True


@pytest.mark.asyncio
async def test_rebinds_when_the_working_page_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _FakeSession("first"), _FakeSession("second")
    input_session, resolved = _build(monkeypatch, _FakePage(first))

    assert await input_session.get_session() is first

    resolved[0] = _FakePage(second)

    assert await input_session.get_session(force_refresh=True) is second
    assert first.detached is True


class _FakeNavigablePage:
    """Stands in for the Playwright page `_dispatch_navigate_event` calls `goto()` on."""

    def __init__(self, response: object = None) -> None:
        self.goto_calls: list[str] = []
        self._response = response

    async def goto(self, url: str) -> object:
        self.goto_calls.append(url)
        return self._response


class _FakeInputSession:
    def __init__(self, cdp_session: _FakeSession, page: object = None) -> None:
        self._cdp_session = cdp_session
        self.page = page if page is not None else _FakeNavigablePage()

    async def get_session(self, *, force_refresh: bool = False) -> _FakeSession:
        return self._cdp_session


class _FakeWebSocket:
    """`_run_input_loop` only touches `receive_text`, `send_json`, and `close`."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent_json: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def receive_text(self) -> str:
        if not self._messages:
            raise WebSocketDisconnect()
        return self._messages.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent_json.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class TestNavigateEvent:
    """SKY-13683: a live-view URL input navigates the remote page over the existing
    cdp_input WebSocket, gated by the same take-control check as mouse/keyboard input,
    and validated through the same SSRF guard every real page navigation goes through."""

    @pytest.mark.asyncio
    async def test_dropped_when_not_in_control(self) -> None:
        """The interactor gate lives server-side (cdp_input._run_input_loop), not just in
        the frontend hiding the input box -- anyone dialing the websocket directly without
        having taken control must not be able to redirect the page."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.com"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == []
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_dispatches_page_navigate_after_take_control(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"kind": "take-control"}),
                json.dumps({"type": "navigateEvent", "url": "https://example.com/path"}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == ["https://example.com/path"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_rejects_blocked_destination_via_the_real_ssrf_guard(self) -> None:
        """Uses the real validate_navigation_destination (no monkeypatch) against a known
        cloud-metadata IP: if the guard call is ever deleted or bypassed, page.goto WOULD
        get dispatched here and this assertion goes red -- that is the point of the test."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [json.dumps({"type": "navigateEvent", "url": "http://169.254.169.254/latest/meta-data/"})]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_rejects_empty_url_without_dispatching(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "   "})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "invalid_url"}]

    @pytest.mark.asyncio
    async def test_allows_a_public_destination_through_the_real_guard(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.org/"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == ["https://example.org/"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_normalizes_a_bare_host_before_dispatch(self) -> None:
        """A schemeless entry like `example.org` passes validation because a scheme is
        prepended for the check; the browser must be sent that same normalized value, not
        the raw user text, or a scheme-less string reaching page.goto behaves differently
        (and any dispatch failure would close the whole channel instead of erroring inline)."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "example.org"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == ["https://example.org"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_navigate_blocked_via_redirect_chain_resets_the_page(self) -> None:
        """page.goto follows redirects at the network layer, so a destination that itself
        passes validate_navigation_destination can still land on a blocked host after a
        redirect. Uses the real revalidate_redirect_chain/validate_navigation_destination
        (no monkeypatch): remove the revalidation call and this test's second assertion on
        goto_calls goes red, since the page would be left sitting on the blocked content."""
        final_request = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            redirected_from=SimpleNamespace(url="https://example.org/redirect", redirected_from=None),
        )
        response = SimpleNamespace(request=final_request)
        page = _FakeNavigablePage(response=response)
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.org/redirect"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert page.goto_calls == ["https://example.org/redirect", "about:blank"]
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]


class TestInteractiveInputDispatch:
    def test_key_event_preserves_supported_editing_commands(self) -> None:
        assert cdp_input._validate_key_event(
            {
                "eventType": "rawKeyDown",
                "key": "ArrowLeft",
                "code": "ArrowLeft",
                "modifiers": 4,
                "windowsVirtualKeyCode": 37,
                "commands": ["moveToLeftEndOfLine"],
            }
        ) == {
            "type": "rawKeyDown",
            "key": "ArrowLeft",
            "code": "ArrowLeft",
            "modifiers": 4,
            "windowsVirtualKeyCode": 37,
            "commands": ["moveToLeftEndOfLine"],
        }

    def test_key_event_drops_unknown_editing_commands(self) -> None:
        validated = cdp_input._validate_key_event(
            {
                "eventType": "rawKeyDown",
                "key": "a",
                "code": "KeyA",
                "commands": ["selectAll", "notARealEditingCommand"],
            }
        )

        assert validated is not None
        assert validated["commands"] == ["selectAll"]

    def test_key_event_preserves_word_selection_command(self) -> None:
        validated = cdp_input._validate_key_event(
            {
                "eventType": "rawKeyDown",
                "key": "ArrowRight",
                "code": "ArrowRight",
                "modifiers": 9,
                "windowsVirtualKeyCode": 39,
                "commands": ["moveWordRightAndModifySelection"],
            }
        )

        assert validated is not None
        assert validated["commands"] == ["moveWordRightAndModifySelection"]

    def test_mouse_move_preserves_pressed_buttons(self) -> None:
        assert cdp_input._validate_mouse_event(
            {
                "eventType": "mouseMoved",
                "x": 10,
                "y": 20,
                "button": "left",
                "buttons": 1,
            }
        ) == {
            "type": "mouseMoved",
            "x": 10,
            "y": 20,
            "button": "left",
            "buttons": 1,
            "clickCount": 0,
            "modifiers": 0,
        }

    @pytest.mark.asyncio
    async def test_insert_text_dispatches_through_cdp(self) -> None:
        session = _FakeSession("s")
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "insertText", "text": "from local clipboard"})])

        await cdp_input._run_input_loop(
            websocket,
            channel,
            input_session,
            "browser_session_id",
            "pbs_test",
        )

        assert _dispatched(session) == [("Input.insertText", {"text": "from local clipboard"})]

    @pytest.mark.asyncio
    async def test_copy_selected_text_returns_remote_selection(self) -> None:
        class _SelectionSession(_FakeSession):
            async def send(self, method: str, params: dict) -> dict | None:
                self.sent.append((method, params))
                if method == "Runtime.evaluate":
                    return {"result": {"value": "selected remotely"}}
                return None

        session = _SelectionSession("s")
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "copySelectedText"})])

        await cdp_input._run_input_loop(
            websocket,
            channel,
            input_session,
            "browser_session_id",
            "pbs_test",
        )

        assert websocket.sent_json == [{"kind": "copied-text", "text": "selected remotely"}]


def _history(current_index: int, *urls: str) -> dict:
    return {
        "currentIndex": current_index,
        "entries": [{"id": i, "url": url} for i, url in enumerate(urls)],
    }


def _dispatched(session: _FakeSession) -> list[tuple[str, dict]]:
    return [call for call in session.sent if call[0] != "Page.getNavigationHistory"]


class TestHistoryNavigation:
    """SKY-13724: the live-view browser chrome drives back/forward/reload over the same
    cdp_input socket, behind the same take-control gate, and re-validates the history entry
    it is about to replay rather than trusting that everything in the back stack was safe."""

    @pytest.mark.asyncio
    async def test_back_navigates_to_the_previous_entry(self) -> None:
        session = _FakeSession("s", history=_history(1, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert _dispatched(session) == [("Page.navigateToHistoryEntry", {"entryId": 0})]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_forward_navigates_to_the_next_entry(self) -> None:
        session = _FakeSession("s", history=_history(0, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goForwardEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert _dispatched(session) == [("Page.navigateToHistoryEntry", {"entryId": 1})]

    @pytest.mark.asyncio
    async def test_reload_reloads_rather_than_replaying_a_history_entry(self) -> None:
        session = _FakeSession("s", history=_history(0, "https://example.org/one"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "reloadEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert _dispatched(session) == [("Page.reload", {})]

    @pytest.mark.asyncio
    async def test_back_at_the_start_of_history_is_a_no_op(self) -> None:
        """The frontend leaves the buttons enabled, so the end-of-stack check has to live
        here; walking off the end must not raise and must not close the input channel."""
        session = _FakeSession("s", history=_history(0, "https://example.org/one"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert _dispatched(session) == []
        assert websocket.sent_json == []
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_refuses_to_replay_a_blocked_entry_left_in_the_back_stack(self) -> None:
        """A destination blocked mid-redirect resets the page but stays in the history, so
        going back would re-request it. Uses the real validate_navigation_destination (no
        monkeypatch): drop the guard from _dispatch_history_event and this goes red."""
        session = _FakeSession(
            "s",
            history=_history(1, "http://169.254.169.254/latest/meta-data/", "https://example.org/two"),
        )
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert _dispatched(session) == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]

    @pytest.mark.asyncio
    async def test_dropped_when_not_in_control(self) -> None:
        session = _FakeSession("s", history=_history(1, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test")

        assert session.sent == []
