"""Tests for skyvern_observe and skyvern_execute MCP tools."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import (
    ExecuteStep,
    ObserveResult,
    _flatten_a11y_tree,
    _is_password_field,
    do_execute,
    do_observe,
    ref_to_selector,
    serialize_elements,
)
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_a11y_tree(**overrides: Any) -> dict[str, Any]:
    """Build a minimal a11y tree for testing."""
    tree: dict[str, Any] = {
        "role": "WebArea",
        "name": "",
        "children": overrides.get(
            "children",
            [
                {"role": "textbox", "name": "Email", "value": ""},
                {"role": "textbox", "name": "Password", "value": "secret123"},
                {"role": "button", "name": "Sign In"},
                {"role": "link", "name": "Forgot password?"},
                {"role": "heading", "name": "Login", "level": 1},
            ],
        ),
    }
    return tree


def _make_page(a11y_tree: dict[str, Any] | None = None) -> AsyncMock:
    """Create a mock page with accessibility.snapshot()."""
    page = AsyncMock()
    page.url = "https://example.com/login"
    page.title = AsyncMock(return_value="Login Page")

    tree = a11y_tree or _make_a11y_tree()
    page.accessibility = SimpleNamespace(snapshot=AsyncMock(return_value=tree))
    page.locator = AsyncMock()
    return page


# ---------------------------------------------------------------------------
# Unit tests: _flatten_a11y_tree
# ---------------------------------------------------------------------------


class TestFlattenA11yTree:
    def test_empty_tree(self) -> None:
        assert _flatten_a11y_tree(None) == []

    def test_skips_web_area_root(self) -> None:
        tree = {"role": "WebArea", "name": "", "children": []}
        assert _flatten_a11y_tree(tree) == []

    def test_flattens_nested(self) -> None:
        tree = {
            "role": "WebArea",
            "name": "",
            "children": [
                {
                    "role": "navigation",
                    "name": "Main",
                    "children": [
                        {"role": "link", "name": "Home"},
                        {"role": "link", "name": "About"},
                    ],
                },
                {"role": "button", "name": "Submit"},
            ],
        }
        flat = _flatten_a11y_tree(tree)
        roles = [e["role"] for e in flat]
        assert roles == ["navigation", "link", "link", "button"]

    def test_no_children_key(self) -> None:
        tree = {"role": "button", "name": "Click me"}
        flat = _flatten_a11y_tree(tree)
        assert len(flat) == 1
        assert flat[0]["name"] == "Click me"


# ---------------------------------------------------------------------------
# Unit tests: _is_password_field
# ---------------------------------------------------------------------------


class TestIsPasswordField:
    def test_password_name(self) -> None:
        assert _is_password_field("textbox", "Password") is True

    def test_passphrase_name(self) -> None:
        assert _is_password_field("textbox", "Enter your passphrase") is True

    def test_secret_name(self) -> None:
        assert _is_password_field("textbox", "API Secret") is True

    def test_token_name(self) -> None:
        assert _is_password_field("textbox", "Auth Token") is True

    def test_non_password(self) -> None:
        assert _is_password_field("textbox", "Email") is False

    def test_button_with_password_name(self) -> None:
        # buttons named "password" still match the regex
        assert _is_password_field("button", "Show Password") is True


# ---------------------------------------------------------------------------
# Unit tests: ref_to_selector
# ---------------------------------------------------------------------------


class TestRefToSelector:
    def test_with_name(self) -> None:
        assert ref_to_selector({"role": "button", "name": "Submit"}) == 'role=button[name="Submit"]'

    def test_without_name(self) -> None:
        assert ref_to_selector({"role": "textbox", "name": ""}) == "role=textbox"

    def test_name_with_quotes(self) -> None:
        result = ref_to_selector({"role": "button", "name": 'Click "here"'})
        assert result == 'role=button[name="Click \\"here\\""]'

    def test_match_index_zero_emits_nth(self) -> None:
        """Presence of match_index in the dict signals a duplicate group; emit nth even for 0."""
        elem = {"role": "combobox", "name": "", "match_index": 0}
        assert ref_to_selector(elem) == "role=combobox >> nth=0"

    def test_unnamed_duplicate_appends_nth(self) -> None:
        elem = {"role": "combobox", "name": "", "match_index": 2}
        assert ref_to_selector(elem) == "role=combobox >> nth=2"

    def test_named_duplicate_appends_nth(self) -> None:
        elem = {"role": "button", "name": "Edit", "match_index": 1}
        assert ref_to_selector(elem) == 'role=button[name="Edit"] >> nth=1'

    def test_missing_match_index_is_backward_compatible(self) -> None:
        assert ref_to_selector({"role": "textbox", "name": "Email"}) == 'role=textbox[name="Email"]'


# ---------------------------------------------------------------------------
# Unit tests: do_observe
# ---------------------------------------------------------------------------


class TestDoObserve:
    @pytest.mark.asyncio
    async def test_basic_observe(self) -> None:
        page = _make_page()
        result = await do_observe(page)

        assert isinstance(result, ObserveResult)
        assert result.url == "https://example.com/login"
        assert result.title == "Login Page"
        # Default interactive_only=True filters out heading
        assert result.element_count == 4
        assert result.total_on_page == 4

    @pytest.mark.asyncio
    async def test_ref_assignment(self) -> None:
        page = _make_page()
        result = await do_observe(page)
        refs = [e.ref for e in result.elements]
        assert refs == ["e0", "e1", "e2", "e3"]

    @pytest.mark.asyncio
    async def test_password_redaction(self) -> None:
        """DESIGN-2: Password field values must be redacted."""
        page = _make_page()
        result = await do_observe(page)
        password_elem = next(e for e in result.elements if e.name == "Password")
        assert password_elem.value == "***"

    @pytest.mark.asyncio
    async def test_non_password_value_preserved(self) -> None:
        page = _make_page()
        result = await do_observe(page)
        email_elem = next(e for e in result.elements if e.name == "Email")
        assert email_elem.value == ""

    @pytest.mark.asyncio
    async def test_max_elements_cap(self) -> None:
        children = [{"role": "button", "name": f"Btn {i}"} for i in range(100)]
        page = _make_page(_make_a11y_tree(children=children))
        result = await do_observe(page, max_elements=10)
        assert result.element_count == 10
        assert result.total_on_page == 100

    @pytest.mark.asyncio
    async def test_interactive_only_false(self) -> None:
        page = _make_page()
        result = await do_observe(page, interactive_only=False)
        # Should include heading (non-interactive)
        assert result.element_count == 5

    @pytest.mark.asyncio
    async def test_selector_scoping(self) -> None:
        page = _make_page()
        mock_handle = AsyncMock()
        # locator() is synchronous in Playwright, returns a Locator
        locator_mock = MagicMock()
        locator_mock.first.element_handle = AsyncMock(return_value=mock_handle)
        page.locator = MagicMock(return_value=locator_mock)

        scoped_tree = {
            "role": "group",
            "name": "form",
            "children": [{"role": "textbox", "name": "Name"}],
        }
        page.accessibility.snapshot = AsyncMock(return_value=scoped_tree)

        result = await do_observe(page, selector="form#login")
        page.accessibility.snapshot.assert_awaited_once_with(root=mock_handle)
        assert result.element_count == 1

    @pytest.mark.asyncio
    async def test_combobox_options(self) -> None:
        tree = _make_a11y_tree(
            children=[
                {
                    "role": "combobox",
                    "name": "Country",
                    "children": [
                        {"role": "option", "name": "US"},
                        {"role": "option", "name": "UK"},
                        {"role": "option", "name": "CA"},
                    ],
                },
            ]
        )
        page = _make_page(tree)
        result = await do_observe(page)
        assert result.elements[0].options == ["US", "UK", "CA"]

    @pytest.mark.asyncio
    async def test_role_to_tag_mapping(self) -> None:
        page = _make_page()
        result = await do_observe(page)
        tags = {e.name: e.tag for e in result.elements}
        assert tags["Email"] == "input"
        assert tags["Sign In"] == "button"
        assert tags["Forgot password?"] == "a"

    @pytest.mark.asyncio
    async def test_unique_elements_have_match_index_zero(self) -> None:
        page = _make_page()
        result = await do_observe(page)
        assert all(e.match_index == 0 for e in result.elements)

    @pytest.mark.asyncio
    async def test_unnamed_duplicates_get_distinct_match_indices(self) -> None:
        """SKY-9701: multiple unnamed widgets of the same role must each get a unique index."""
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
            ]
        )
        page = _make_page(tree)
        result = await do_observe(page)
        assert [e.match_index for e in result.elements] == [0, 1, 2, 3]

    @pytest.mark.asyncio
    async def test_named_duplicates_get_distinct_match_indices(self) -> None:
        """Elements sharing both role and name also collide; disambiguate by index."""
        tree = _make_a11y_tree(
            children=[
                {"role": "button", "name": "Edit"},
                {"role": "button", "name": "Delete"},
                {"role": "button", "name": "Edit"},
            ]
        )
        page = _make_page(tree)
        result = await do_observe(page)
        grouped = [(e.role, e.name, e.match_index) for e in result.elements]
        assert grouped == [
            ("button", "Edit", 0),
            ("button", "Delete", 0),
            ("button", "Edit", 1),
        ]

    @pytest.mark.asyncio
    async def test_serialized_output_includes_match_index_for_every_member_of_duplicate_group(self) -> None:
        """Every member of a multi-element (role, name) group carries match_index, including the first."""
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
            ]
        )
        page = _make_page(tree)
        result = await do_observe(page)
        serialized = serialize_elements(result.elements)
        assert serialized[0]["match_index"] == 0
        assert serialized[1]["match_index"] == 1

    @pytest.mark.asyncio
    async def test_serialized_output_omits_match_index_for_unique_elements(self) -> None:
        page = _make_page()
        result = await do_observe(page)
        serialized = serialize_elements(result.elements)
        assert all("match_index" not in d for d in serialized)

    @pytest.mark.asyncio
    async def test_disambiguation_counts_collisions_beyond_max_elements_cap(self) -> None:
        """SKY-9701 follow-up: duplicates beyond the cap must still trigger disambiguation
        for kept refs, otherwise selector-only clicks on capped duplicates fall back to the
        ambiguous base selector."""
        children = [{"role": "combobox", "name": ""} for _ in range(6)]
        page = _make_page(_make_a11y_tree(children=children))
        result = await do_observe(page, max_elements=2)
        assert result.element_count == 2
        assert result.total_on_page == 6
        serialized = serialize_elements(result.elements)
        # Both kept refs must carry match_index so the selector emits nth=N,
        # even though only 2 of 6 are returned.
        assert serialized[0]["match_index"] == 0
        assert serialized[1]["match_index"] == 1
        selectors = [ref_to_selector(elem) for elem in serialized]
        assert selectors == ["role=combobox >> nth=0", "role=combobox >> nth=1"]

    @pytest.mark.asyncio
    async def test_unnamed_duplicate_resolves_to_distinct_selectors(self) -> None:
        """End-to-end: observe -> serialize -> ref_to_selector produces unique selectors for every ref, including e0."""
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
            ]
        )
        page = _make_page(tree)
        result = await do_observe(page)
        serialized = serialize_elements(result.elements)
        selectors = [ref_to_selector(elem) for elem in serialized]
        assert selectors == [
            "role=combobox >> nth=0",
            "role=combobox >> nth=1",
            "role=combobox >> nth=2",
            "role=combobox >> nth=3",
        ]


# ---------------------------------------------------------------------------
# Unit tests: do_execute
# ---------------------------------------------------------------------------


class TestDoExecute:
    @pytest.mark.asyncio
    async def test_basic_batch(self) -> None:
        call_log: list[str] = []

        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            call_log.append(step.tool)
            return {"status": "ok"}

        steps = [
            ExecuteStep(tool="navigate", params={"url": "https://example.com"}),
            ExecuteStep(tool="click", params={"selector": "#btn"}),
        ]
        result = await do_execute(dispatch, steps)

        assert result.steps_completed == 2
        assert result.steps_total == 2
        assert result.error_step is None
        assert call_log == ["navigate", "click"]

    @pytest.mark.asyncio
    async def test_stop_on_error_true(self) -> None:
        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            if step.tool == "click":
                raise RuntimeError("Element not found")
            return None

        steps = [
            ExecuteStep(tool="navigate", params={}),
            ExecuteStep(tool="click", params={}),
            ExecuteStep(tool="type", params={}),
        ]
        result = await do_execute(dispatch, steps, stop_on_error=True)

        assert result.steps_completed == 2
        assert result.error_step == 1
        assert result.results[1].ok is False
        assert "Element not found" in (result.results[1].error or "")

    @pytest.mark.asyncio
    async def test_stop_on_error_false_continues(self) -> None:
        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            if step.tool == "click":
                raise RuntimeError("fail")
            return None

        steps = [
            ExecuteStep(tool="click", params={}),
            ExecuteStep(tool="scroll", params={}),
        ]
        result = await do_execute(dispatch, steps, stop_on_error=False)

        assert result.steps_completed == 2
        assert result.results[0].ok is False
        assert result.results[1].ok is True

    @pytest.mark.asyncio
    async def test_design_3_blocks_sensitive_after_failed_nav(self) -> None:
        """DESIGN-3: type and evaluate are blocked after failed navigate."""

        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            if step.tool == "navigate":
                raise RuntimeError("DNS resolution failed")
            return None

        steps = [
            ExecuteStep(tool="navigate", params={}),
            ExecuteStep(tool="type", params={}),
            ExecuteStep(tool="click", params={}),
            ExecuteStep(tool="evaluate", params={}),
        ]
        result = await do_execute(dispatch, steps, stop_on_error=False)

        assert result.steps_completed == 4
        # navigate failed
        assert result.results[0].ok is False
        # "type" tool blocked (sensitive)
        assert result.results[1].ok is False
        assert "blocked_by_failed_navigate" in (result.results[1].error or "")
        # click allowed (non-sensitive)
        assert result.results[2].ok is True
        # evaluate blocked (sensitive)
        assert result.results[3].ok is False
        assert "blocked_by_failed_navigate" in (result.results[3].error or "")

    @pytest.mark.asyncio
    async def test_design_3_not_triggered_with_stop_on_error(self) -> None:
        """DESIGN-3 only applies when stop_on_error=false."""

        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            if step.tool == "navigate":
                raise RuntimeError("fail")
            return None

        steps = [
            ExecuteStep(tool="navigate", params={}),
            ExecuteStep(tool="type", params={}),
        ]
        result = await do_execute(dispatch, steps, stop_on_error=True)
        # Stops at navigate, never reaches type
        assert result.steps_completed == 1

    @pytest.mark.asyncio
    async def test_design_4_ref_map_replaced_on_observe(self) -> None:
        """DESIGN-4: Each observe replaces the entire ref_map."""
        ref_maps_seen: list[dict] = []

        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            ref_maps_seen.append(dict(ref_map))
            if step.tool == "observe":
                return {
                    "elements": [
                        {"ref": "e0", "role": "button", "name": f"Btn-{step.params.get('call', 0)}"},
                    ],
                    "element_count": 1,
                    "total_on_page": 1,
                }
            return None

        steps = [
            ExecuteStep(tool="observe", params={"call": 1}),
            ExecuteStep(tool="click", params={}),
            ExecuteStep(tool="observe", params={"call": 2}),
            ExecuteStep(tool="click", params={}),
        ]
        result = await do_execute(dispatch, steps)

        assert result.steps_completed == 4
        # After first observe, ref_map has Btn-1
        assert ref_maps_seen[1].get("e0", {}).get("name") == "Btn-1"
        # After second observe, ref_map replaced with Btn-2
        assert ref_maps_seen[3].get("e0", {}).get("name") == "Btn-2"

    @pytest.mark.asyncio
    async def test_empty_steps(self) -> None:
        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            return None

        result = await do_execute(dispatch, [])
        assert result.steps_completed == 0
        assert result.error_step is None


# ---------------------------------------------------------------------------
# MCP tool tests: skyvern_observe
# ---------------------------------------------------------------------------


class TestSkyvernObserveMCP:
    @pytest.mark.asyncio
    async def test_observe_returns_elements(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        result = await mcp_browser.skyvern_observe()

        assert result["ok"] is True
        assert len(result["data"]["elements"]) == 4
        assert result["data"]["element_count"] == 4

    @pytest.mark.asyncio
    async def test_observe_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError("no browser")))

        result = await mcp_browser.skyvern_observe()
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# MCP tool tests: skyvern_execute
# ---------------------------------------------------------------------------


class TestSkyvernExecuteMCP:
    @pytest.mark.asyncio
    async def test_execute_empty_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await mcp_browser.skyvern_execute(steps=[])
        assert result["ok"] is True
        assert result["data"]["steps_completed"] == 0

    @pytest.mark.asyncio
    async def test_execute_too_many_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [{"tool": "click", "params": {}} for _ in range(25)]
        result = await mcp_browser.skyvern_execute(steps=steps)
        assert result["ok"] is False
        assert "Too many steps" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_execute_invalid_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await mcp_browser.skyvern_execute(steps=[{"tool": "act", "params": {}}])
        assert result["ok"] is False
        assert "unknown tool" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_execute_missing_tool_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await mcp_browser.skyvern_execute(steps=[{"params": {}}])
        assert result["ok"] is False
        assert "missing 'tool'" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_execute_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError("no browser")))

        result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"selector": "#btn"}}])
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_execute_dispatch_calls_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": {"resolved_selector": "#btn"}}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"selector": "#btn"}}])
        assert result["ok"] is True
        assert result["data"]["steps_completed"] == 1
        mcp_browser.skyvern_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_observe_then_click_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Integration: observe provides refs, click uses them."""
        page = _make_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        result = await mcp_browser.skyvern_execute(
            steps=[
                {"tool": "observe", "params": {}},
                {"tool": "click", "params": {"ref": "e2"}},  # e2 = Sign In button
            ]
        )
        assert result["ok"] is True
        assert result["data"]["steps_completed"] == 2

        # Verify click was called with selector resolved from ref
        click_call = mcp_browser.skyvern_click.call_args
        assert 'role=button[name="Sign In"]' in str(click_call)

    @pytest.mark.asyncio
    async def test_execute_unknown_ref_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"ref": "e99"}}])
        assert result["ok"] is False
        assert "unknown ref" in result["data"]["results"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_ref_to_unnamed_duplicate_uses_nth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SKY-9701: clicking ref=e2 on a page with 4 unnamed comboboxes resolves to the 3rd, not the 1st."""
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
            ]
        )
        page = _make_page(tree)
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        result = await mcp_browser.skyvern_execute(
            steps=[
                {"tool": "observe", "params": {}},
                {"tool": "click", "params": {"ref": "e2"}},
            ]
        )
        assert result["ok"] is True
        click_kwargs = mcp_browser.skyvern_click.call_args.kwargs
        assert click_kwargs["selector"] == "role=combobox >> nth=2"

    @pytest.mark.asyncio
    async def test_execute_ref_e0_on_unnamed_duplicate_emits_nth_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SKY-9701 follow-up: the first duplicate (e0) must also emit `nth=0` so Playwright
        strict mode does not raise and the click does not silently resolve to whichever
        element happens to be first."""
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": ""},
                {"role": "combobox", "name": ""},
            ]
        )
        page = _make_page(tree)
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        result = await mcp_browser.skyvern_execute(
            steps=[
                {"tool": "observe", "params": {}},
                {"tool": "click", "params": {"ref": "e0"}},
            ]
        )
        assert result["ok"] is True
        click_kwargs = mcp_browser.skyvern_click.call_args.kwargs
        assert click_kwargs["selector"] == "role=combobox >> nth=0"
