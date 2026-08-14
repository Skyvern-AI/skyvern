"""Unit tests for the Task V3 raw-browser tools.

A fake Playwright page records calls so we can assert each tool dispatches raw browser
operations (no task-ecosystem) with the right args, without a live browser.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from skyvern.forge.taskv3.tools import build_browser_tools


class _FakeElement:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def inner_html(self) -> str:
        return "<div>inner</div>"

    async def scroll_into_view_if_needed(self) -> None:
        self.calls.append(("scroll_into_view", None))

    async def set_input_files(self, paths: Any) -> None:
        self.calls.append(("set_input_files", paths))


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.test/apply"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.element = _FakeElement()

    async def evaluate(self, _js: str) -> str:
        return json.dumps(
            {
                "url": self.url,
                "title": "Apply",
                "elements": [
                    {
                        "i": 0,
                        "tag": "input",
                        "type": "text",
                        "selector": "#first",
                        "label": "First name",
                        "required": True,
                    },
                    {
                        "i": 1,
                        "tag": "select",
                        "type": None,
                        "selector": "#country",
                        "label": "Country",
                        "options": ["us|United States", "ca|Canada"],
                    },
                    {
                        "i": 2,
                        "tag": "input",
                        "type": "checkbox",
                        "selector": "#agree",
                        "label": "I agree",
                        "checked": False,
                        "group": "Consent: I agree to the terms and privacy policy",
                    },
                ],
            }
        )

    async def content(self) -> str:
        return "<html><body>full page</body></html>"

    async def query_selector(self, selector: str):
        self.calls.append(("query_selector", {"selector": selector}))
        return self.element

    async def click(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("click", {"selector": selector}))

    async def fill(self, selector: str, text: str, timeout: int | None = None) -> None:
        self.calls.append(("fill", {"selector": selector, "text": text}))

    async def type(self, selector: str, text: str, timeout: int | None = None) -> None:
        self.calls.append(("type", {"selector": selector, "text": text}))

    async def select_option(
        self, selector: str, value: Any = None, label: Any = None, timeout: int | None = None
    ) -> None:
        self.calls.append(("select_option", {"selector": selector, "value": value, "label": label}))

    async def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", {"selector": selector, "key": key}))

    async def goto(self, url: str, timeout: int | None = None, wait_until: str | None = None) -> None:
        self.url = url
        self.calls.append(("goto", {"url": url}))

    async def wait_for_selector(self, selector: str, state: str = "visible", timeout: int | None = None) -> None:
        self.calls.append(("wait_for_selector", {"selector": selector, "state": state, "timeout": timeout}))

    class _KB:
        def __init__(self, page: _FakePage) -> None:
            self._page = page

        async def press(self, key: str) -> None:
            self._page.calls.append(("kb_press", {"key": key}))

    class _Mouse:
        def __init__(self, page: _FakePage) -> None:
            self._page = page

        async def wheel(self, dx: int, dy: int) -> None:
            self._page.calls.append(("wheel", {"dy": dy}))

    @property
    def keyboard(self) -> _FakePage._KB:
        return _FakePage._KB(self)

    @property
    def mouse(self) -> _FakePage._Mouse:
        return _FakePage._Mouse(self)


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_tool_set_and_no_task_ecosystem_tools() -> None:
    tools = build_browser_tools(_FakePage())
    names = {t.name for t in tools}
    assert {
        "observe",
        "get_html",
        "click",
        "type",
        "select_option",
        "press_key",
        "scroll",
        "wait",
        "navigate",
        "file_upload",
    } <= names
    # The whole point: no task-ecosystem / LLM-backed tools in the raw harness.
    assert not ({"act", "extract", "validate", "login", "run_task"} & names)


@pytest.mark.asyncio
async def test_observe_renders_selectors_labels_options() -> None:
    tools = build_browser_tools(_FakePage())
    r = await _tool(tools, "observe").handler({})
    assert r.status == "ok"
    assert "#first" in r.content and "First name" in r.content
    assert "#country" in r.content and "United States" in r.content
    assert "*required" in r.content


@pytest.mark.asyncio
async def test_observe_renders_checkbox_checked_state() -> None:
    # observe must surface checked-state so a required consent box can be re-verified.
    tools = build_browser_tools(_FakePage())
    r = await _tool(tools, "observe").handler({})
    assert "#agree" in r.content and "checked=" in r.content


@pytest.mark.asyncio
async def test_observe_renders_group_context() -> None:
    # Controls whose meaning lives in surrounding text (radio/checkbox groups, weak labels) carry a
    # `group` field with the question text, so the agent can answer without fetching raw HTML.
    tools = build_browser_tools(_FakePage())
    r = await _tool(tools, "observe").handler({})
    assert "group=" in r.content
    assert "Consent: I agree to the terms" in r.content


@pytest.mark.asyncio
async def test_click_type_select_dispatch_raw_ops() -> None:
    page = _FakePage()
    tools = build_browser_tools(page)
    await _tool(tools, "click").handler({"selector": "#submit"})
    await _tool(tools, "type").handler({"selector": "#first", "text": "John"})
    await _tool(tools, "select_option").handler({"selector": "#country", "label": "United States"})
    kinds = {c[0]: c[1] for c in page.calls}
    assert kinds["click"]["selector"] == "#submit"
    assert kinds["fill"] == {"selector": "#first", "text": "John"}
    assert kinds["select_option"]["label"] == "United States"


@pytest.mark.asyncio
async def test_navigate_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)  # no DNS in unit tests
    page = _FakePage()
    tools = build_browser_tools(page)
    r = await _tool(tools, "navigate").handler({"url": "https://jobs.example.test/acme/123"})
    assert r.status == "ok" and page.url.endswith("/acme/123")
    r2 = await _tool(tools, "wait").handler({"selector": "#next", "state": "visible"})
    assert r2.status == "ok"
    assert any(c[0] == "wait_for_selector" for c in page.calls)


@pytest.mark.asyncio
async def test_navigate_validates_url_before_goto(monkeypatch: pytest.MonkeyPatch) -> None:
    # A model-chosen URL must go through the repo's SSRF/URL validation before page.goto.
    import skyvern.utils.url_validators as urlv

    def _reject(url: str) -> str:
        raise ValueError("blocked host")

    monkeypatch.setattr(urlv, "validate_fetch_url", _reject)
    page = _FakePage()
    tools = build_browser_tools(page)
    with pytest.raises(ValueError):
        await _tool(tools, "navigate").handler({"url": "http://169.254.169.254/latest/meta-data/"})
    assert not any(c[0] == "goto" for c in page.calls)  # never navigated


@pytest.mark.asyncio
async def test_handler_error_is_captured_not_raised() -> None:
    page = _FakePage()

    async def boom(*a, **k):
        raise RuntimeError("detached")

    page.click = boom  # type: ignore[assignment]
    tools = build_browser_tools(page)
    # ToolSpec handlers may raise; the loop converts to an error result. Here we assert the
    # handler itself raises so the loop's try/except path is what catches it (parity with loop.py).
    with pytest.raises(RuntimeError):
        await _tool(tools, "click").handler({"selector": "#x"})


def test_build_action_maps_each_tool_to_action_model() -> None:
    from skyvern.forge.taskv3.preflight import _build_action
    from skyvern.webeye.actions.actions import (
        ClickAction,
        GotoUrlAction,
        InputTextAction,
        KeypressAction,
        SelectOptionAction,
        UploadFileAction,
    )

    click = _build_action("click", {"selector": "#submit"})
    assert isinstance(click, ClickAction) and click.element_id == "#submit"

    typed = _build_action("type", {"selector": "#first", "text": "John"})
    assert isinstance(typed, InputTextAction) and typed.element_id == "#first" and typed.text == "John"

    sel = _build_action("select_option", {"selector": "#country", "value": "us"})
    assert isinstance(sel, SelectOptionAction) and sel.option is not None and sel.option.value == "us"

    key = _build_action("press_key", {"key": "Enter"})
    assert isinstance(key, KeypressAction) and key.keys == ["Enter"]

    upload = _build_action("file_upload", {"selector": "#cv", "file": "/tmp/cv.pdf"})
    assert isinstance(upload, UploadFileAction) and upload.file_url == "/tmp/cv.pdf"

    nav = _build_action("navigate", {"url": "https://example.test/apply"})
    assert isinstance(nav, GotoUrlAction) and nav.url == "https://example.test/apply"

    # A navigate with no URL yields no action (nothing to evaluate); observe/wait aren't preflighted.
    assert _build_action("navigate", {}) is None
    assert _build_action("observe", {}) is None


@pytest.mark.asyncio
async def test_wrapped_tool_skips_preflight_when_policy_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("skyvern.forge.taskv3.preflight.policy_observation_enabled", lambda: False)
    monkeypatch.setattr("skyvern.forge.taskv3.preflight.preflight_action", lambda *a, **k: calls.append(a))

    page = _FakePage()
    tools = build_browser_tools(page)
    await _tool(tools, "click").handler({"selector": "#submit"})

    # No policy call when disabled, but the underlying browser op still runs.
    assert calls == []
    assert any(c[0] == "click" and c[1]["selector"] == "#submit" for c in page.calls)


@pytest.mark.asyncio
async def test_wrapped_tool_runs_preflight_when_observation_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, str]] = []
    monkeypatch.setattr("skyvern.forge.taskv3.preflight.policy_observation_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.taskv3.preflight.preflight_action",
        lambda action, page, *, site: seen.append((action, site)),
    )

    page = _FakePage()
    tools = build_browser_tools(page)
    await _tool(tools, "click").handler({"selector": "#submit"})

    assert len(seen) == 1
    action, site = seen[0]
    assert site == "taskv3-click"
    assert action.element_id == "#submit"
    # Preflight is observe-only: the click still executes.
    assert any(c[0] == "click" and c[1]["selector"] == "#submit" for c in page.calls)


@pytest.mark.asyncio
async def test_wait_caps_selector_timeout() -> None:
    page = _FakePage()
    tools = build_browser_tools(page)
    wait = _tool(tools, "wait")

    await wait.handler({"selector": "#ready", "timeout_ms": 999999})
    capped = next(c for c in page.calls if c[0] == "wait_for_selector")
    assert capped[1]["timeout"] == 30000  # model-supplied timeout is clamped

    page.calls.clear()
    await wait.handler({"selector": "#ready"})
    default = next(c for c in page.calls if c[0] == "wait_for_selector")
    assert default[1]["timeout"] == 15000  # default when unspecified


def test_preflight_tool_set_matches_builder() -> None:
    # Pin the exact preflight set so narrowing it (e.g. dropping `navigate`, the one target whose
    # origin the policy can act on) fails a test rather than silently shrinking coverage.
    from skyvern.forge.taskv3.preflight import PREFLIGHT_TOOL_NAMES, _build_action

    assert PREFLIGHT_TOOL_NAMES == {"click", "type", "select_option", "press_key", "file_upload", "navigate"}
    rep = {
        "click": {"selector": "#x"},
        "type": {"selector": "#x", "text": "y"},
        "select_option": {"selector": "#x", "value": "v"},
        "press_key": {"key": "Enter"},
        "file_upload": {"selector": "#x", "file": "/tmp/f"},
        "navigate": {"url": "https://example.test/x"},
    }
    for name in PREFLIGHT_TOOL_NAMES:
        assert _build_action(name, rep[name]) is not None, name
    # Perception/benign tools carry no policy-relevant action and are not preflighted.
    for name in ("observe", "get_html", "scroll", "wait"):
        assert name not in PREFLIGHT_TOOL_NAMES


@pytest.mark.asyncio
async def test_navigate_runs_preflight_when_observation_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)
    seen: list[tuple[Any, str]] = []
    monkeypatch.setattr("skyvern.forge.taskv3.preflight.policy_observation_enabled", lambda: True)
    monkeypatch.setattr(
        "skyvern.forge.taskv3.preflight.preflight_action",
        lambda action, page, *, site: seen.append((action, site)),
    )
    page = _FakePage()
    tools = build_browser_tools(page)
    await _tool(tools, "navigate").handler({"url": "https://jobs.example.test/acme/1"})

    assert len(seen) == 1
    action, site = seen[0]
    assert site == "taskv3-navigate"
    assert action.url == "https://jobs.example.test/acme/1"  # PAGE target the origin check can act on
