"""Unit tests for the Task V3 raw-browser tools.

A fake Playwright page records calls so we can assert each tool dispatches raw browser
operations (no task-ecosystem) with the right args, without a live browser.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from skyvern.forge.taskv3.tools import build_browser_tools


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
async def test_click_type_select_dispatch_raw_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)  # the typeahead probe polls; skip real waiting in tests
    page = _FakePage()
    tools = build_browser_tools(page)
    await _tool(tools, "click").handler({"selector": "#submit"})
    await _tool(tools, "type").handler({"selector": "#first", "text": "John"})
    await _tool(tools, "select_option").handler({"selector": "#country", "label": "United States"})
    assert ("click", {"selector": "#submit"}) in page.calls
    # `type` keystroke-types the value (typeahead-safe path) — the value is entered via page.type
    assert ("type", {"selector": "#first", "text": "John"}) in page.calls
    assert ("select_option", {"selector": "#country", "value": None, "label": "United States"}) in page.calls


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

    assert PREFLIGHT_TOOL_NAMES == {
        "click",
        "type",
        "select_combobox",
        "select_option",
        "press_key",
        "file_upload",
        "navigate",
    }
    rep = {
        "click": {"selector": "#x"},
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
    tools = build_browser_tools(page)
    await _tool(tools, "navigate").handler({"url": "https://jobs.example.test/acme/1"})

    assert len(seen) == 1
    action, site = seen[0]
    assert site == "taskv3-navigate"
    assert action.url == "https://jobs.example.test/acme/1"  # PAGE target the origin check can act on


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
    tools = build_browser_tools(page)
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
    tools = build_browser_tools(page)
    r = await _tool(tools, "type").handler({"selector": "#notes", "text": "Springfield"})
    assert r.status == "ok"
    assert ("type", ("#notes", "Springfield")) in page.calls  # keystroke-typed; raw text left in place
    assert not page.clicked_suggestion


@pytest.mark.asyncio
async def test_type_non_text_input_skips_probe_fast_path() -> None:
    # A non-text input (email/tel/…) is never a typeahead: fast fill, no suggestion probe, no click —
    # even if a suggestion would have been offered.
    page = _TypeaheadFakePage(field_type="email", suggestion={"text": "x@y.com", "score": 9})
    tools = build_browser_tools(page)
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
    tools = build_browser_tools(page)
    r = await _tool(tools, "select_combobox").handler({"selector": "#loc", "value": "San Francisco, California"})
    assert r.status == "ok" and "San Francisco, CA, USA" in r.content
    assert page.clicked_suggestion


@pytest.mark.asyncio
async def test_select_combobox_fails_loud_when_no_suggestion_reacts(monkeypatch: pytest.MonkeyPatch) -> None:
    # No suggestion overlaps the value => no false "filled": select_combobox must error, never claim success.
    import asyncio as _a

    monkeypatch.setattr(_a, "sleep", _instant_sleep)
    page = _TypeaheadFakePage(field_type="text", suggestion=None)
    tools = build_browser_tools(page)
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
