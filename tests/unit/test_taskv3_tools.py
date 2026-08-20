"""Unit tests for the Task V3 raw-browser tools.

A fake Playwright page records calls so we can assert each tool dispatches raw browser
operations (no task-ecosystem) with the right args, without a live browser.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from skyvern.forge.taskv3.tools import PAGE_UNAVAILABLE_ERROR, build_browser_tools


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


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

    async def eval_on_selector(self, selector: str, js: str) -> str:
        # Field-type probe: report a non-typeahead type so legacy tests exercise the plain fill path.
        return "password"

    async def hover(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("hover", {"selector": selector}))

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

    async def type(self, selector: str, text: str, delay: int | None = None, timeout: int | None = None) -> None:
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


def _fixed_page_provider(page: Any) -> Callable[[], Awaitable[Any]]:
    """A provider that always resolves to the same `page` object (today's bound-once shape)."""

    async def _provider() -> Any:
        return page

    return _provider


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_tool_set_and_no_task_ecosystem_tools() -> None:
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
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
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
    r = await _tool(tools, "observe").handler({})
    assert r.status == "ok"
    assert "#first" in r.content and "First name" in r.content
    assert "#country" in r.content and "United States" in r.content
    assert "*required" in r.content


@pytest.mark.asyncio
async def test_observe_renders_checkbox_checked_state() -> None:
    # observe must surface checked-state so a required consent box can be re-verified.
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
    r = await _tool(tools, "observe").handler({})
    assert "#agree" in r.content and "checked=" in r.content


@pytest.mark.asyncio
async def test_observe_renders_group_context() -> None:
    # Controls whose meaning lives in surrounding text (radio/checkbox groups, weak labels) carry a
    # `group` field with the question text, so the agent can answer without fetching raw HTML.
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
    r = await _tool(tools, "observe").handler({})
    assert "group=" in r.content
    assert "Consent: I agree to the terms" in r.content


@pytest.mark.asyncio
async def test_click_type_select_dispatch_raw_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)  # the typeahead probe polls; skip real waiting in tests
    page = _FakePage()
    tools = build_browser_tools(_fixed_page_provider(page))
    await _tool(tools, "click").handler({"selector": "#submit"})
    await _tool(tools, "type").handler({"selector": "#first", "text": "John"})
    await _tool(tools, "select_option").handler({"selector": "#country", "label": "United States"})
    assert ("click", {"selector": "#submit"}) in page.calls
    # `type` keystroke-types the value (typeahead-safe path) — the value is entered via page.type
    assert ("fill", {"selector": "#first", "text": "John"}) in page.calls
    assert ("select_option", {"selector": "#country", "value": None, "label": "United States"}) in page.calls


@pytest.mark.asyncio
async def test_navigate_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)  # no DNS in unit tests
    page = _FakePage()
    tools = build_browser_tools(_fixed_page_provider(page))
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
    tools = build_browser_tools(_fixed_page_provider(page))
    with pytest.raises(ValueError):
        await _tool(tools, "navigate").handler({"url": "http://169.254.169.254/latest/meta-data/"})
    assert not any(c[0] == "goto" for c in page.calls)  # never navigated


@pytest.mark.asyncio
async def test_handler_error_is_captured_not_raised() -> None:
    page = _FakePage()

    async def boom(*a, **k):
        raise RuntimeError("detached")

    page.click = boom  # type: ignore[assignment]
    tools = build_browser_tools(_fixed_page_provider(page))
    # ToolSpec handlers may raise; the loop converts to an error result. Here we assert the
    # handler itself raises so the loop's try/except path is what catches it (parity with loop.py).
    with pytest.raises(RuntimeError):
        await _tool(tools, "click").handler({"selector": "#x"})


@pytest.mark.asyncio
async def test_tool_resolves_page_at_call_time_not_bound_once() -> None:
    # The core fix: a call must resolve the CURRENT page from the provider, not a page captured
    # once when the tools were built — so a click landing on a new tab/popup is followed.
    page_a, page_b = _FakePage(), _FakePage()
    current: dict[str, Any] = {"page": page_a}

    async def provider() -> Any:
        return current["page"]

    tools = build_browser_tools(provider)
    await _tool(tools, "click").handler({"selector": "#first"})
    current["page"] = page_b
    await _tool(tools, "click").handler({"selector": "#second"})

    assert any(c[0] == "click" and c[1]["selector"] == "#first" for c in page_a.calls)
    assert not any(c[1].get("selector") == "#second" for c in page_a.calls)
    assert any(c[0] == "click" and c[1]["selector"] == "#second" for c in page_b.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("observe", {}),
        ("get_html", {}),
        ("click", {"selector": "#x"}),
        ("type", {"selector": "#x", "text": "y"}),
        ("wait", {"time_ms": 10}),
        ("navigate", {"url": "https://example.test/x"}),
    ],
)
async def test_tool_reports_page_unavailable_when_provider_returns_none(tool_name: str, args: dict[str, Any]) -> None:
    async def gone_provider() -> Any:
        return None

    tools = build_browser_tools(gone_provider)
    r = await _tool(tools, tool_name).handler(args)
    assert r.status == "error"
    assert r.content == PAGE_UNAVAILABLE_ERROR


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
    tools = build_browser_tools(_fixed_page_provider(page))
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
    tools = build_browser_tools(_fixed_page_provider(page))
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
    tools = build_browser_tools(_fixed_page_provider(page))
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

    assert PREFLIGHT_TOOL_NAMES == {
        "click",
        "hover",
        "type",
        "select_combobox",
        "select_option",
        "press_key",
        "file_upload",
        "navigate",
    }
    rep = {
        "click": {"selector": "#x"},
        "hover": {"selector": "#x"},
        "type": {"selector": "#x", "text": "y"},
        "select_combobox": {"selector": "#x", "value": "v"},
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
    tools = build_browser_tools(_fixed_page_provider(page))
    await _tool(tools, "navigate").handler({"url": "https://jobs.example.test/acme/1"})

    assert len(seen) == 1
    action, site = seen[0]
    assert site == "taskv3-navigate"
    assert action.url == "https://jobs.example.test/acme/1"  # PAGE target the origin check can act on


@pytest.mark.asyncio
async def test_type_resolves_secret_placeholder_at_fill_time() -> None:
    # Workflow credentials reach the model only as placeholders; the real value must be what
    # lands in the page, while the tool result echoes neither.
    page = _FakePage()
    tools = build_browser_tools(
        _fixed_page_provider(page),
        resolve_typed_text=lambda text: "real-secret" if text == "placeholder_abc" else text,
    )
    result = await _tool(tools, "type").handler({"selector": "#password", "text": "placeholder_abc"})
    kinds = {c[0]: c[1] for c in page.calls}
    assert kinds["fill"] == {"selector": "#password", "text": "real-secret"}
    assert "real-secret" not in result.content
    assert "placeholder_abc" not in result.content


@pytest.mark.asyncio
async def test_type_resolver_failure_or_non_string_falls_back_to_literal() -> None:
    page = _FakePage()

    def _raises(_text: str) -> str:
        raise RuntimeError("resolver blew up")

    tools = build_browser_tools(_fixed_page_provider(page), resolve_typed_text=_raises)
    await _tool(tools, "type").handler({"selector": "#a", "text": "literal-1"})
    tools = build_browser_tools(_fixed_page_provider(page), resolve_typed_text=lambda _t: None)
    await _tool(tools, "type").handler({"selector": "#b", "text": "literal-2"})
    filled = [c[1] for c in page.calls if c[0] == "fill"]
    assert filled == [
        {"selector": "#a", "text": "literal-1"},
        {"selector": "#b", "text": "literal-2"},
    ]


@pytest.mark.asyncio
async def test_file_upload_resolves_secret_placeholder_and_does_not_echo() -> None:
    # A secret-bound file value reaches the model as a placeholder; the upload must resolve it
    # the same way type does (the step engine resolves UploadFileAction.file_url), and neither
    # the placeholder nor the resolved source may echo in the tool result.
    page = _FakePage()
    captured: dict[str, str] = {}

    async def fake_download_file(source: str, output_dir: str | None = None, organization_id: str | None = None) -> str:
        captured["source"] = source
        return "/tmp/downloaded-file.pdf"

    tools = build_browser_tools(
        _fixed_page_provider(page),
        resolve_typed_text=lambda text: "https://files.internal/real.pdf" if text == "placeholder_file" else text,
    )
    import skyvern.forge.sdk.api.files as files_module

    original = files_module.download_file
    files_module.download_file = fake_download_file  # type: ignore[assignment]
    try:
        result = await _tool(tools, "file_upload").handler({"selector": "#upload", "file": "placeholder_file"})
    finally:
        files_module.download_file = original
    assert captured["source"] == "https://files.internal/real.pdf"
    assert "placeholder_file" not in result.content
    assert "real.pdf" not in result.content


class _TypeaheadFakePage:
    """Fake page for the BEHAVIORAL typeahead path. `evaluate` stands in for the JS suggestion-finder
    (_FIND_SUGGESTION_JS, identified by the data-tv3-sugg tag it sets) and the commit-verifier
    (_VERIFY_COMMIT_JS, identified by its `closest` call), so type()/select_combobox control flow can be
    driven without a real DOM. `field_type` feeds the pre-probe input-type check; `suggestion` is what the
    finder returns (None => the page rendered nothing that overlaps the value); `committed` is the value
    the verifier reads back after the suggestion is clicked."""

    def __init__(
        self, *, field_type: str = "text", suggestion: dict[str, Any] | None = None, committed: str = ""
    ) -> None:
        self._field_type = field_type
        self._suggestion = suggestion
        self._committed = committed
        self.calls: list[tuple[str, Any]] = []
        self.clicked_suggestion = False

    async def eval_on_selector(self, selector: str, js: str) -> str:
        return self._field_type

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        # Order matters: the verify JS also references data-tv3-sugg (its list-closed check), so match
        # the verifier (identified by its `closest` call) first, then the finder.
        if "closest" in js:
            return self._committed
        if "data-tv3-sugg" in js:
            return self._suggestion
        return None

    async def click(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("click", selector))
        if selector == '[data-tv3-sugg="1"]':
            self.clicked_suggestion = True

    async def fill(self, selector: str, text: str, timeout: int | None = None) -> None:
        self.calls.append(("fill", (selector, text)))

    async def type(self, selector: str, text: str, delay: int | None = None, timeout: int | None = None) -> None:
        self.calls.append(("type", (selector, text)))

    async def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", (selector, key)))


async def _instant_sleep(*_a: Any, **_k: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_type_auto_commits_reacting_typeahead(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain `type` into a text field that REACTS with a suggestion list must commit the match itself
    # (the model does not reliably reach for select_combobox) and report the committed value.
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)
    page = _TypeaheadFakePage(
        field_type="text",
        suggestion={"text": "San Francisco, CA, USA", "score": 2},
        committed="San Francisco, CA, USA",
    )
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "type").handler({"selector": "#location-input", "text": "San Francisco, California"})
    assert r.status == "ok"
    assert "San Francisco, CA, USA" in r.content  # the value the widget committed (normalized), not raw text
    assert page.clicked_suggestion  # it clicked the tagged suggestion rather than leaving typed text


@pytest.mark.asyncio
async def test_type_leaves_raw_text_when_no_suggestion_reacts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Text field that does NOT react with a suggestion list is an ordinary field: keep the typed text,
    # do not click anything, do not error.
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)
    page = _TypeaheadFakePage(field_type="text", suggestion=None)
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "type").handler({"selector": "#notes", "text": "Springfield"})
    assert r.status == "ok"
    assert ("type", ("#notes", "Springfield")) in page.calls  # keystroke-typed; raw text left in place
    assert not page.clicked_suggestion


@pytest.mark.asyncio
async def test_type_non_text_input_skips_probe_fast_path() -> None:
    # A non-text input (email/tel/…) is never a typeahead: fast fill, no suggestion probe, no click —
    # even if a suggestion would have been offered.
    page = _TypeaheadFakePage(field_type="email", suggestion={"text": "x@y.com", "score": 9})
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "type").handler({"selector": "#email", "text": "john.smith@example.com"})
    assert r.status == "ok"
    assert ("fill", ("#email", "john.smith@example.com")) in page.calls
    assert not page.clicked_suggestion


@pytest.mark.asyncio
async def test_select_combobox_commits_reacting_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)
    page = _TypeaheadFakePage(
        field_type="text", suggestion={"text": "San Francisco, CA, USA", "score": 2}, committed="San Francisco, CA, USA"
    )
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "select_combobox").handler({"selector": "#loc", "value": "San Francisco, California"})
    assert r.status == "ok" and "San Francisco, CA, USA" in r.content
    assert page.clicked_suggestion


@pytest.mark.asyncio
async def test_select_combobox_fails_loud_when_no_suggestion_reacts(monkeypatch: pytest.MonkeyPatch) -> None:
    # No suggestion overlaps the value => no false "filled": select_combobox must error, never claim success.
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)
    page = _TypeaheadFakePage(field_type="text", suggestion=None)
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "select_combobox").handler({"selector": "#loc", "value": "San Francisco, California"})
    assert r.status == "error" and "NOT filled" in r.content


# --- DOM-level tests: exercise the REAL finder/pre-snapshot JS against a live page, so the safeguards
# (reaction-gate, container-vs-row, nav-exclusion) are actually executed, not mocked by a fake page. ---

_FINDER_FIXTURE_HTML = """
<!doctype html><html><body style="margin:0">
  <label for="location-input">Current location</label>
  <input id="location-input" type="text" style="position:absolute;top:100px;left:40px;width:300px;height:28px">
  <!-- Static page text present BEFORE typing. It shares — and OUTSCORES on — the typed value, so if the
       pre-snapshot reaction-gate weren't working it would win. It must be ignored. -->
  <div id="static-distractor" style="position:absolute;top:150px;left:40px;width:300px;height:30px">San Francisco California office openings</div>
</body></html>
"""


@contextlib.asynccontextmanager
async def _finder_page() -> AsyncIterator[Any]:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1024, "height": 900})
            page = await context.new_page()
            await page.set_content(_FINDER_FIXTURE_HTML)
            yield page
        finally:
            await browser.close()


async def _snapshot_react_find(page: Any, inject_js: str) -> Any:
    # Mirror _type_and_commit's order: snapshot the pre-typing DOM, let the widget "react" (inject its
    # dropdown), then run the finder — so only reacting DOM is eligible.
    from skyvern.forge.taskv3.tools import _FIND_SUGGESTION_JS, _PRESNAPSHOT_JS  # noqa: PLC0415

    await page.evaluate(_PRESNAPSHOT_JS)
    await page.evaluate(inject_js)
    return await page.evaluate(_FIND_SUGGESTION_JS, {"value": "San Francisco, California", "field": "#location-input"})


@_skip_no_browser
@pytest.mark.asyncio
async def test_finder_picks_row_over_container_and_ignores_static_text() -> None:
    # A dropdown container holding two rows appears under the field IN REACTION to typing. The finder must
    # (a) ignore the pre-existing static text that outscores the rows, and (b) tag the matching ROW — not
    # the container (whose center-click would land on the wrong row: "San Francisco" -> "San Jose").
    inject = """() => {
      const c = document.createElement('div');
      c.id = 'dd'; c.setAttribute('style', 'position:absolute;top:132px;left:40px;width:300px');
      for (const t of ['San Francisco, CA, USA', 'San Jose, CA, USA']) {
        const row = document.createElement('div');
        row.className = 'opt'; row.textContent = t; row.setAttribute('style', 'height:28px');
        c.appendChild(row);
      }
      document.body.appendChild(c);
    }"""
    async with _finder_page() as page:
        found = await _snapshot_react_find(page, inject)
        assert found is not None and found["text"] == "San Francisco, CA, USA"
        tagged = await page.eval_on_selector_all("[data-tv3-sugg]", "els => els.map(e => e.textContent.trim())")
        assert tagged == ["San Francisco, CA, USA"]  # exactly one tag, on the row (not the container)
        static_tagged = await page.eval_on_selector("#static-distractor", "e => e.hasAttribute('data-tv3-sugg')")
        assert static_tagged is False  # pre-existing text excluded despite its higher token score


@_skip_no_browser
@pytest.mark.asyncio
async def test_finder_excludes_navigational_anchor() -> None:
    # A matching <a href> appearing after typing is navigational (clicking would leave the form), so the
    # finder must never select it; with no non-nav candidate it returns null and tags nothing.
    inject = """() => {
      const a = document.createElement('a');
      a.href = '/somewhere'; a.textContent = 'San Francisco, CA, USA';
      a.setAttribute('style', 'position:absolute;top:132px;left:40px;width:300px;height:28px;display:block');
      document.body.appendChild(a);
    }"""
    async with _finder_page() as page:
        found = await _snapshot_react_find(page, inject)
        assert found is None
        anchor_tagged = await page.eval_on_selector("a", "e => e.hasAttribute('data-tv3-sugg')")
        assert anchor_tagged is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_finder_refuses_multirow_container() -> None:
    # A reacting element whose match comes from its own text but that stacks multiple visible child rows
    # is a list container, not a single suggestion — clicking its center would hit an arbitrary row, so
    # the finder must refuse it (return null) rather than commit a wrong value.
    inject = """() => {
      const c = document.createElement('div');
      c.id = 'ddc';
      c.setAttribute('style', 'position:absolute;top:132px;left:40px;width:300px');
      c.appendChild(document.createTextNode('San Francisco California'));
      for (const t of ['Result one', 'Result two']) {
        const row = document.createElement('div');
        row.textContent = t; row.setAttribute('style', 'height:28px');
        c.appendChild(row);
      }
      document.body.appendChild(c);
    }"""
    async with _finder_page() as page:
        found = await _snapshot_react_find(page, inject)
        assert found is None
        any_tagged = await page.eval_on_selector_all("[data-tv3-sugg]", "els => els.length")
        assert any_tagged == 0


@_skip_no_browser
@pytest.mark.asyncio
async def test_verify_accepts_short_normalized_committed_value() -> None:
    # A selection that normalizes to a short value ("New York" -> "NY") has no >=3-char token to overlap;
    # verify must still accept it on causality (value changed, suggestion list gone), not report failure.
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from skyvern.forge.taskv3.tools import _VERIFY_COMMIT_JS  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.set_content(
                '<input id="loc" value="NY">'
            )  # widget committed the short normalized value; no open list
            committed = await page.evaluate(
                _VERIFY_COMMIT_JS, {"field": "#loc", "typed": "New York", "chosen": "New York, NY, USA"}
            )
            assert committed == "NY"
            # negative: field still holds the raw typed text and the list is open (tagged present) => not committed
            await page.set_content('<input id="loc2" value="New York"><div data-tv3-sugg="1">New York, NY, USA</div>')
            not_committed = await page.evaluate(
                _VERIFY_COMMIT_JS, {"field": "#loc2", "typed": "New York", "chosen": "New York, NY, USA"}
            )
            assert not_committed == ""
        finally:
            await browser.close()


@_skip_no_browser
@pytest.mark.asyncio
async def test_autocomplete_flag_requires_real_combobox_semantics() -> None:
    # The observe hint must fire on real combobox semantics, not on a bare aria-controls (which a
    # search/filter input pointing at a results table also carries).
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from skyvern.forge.taskv3.tools import _IS_AUTOCOMPLETE_JS  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.set_content(
                '<input id="search" aria-controls="results">'
                '<input id="combo" role="combobox" aria-autocomplete="list">'
                '<input id="haspopup" aria-haspopup="listbox">'
            )
            assert await page.eval_on_selector("#search", _IS_AUTOCOMPLETE_JS) is False
            assert await page.eval_on_selector("#combo", _IS_AUTOCOMPLETE_JS) is True
            assert await page.eval_on_selector("#haspopup", _IS_AUTOCOMPLETE_JS) is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_hover_tool_dispatches_and_is_billable() -> None:
    # ActionBlocks can carry hover goals; without a hover tool a v3 action block would churn its
    # budget trying to hover with clicks.
    page = _FakePage()
    tools = build_browser_tools(_fixed_page_provider(page))
    spec = _tool(tools, "hover")
    result = await spec.handler({"selector": "#menu"})
    assert result.status == "ok"
    assert ("hover", {"selector": "#menu"}) in page.calls
    assert spec.billable is True


def test_perception_tools_are_compactable_and_actions_are_not() -> None:
    # Compaction only elides tools flagged `compactable` on the real factory output. If a rename or a
    # refactor of the flag loop in tools.py stops flagging observe/get_html, the transcript stops being
    # bounded and the token-runaway returns — this asserts the production wiring, not a hand-built spec.
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
    assert _tool(tools, "observe").compactable is True
    assert _tool(tools, "get_html").compactable is True
    assert _tool(tools, "click").compactable is False  # a page action is never elided


@pytest.mark.asyncio
async def test_preflighted_tool_resolves_page_once_per_call() -> None:
    # The preflight wrapper hands its resolved page to the handler; a preflighted call must not
    # resolve twice (each resolution is a must_get_working_page with its recovery path).
    page = _FakePage()
    calls = 0

    async def counting_provider() -> Any:
        nonlocal calls
        calls += 1
        return page

    tools = build_browser_tools(counting_provider)
    await _tool(tools, "click").handler({"selector": "#a"})
    assert calls == 1
    await _tool(tools, "click").handler({"selector": "#b"})
    assert calls == 2


_STATUS_PAGE_HTML = """
<!doctype html><html><head><title>Apply</title></head><body>
  <h1>Software Engineer</h1>
  <p>{prose}</p>
  <div id="result-panel">
    <div role="status" aria-live="polite"><h2>Success</h2><p>Your submission was received. We will contact you if there are next steps.</p></div>
  </div>
  <div role="alert">We couldn't process your request. Please try again.</div>
  <button id="toggle-no" aria-pressed="true">No</button>
  <input id="email" type="email" placeholder="Email">
</body></html>
"""


@contextlib.asynccontextmanager
async def _content_page(html: str) -> AsyncIterator[Any]:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1024, "height": 900})
            page = await context.new_page()
            await page.set_content(html)
            yield page
        finally:
            await browser.close()


async def _observe_data(page: Any) -> dict[str, Any]:
    from skyvern.forge.taskv3.tools import _OBSERVE_JS  # noqa: PLC0415

    return json.loads(await page.evaluate(_OBSERVE_JS))


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_digest_surfaces_status_alert_and_heading_text() -> None:
    # After an async submit many sites replace the form with a static outcome banner (role=status /
    # role=alert). observe lists only interactive elements, so without a text digest the outcome is
    # invisible and the agent re-submits (duplicate submissions) or falsely reports failure.
    prose = "word " * 200  # long plain prose: NOT a status source, must stay out of the digest
    async with _content_page(_STATUS_PAGE_HTML.format(prose=prose)) as page:
        data = await _observe_data(page)
        texts = data.get("text") or []
        joined = " | ".join(texts)
        assert "Your submission was received" in joined  # role=status body, not just the heading
        assert "We couldn't process your request" in joined  # role=alert
        assert "word word word word word" not in joined  # plain prose excluded
        assert sum(len(t) for t in texts) <= 900
        assert all(len(t) <= 300 for t in texts)
        # The role=status body and its inner heading must not appear twice (dedupe).
        assert joined.count("Your submission was received") == 1


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_digest_takes_heading_alone_when_parent_is_large() -> None:
    # A heading inside a large container (e.g. h1 over the whole page body) contributes only its own
    # text; pulling the parent text would re-open the context-growth problem the digest cap exists for.
    prose = "word " * 200
    async with _content_page(_STATUS_PAGE_HTML.format(prose=prose)) as page:
        data = await _observe_data(page)
        texts = data.get("text") or []
        assert any(t == "Software Engineer" for t in texts)  # h1 text alone, not the whole body


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_digest_total_cap_holds_under_adversarial_status_content() -> None:
    html = (
        "<!doctype html><html><body>"
        + "".join(f'<div role="status">{"status entry %d " % i * 30}</div>' for i in range(20))
        + "</body></html>"
    )
    async with _content_page(html) as page:
        data = await _observe_data(page)
        texts = data.get("text") or []
        assert texts  # something survived
        assert sum(len(t) for t in texts) <= 900


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_reports_aria_pressed_state() -> None:
    # Toggle buttons (aria-pressed) previously showed no state, so agents re-clicked them for turns.
    prose = "short"
    async with _content_page(_STATUS_PAGE_HTML.format(prose=prose)) as page:
        data = await _observe_data(page)
        by_sel = {e["selector"]: e for e in data["elements"]}
        assert by_sel["#toggle-no"].get("pressed") is True


@pytest.mark.asyncio
async def test_observe_renders_text_digest_and_pressed_state() -> None:
    class _DigestPage(_FakePage):
        async def evaluate(self, _js: str) -> str:
            return json.dumps(
                {
                    "url": self.url,
                    "title": "Apply",
                    "text": ["Success Your submission was received."],
                    "elements": [
                        {"i": 0, "tag": "button", "type": None, "selector": "#no", "label": "No", "pressed": True}
                    ],
                }
            )

    tools = build_browser_tools(_fixed_page_provider(_DigestPage()))
    r = await _tool(tools, "observe").handler({})
    assert r.status == "ok"
    assert "text: 'Success Your submission was received.'" in r.content
    assert "pressed=True" in r.content


@pytest.mark.asyncio
async def test_get_html_falls_back_to_outer_html_for_empty_leaf() -> None:
    # inner_html of a void/leaf element ("", e.g. <input>) used to return ok("") — no signal at all.
    # The element's own tag+attributes are the useful answer for a leaf.
    class _LeafElement(_FakeElement):
        async def inner_html(self) -> str:
            return ""

        async def evaluate(self, js: str) -> str:
            return '<input id="email" type="email">'

    page = _FakePage()
    page.element = _LeafElement()
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "get_html").handler({"selector": "#email"})
    assert r.status == "ok"
    assert r.content == '<input id="email" type="email">'


@pytest.mark.asyncio
async def test_get_html_marks_truncation_explicitly() -> None:
    class _BigElement(_FakeElement):
        async def inner_html(self) -> str:
            return "x" * 30000

    page = _FakePage()
    page.element = _BigElement()
    tools = build_browser_tools(_fixed_page_provider(page))
    r = await _tool(tools, "get_html").handler({"selector": "#big"})
    assert r.status == "ok"
    assert len(r.content) < 30000
    assert r.content.endswith("…[truncated at 20000 chars]")


@pytest.mark.asyncio
async def test_navigate_reports_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # navigate used to say "navigated to <url>" unconditionally — a 400 error page read as success,
    # masking dead asset URLs and blank shells from the model.
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)

    class _Response:
        status = 400

    class _StatusPage(_FakePage):
        async def goto(self, url: str, timeout: int | None = None, wait_until: str | None = None) -> Any:
            self.url = url
            return _Response()

    tools = build_browser_tools(_fixed_page_provider(_StatusPage()))
    r = await _tool(tools, "navigate").handler({"url": "https://example.test/apply"})
    assert r.status == "ok"
    assert "HTTP 400" in r.content


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_digest_containment_dedupe_keeps_one_banner_copy() -> None:
    # An alert's text re-surfaces inside its heading's parent container text; the digest must not
    # spend its cap twice on the same banner.
    html = """<!doctype html><html><body>
      <div id="panel"><h2>Account Access</h2><div role="alert">Access revoked — contact your administrator</div></div>
    </body></html>"""
    async with _content_page(html) as page:
        data = await _observe_data(page)
        texts = data.get("text") or []
        assert sum("Access revoked" in t for t in texts) == 1


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_survives_hostile_page_with_throwing_text_accessor() -> None:
    # Fingerprinting/prototype-patched pages can make innerText throw on nodes only the digest
    # visits (headings, live regions). A digest failure must degrade to "no digest", never take
    # down element perception with it.
    filler = "lorem ipsum " * 40  # parent text > 300 chars forces the heading-text-alone branch
    html = """<!doctype html><html><body>
      <h1 id="poisoned">Title</h1>
      <p>FILLER</p>
      <input id="email" type="email" placeholder="Email">
      <script>
        Object.defineProperty(document.getElementById('poisoned'), 'innerText',
          { get() { throw new Error('boom from poisoned innerText'); } });
      </script>
    </body></html>""".replace("FILLER", filler)
    async with _content_page(html) as page:
        data = await _observe_data(page)
        selectors = [e["selector"] for e in data["elements"]]
        assert "#email" in selectors  # element perception intact despite the poisoned digest source


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_surfaces_cross_origin_iframe_presence() -> None:
    # An anti-bot widget rendered in a cross-origin iframe gates submission on many sites, but
    # element perception is main-frame only — without a presence record the model can neither see
    # nor reason about the gate. Attributes only (host + signature); never the frame's document.
    html = """<!doctype html><html><body>
      <input id="email" type="email" placeholder="Email">
      <iframe src="https://challenges.antibot-vendor.test/turnstile/anchor?k=secret123"
              title="Widget containing a security challenge" width="300" height="65"></iframe>
      <iframe src="https://tracker.analytics.test/pixel" style="display:none"></iframe>
      <iframe srcdoc="<p>same-origin help panel</p>" src="https://legacy.fallback.test/x"
              width="200" height="50"></iframe>
      <button id="submit">Submit application</button>
    </body></html>"""
    async with _content_page(html) as page:
        data = await _observe_data(page)
        info = data.get("iframes") or {}
        entries = info.get("entries") or []
        assert info.get("total") == 1  # hidden and srcdoc frames (even with a src fallback) are excluded
        assert len(entries) == 1
        assert entries[0]["host"] == "challenges.antibot-vendor.test"
        assert entries[0]["captcha"] is True
        assert "secret123" not in json.dumps(entries)  # hosts only — no full URLs/query strings


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_iframe_summary_is_bounded() -> None:
    # A frame-heavy page (embeds, ad slots) must not regrow the context the digest work bounded:
    # detail is capped while the total count stays honest.
    frames = "\n".join(
        f'<iframe src="https://embed{i}.media.test/player" width="100" height="40"></iframe>' for i in range(12)
    )
    async with _content_page(f"<!doctype html><html><body>{frames}</body></html>") as page:
        data = await _observe_data(page)
        info = data.get("iframes") or {}
        assert info.get("total") == 12
        assert len(info.get("entries") or []) <= 8


@pytest.mark.asyncio
async def test_observe_renders_cross_origin_iframe_presence_line() -> None:
    # The rendered line must carry the captcha flag and say the frame's contents are unreachable,
    # so the model doesn't hunt for the widget's elements with selectors that can never resolve.
    class _IframePage(_FakePage):
        async def evaluate(self, _js: str) -> str:
            return json.dumps(
                {
                    "url": self.url,
                    "title": "Apply",
                    "text": [],
                    "iframes": {
                        "total": 9,
                        "entries": [
                            {"host": "challenges.antibot-vendor.test", "title": "Security challenge", "captcha": True},
                            {"host": "embed.media.test", "title": "", "captcha": False},
                        ],
                    },
                    "elements": [],
                }
            )

    tools = build_browser_tools(_fixed_page_provider(_IframePage()))
    r = await _tool(tools, "observe").handler({})
    assert r.status == "ok"
    assert "9 cross-origin" in r.content
    assert "[captcha] challenges.antibot-vendor.test" in r.content
    assert "embed.media.test" in r.content
    assert "+7 more" in r.content


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_iframe_scan_survives_poisoned_frame_and_renders_end_to_end() -> None:
    # A hostile page can poison a single frame's accessors; the iframe scan must degrade to "no
    # iframe report" without taking element perception down (same isolation contract as the digest).
    # Also drives a healthy page through the REAL observe() handler — JS shape and Python renderer
    # are otherwise only ever tested against each other's hand-written stand-ins.
    poisoned = """<!doctype html><html><body>
      <input id="email" type="email" placeholder="Email">
      <iframe id="bad" src="https://challenges.antibot-vendor.test/anchor" width="100" height="40"></iframe>
      <script>
        Object.defineProperty(document.getElementById('bad'), 'getBoundingClientRect',
          { get() { throw new Error('boom from poisoned frame'); } });
      </script>
    </body></html>"""
    async with _content_page(poisoned) as page:
        data = await _observe_data(page)
        assert "#email" in [e["selector"] for e in data["elements"]]  # element perception intact
        info = data.get("iframes") or {}
        assert info.get("total") == 0 and not info.get("entries")  # degraded, not crashed
    healthy = """<!doctype html><html><body>
      <input id="email" type="email" placeholder="Email">
      <iframe src="https://challenges.antibot-vendor.test/turnstile/anchor"
              title="Security challenge" width="300" height="65"></iframe>
    </body></html>"""
    async with _content_page(healthy) as page:

        async def _provider() -> Any:
            return page

        r = await _tool(build_browser_tools(_provider), "observe").handler({})
        assert r.status == "ok"
        assert "iframes: 1 cross-origin" in r.content
        assert "[captcha] challenges.antibot-vendor.test" in r.content


@pytest.mark.asyncio
async def test_observe_omits_iframe_line_when_no_cross_origin_iframes() -> None:
    # The common case (no cross-origin frames) must add zero output — not an empty line on every
    # observe of every run.
    tools = build_browser_tools(_fixed_page_provider(_FakePage()))
    r = await _tool(tools, "observe").handler({})
    assert r.status == "ok"
    assert "iframes:" not in r.content


@_skip_no_browser
@pytest.mark.asyncio
async def test_observe_digest_superset_replaces_terse_contained_entry() -> None:
    # A terse live-region entry collected first ("Saved") must not suppress a later, richer
    # superset ("Saved — confirmation #A1B2") — the superset replaces it.
    html = """<!doctype html><html><body>
      <div role="status">Saved</div>
      <div><h2>Saved</h2><p>— confirmation #A1B2</p></div>
    </body></html>"""
    async with _content_page(html) as page:
        data = await _observe_data(page)
        texts = data.get("text") or []
        assert any("confirmation #A1B2" in t for t in texts)
        assert "Saved" not in texts  # the bare terse entry was replaced, not kept alongside
