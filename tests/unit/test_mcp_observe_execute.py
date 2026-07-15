"""Tests for skyvern_observe and skyvern_execute MCP tools."""

from __future__ import annotations

import gc
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import browser_ops, session_manager
from skyvern.cli.core.browser_ops import (
    _OBSERVE_INTERACTABLES_JS,
    ExecuteStep,
    ObserveResult,
    _flatten_a11y_tree,
    do_execute,
    do_observe,
    ref_to_selector,
    serialize_elements,
)
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import scoped_session
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import tabs as mcp_tabs
from skyvern.client.errors import InternalServerError
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage
from tests.unit._mcp_browser_fakes import make_real_wait_for_timeout, make_session_state

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
    page._working_frame = None
    page.url = "https://example.com/login"
    page.title = AsyncMock(return_value="Login Page")

    tree = a11y_tree or _make_a11y_tree()
    page.accessibility = SimpleNamespace(snapshot=AsyncMock(return_value=tree))
    page.locator = AsyncMock()
    return page


def _make_values_page() -> AsyncMock:
    """Page with a non-password input (DOM value present) and a password input (DOM value absent)."""
    tree = _make_a11y_tree(
        children=[
            {"role": "textbox", "name": "Email", "value": "snapshot@example.com"},
            {"role": "textbox", "name": "Current", "value": "snapshot-secret"},
        ]
    )
    page = _make_page(tree)
    page.evaluate = AsyncMock(
        return_value=[
            {
                "role": "textbox",
                "name": "Email",
                "tag": "input",
                "selector": "#email",
                "value": "person@example.com",
            },
            {
                "role": "textbox",
                "name": "Current",
                "tag": "input",
                "selector": "#password",
            },
        ]
    )
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
    async def test_default_omits_values(self) -> None:
        page = _make_page()
        observed = serialize_elements((await do_observe(page)).elements)

        assert all("value" not in element for element in observed)

    @pytest.mark.asyncio
    async def test_include_values_keeps_non_password_dom_value_only(self) -> None:
        page = _make_values_page()

        observed = serialize_elements((await do_observe(page, include_values=True)).elements)

        email = next(element for element in observed if element["name"] == "Email")
        password = next(element for element in observed if element["name"] == "Current")
        assert email["value"] == "person@example.com"
        assert "value" not in password

    @pytest.mark.asyncio
    async def test_non_boolean_include_values_fails_closed(self) -> None:
        page = _make_values_page()

        await do_observe(page, include_values="false")  # type: ignore[arg-type]

        page.evaluate.assert_awaited_once_with(
            _OBSERVE_INTERACTABLES_JS,
            {"scopeSelector": None, "includeValues": False},
        )

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
    @pytest.mark.parametrize("selector", [None, "#frame-form"])
    async def test_working_frame_uses_dom_only_and_reports_frame_url(
        self,
        selector: str | None,
    ) -> None:
        main_tree = _make_a11y_tree(children=[{"role": "button", "name": "Parent action"}])
        page = _make_page(main_tree)
        page.evaluate = AsyncMock(
            return_value=[
                {
                    "role": "button",
                    "name": "Parent action",
                    "tag": "button",
                    "selector": "#parent-action",
                }
            ]
        )
        locator = MagicMock()
        locator.first.element_handle = AsyncMock(return_value=MagicMock())
        page.locator = MagicMock(return_value=locator)

        frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            title=AsyncMock(return_value="Payment Frame"),
            evaluate=AsyncMock(
                return_value=[
                    {
                        "role": "button",
                        "name": "Frame action",
                        "tag": "button",
                        "selector": "#frame-action",
                    }
                ]
            ),
        )
        page._working_frame = frame

        result = await do_observe(page, selector=selector)

        frame.evaluate.assert_awaited_once_with(
            _OBSERVE_INTERACTABLES_JS,
            {"scopeSelector": selector, "includeValues": False},
        )
        page.evaluate.assert_not_awaited()
        page.accessibility.snapshot.assert_not_awaited()
        assert {element.name for element in result.elements} == {"Frame action"}
        assert result.url == frame.url
        assert result.title == "Payment Frame"
        frame.title.assert_awaited_once()
        page.title.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_detached_working_frame_raises_before_evaluate(self) -> None:
        page = _make_page()
        frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            is_detached=lambda: True,
            evaluate=AsyncMock(),
        )
        page._working_frame = frame

        with pytest.raises(browser_ops.ObserveFrameError) as exc_info:
            await do_observe(page)

        assert exc_info.value.frame_name == frame.name
        assert exc_info.value.frame_url == frame.url
        frame.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_frame_switch_index_zero_uses_main_frame_accessibility_snapshot(self) -> None:
        main_frame = SimpleNamespace(name="", url="https://example.com/login")
        raw_page = SimpleNamespace(frames=[main_frame], main_frame=main_frame)
        snapshot = AsyncMock(return_value=_make_a11y_tree(children=[{"role": "button", "name": "Main"}]))
        page = SimpleNamespace(
            page=raw_page,
            _working_frame=SimpleNamespace(),
            url=main_frame.url,
            title=AsyncMock(return_value="Main Page"),
            accessibility=SimpleNamespace(snapshot=snapshot),
            evaluate=AsyncMock(return_value=[]),
        )

        switched = await SkyvernBrowserPage.frame_switch(page, index=0)
        observed = await do_observe(page)

        assert switched["url"] == main_frame.url
        assert page._working_frame is None
        snapshot.assert_awaited_once_with()
        assert {element.name for element in observed.elements} == {"Main"}

    @pytest.mark.asyncio
    async def test_working_frame_evaluate_failure_raises_typed_error(self) -> None:
        page = _make_page()
        page.evaluate = AsyncMock(return_value=[])
        page.accessibility = None
        page._working_frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            evaluate=AsyncMock(side_effect=RuntimeError("Execution context was destroyed")),
        )

        with pytest.raises(browser_ops.ObserveFrameError) as exc_info:
            await do_observe(page)

        error = str(exc_info.value)
        assert page._working_frame.name in error

    @pytest.mark.asyncio
    async def test_working_frame_on_different_page_raises_typed_error(self) -> None:
        page = _make_page()
        page.page = object()
        page._working_frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            page=object(),
            evaluate=AsyncMock(return_value=[]),
        )

        with pytest.raises(browser_ops.ObserveFrameError) as exc_info:
            await do_observe(page)

        assert "different page" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_main_frame_evaluate_failure_remains_best_effort(self) -> None:
        page = _make_page()
        page.evaluate = AsyncMock(side_effect=RuntimeError("Execution context was destroyed"))

        result = await do_observe(page)

        assert "Sign In" in {element.name for element in result.elements}

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
        country = serialize_elements(result.elements)[0]
        assert country["options"] == ["US", "UK", "CA"]
        assert "value" not in country

    @pytest.mark.asyncio
    async def test_dom_interactables_are_merged_with_selectors(self) -> None:
        page = _make_page(_make_a11y_tree(children=[{"role": "textbox", "name": "City", "value": ""}]))
        page.evaluate = AsyncMock(
            return_value=[
                {
                    "role": "option",
                    "name": "Lakewood",
                    "tag": "div",
                    "selector": "#city-list > div:nth-of-type(1)",
                }
            ]
        )

        result = await do_observe(page)
        serialized = serialize_elements(result.elements)

        city_option = next(element for element in serialized if element["name"] == "Lakewood")
        assert city_option["selector"] == "#city-list > div:nth-of-type(1)"
        assert ref_to_selector(city_option) == "#city-list > div:nth-of-type(1)"

    @pytest.mark.asyncio
    async def test_dom_interactables_work_without_accessibility_snapshot(self) -> None:
        page = SimpleNamespace(
            url="https://example.com/form",
            title=AsyncMock(return_value="Form"),
            evaluate=AsyncMock(
                return_value=[
                    {
                        "role": "option",
                        "name": "Music",
                        "tag": "div",
                        "selector": "#category-options > div:nth-of-type(2)",
                    }
                ]
            ),
        )

        result = await do_observe(page)
        serialized = serialize_elements(result.elements)

        assert len(serialized) == 1
        assert serialized[0]["name"] == "Music"
        assert serialized[0]["selector"] == "#category-options > div:nth-of-type(2)"

    @pytest.mark.asyncio
    async def test_dom_selector_is_attached_to_matching_a11y_option(self) -> None:
        tree = _make_a11y_tree(
            children=[
                {
                    "role": "combobox",
                    "name": "Region",
                    "children": [
                        {"role": "option", "name": "North"},
                        {"role": "option", "name": "East"},
                    ],
                },
            ]
        )
        page = _make_page(tree)
        page.evaluate = AsyncMock(
            return_value=[
                {
                    "role": "option",
                    "name": "East",
                    "tag": "option",
                    "selector": "#region > option:nth-of-type(2)",
                }
            ]
        )

        result = await do_observe(page)
        serialized = serialize_elements(result.elements)

        east_option = next(element for element in serialized if element["name"] == "East")
        assert east_option["selector"] == "#region > option:nth-of-type(2)"
        assert "value" not in east_option
        assert ref_to_selector(east_option) == "#region > option:nth-of-type(2)"

    @pytest.mark.asyncio
    async def test_duplicate_label_options_do_not_get_wrong_selector(self) -> None:
        # Two a11y options share a label and two DOM options share it too. The merge must NOT
        # attach a DOM selector to an a11y option (ambiguous) — that would risk a confidently
        # wrong deterministic action.
        tree = _make_a11y_tree(
            children=[
                {
                    "role": "combobox",
                    "name": "Region",
                    "children": [
                        {"role": "option", "name": "North"},
                        {"role": "option", "name": "North"},
                    ],
                },
            ]
        )
        page = _make_page(tree)
        page.evaluate = AsyncMock(
            return_value=[
                {
                    "role": "option",
                    "name": "North",
                    "tag": "option",
                    "selector": "#r > option:nth-of-type(1)",
                },
                {
                    "role": "option",
                    "name": "North",
                    "tag": "option",
                    "selector": "#r > option:nth-of-type(2)",
                },
            ]
        )
        result = await do_observe(page)
        serialized = serialize_elements(result.elements)
        a11y_norths = [e for e in serialized if e["name"] == "North" and not e.get("selector")]
        assert len(a11y_norths) == 2

    @pytest.mark.asyncio
    async def test_match_index_stable_when_cap_reorders(self) -> None:
        # A selector-bearing duplicate reordered ahead of the cap must NOT shift the nth ordinal
        # of a selectorless duplicate sharing its (role, name). The a11y duplicate must keep its
        # original ordinal (0), so its `nth=N` fallback ref points at the right element.
        tree = _make_a11y_tree(
            children=[
                {"role": "combobox", "name": "Dup", "value": ""},
                {"role": "combobox", "name": "Dup", "value": ""},
            ]
        )
        page = _make_page(tree)
        page.evaluate = AsyncMock(
            return_value=[
                {"role": "combobox", "name": "Dup", "tag": "select", "selector": "#dup3"},
            ]
        )
        result = await do_observe(page, max_elements=2)
        selectorless_dups = [e for e in result.elements if e.name == "Dup" and not e.selector]
        assert selectorless_dups, result.elements
        assert selectorless_dups[0].match_index == 0

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
    async def test_failed_observe_clears_batch_local_refs(self) -> None:
        seen_ref_maps: list[dict[str, Any]] = []

        async def dispatch(step: ExecuteStep, ref_map: dict) -> dict[str, Any] | None:
            if step.tool == "click":
                seen_ref_maps.append(dict(ref_map))
                return None
            if step.params.get("fail"):
                raise RuntimeError("frame lost")
            return {"elements": [{"ref": "e0", "role": "button", "name": "X", "tag": "button", "selector": "#x"}]}

        steps = [
            ExecuteStep(tool="observe", params={}),
            ExecuteStep(tool="observe", params={"fail": True}),
            ExecuteStep(tool="click", params={"ref": "e0"}),
        ]
        result = await do_execute(dispatch, steps, stop_on_error=False)

        assert result.results[0].ok is True
        assert result.results[1].ok is False
        assert seen_ref_maps == [{}]

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
    async def test_default_observe_omits_values_from_output_and_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(state):
            result = await mcp_browser.skyvern_observe()

        assert all("value" not in element for element in result["data"]["elements"])
        assert all("value" not in element for element in state._observed_refs["refs"].values())

    @pytest.mark.asyncio
    async def test_observe_include_values_keeps_values_out_of_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_values_page()
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(state):
            result = await mcp_browser.skyvern_observe(include_values=True)

        email = next(element for element in result["data"]["elements"] if element["name"] == "Email")
        assert email["value"] == "person@example.com"
        assert all("value" not in element for element in state._observed_refs["refs"].values())

    @pytest.mark.asyncio
    async def test_observe_include_values_keeps_non_password_dom_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_values_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        result = await mcp_browser.skyvern_observe(include_values=True)

        email = next(element for element in result["data"]["elements"] if element["name"] == "Email")
        password = next(element for element in result["data"]["elements"] if element["name"] == "Current")
        assert email["value"] == "person@example.com"
        assert "value" not in password

    @pytest.mark.asyncio
    async def test_observe_preserves_existing_positional_parameter_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        observe = AsyncMock(
            return_value=ObserveResult(
                url=page.url,
                title="Login Page",
                elements=[],
                element_count=0,
                total_on_page=0,
            )
        )
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        monkeypatch.setattr(mcp_browser, "do_observe", observe)

        result = await mcp_browser.skyvern_observe(None, None, None, False, 7)

        assert result["ok"] is True
        observe.assert_awaited_once_with(
            page,
            selector=None,
            interactive_only=False,
            max_elements=7,
            include_values=False,
        )

    @pytest.mark.asyncio
    async def test_observe_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError("no browser")))

        result = await mcp_browser.skyvern_observe()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_non_evaluable_working_frame_returns_structured_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_page()
        page.evaluate = AsyncMock(return_value=[])
        page.accessibility = None
        frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            evaluate=AsyncMock(side_effect=RuntimeError("Execution context was destroyed")),
        )
        page._working_frame = frame
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        state._observed_refs = {"page_key": (1, 2, "old", "old-frame"), "refs": {"e0": {"ref": "e0"}}}
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(state):
            result = await mcp_browser.skyvern_observe()

        assert result["ok"] is False
        error = result["error"]
        assert error["code"] == mcp_browser.ErrorCode.ACTION_FAILED
        assert error["details"]["frame_name"] == frame.name
        assert error["details"]["frame_url"] == frame.url
        assert frame.name in error["message"] or frame.url in error["message"]
        assert any(action in error["hint"] for action in ("skyvern_frame_main", "skyvern_frame_list", "selector"))
        assert state._observed_refs == {}


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
    async def test_execute_observe_threads_include_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        observe = AsyncMock(
            return_value=ObserveResult(
                url=page.url,
                title="Login Page",
                elements=[],
                element_count=0,
                total_on_page=0,
            )
        )
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        monkeypatch.setattr("skyvern.cli.core.browser_ops.do_observe", observe)

        result = await mcp_browser.skyvern_execute(steps=[{"tool": "observe", "params": {"include_values": True}}])

        assert result["ok"] is True
        observe.assert_awaited_once_with(page, include_values=True)

    @pytest.mark.asyncio
    async def test_execute_observe_returns_structured_frame_error_and_clears_refs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_page()
        frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            is_detached=lambda: True,
            evaluate=AsyncMock(),
        )
        page._working_frame = frame
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        state._observed_refs = {"page_key": (1, 2, "old", "old-frame"), "refs": {"e0": {"ref": "e0"}}}
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(state):
            result = await mcp_browser.skyvern_execute(steps=[{"tool": "observe", "params": {}}])

        error = result["data"]["results"][0]["error"]
        assert result["ok"] is False
        assert error["code"] == mcp_browser.ErrorCode.ACTION_FAILED
        assert error["details"] == {"frame_name": frame.name, "frame_url": frame.url}
        assert "skyvern_frame_main" in error["hint"]
        assert state._observed_refs == {}
        frame.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_resolves_ref_from_prior_observe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        async with scoped_session(make_session_state(context=ctx)):
            observe_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Sign In")

        async with scoped_session(make_session_state(context=ctx)):
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": ref}}],
                session_id=ctx.session_id,
            )

        assert execute_result["ok"] is True
        assert execute_result["data"]["steps_completed"] == 1
        assert mcp_browser.skyvern_click.call_args.kwargs["selector"] == 'role=button[name="Sign In"]'

    @pytest.mark.asyncio
    async def test_execute_observe_include_values_keeps_values_out_of_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_values_page()
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(state):
            result = await mcp_browser.skyvern_execute(steps=[{"tool": "observe", "params": {"include_values": True}}])

        elements = result["data"]["results"][0]["data"]["elements"]
        email = next(element for element in elements if element["name"] == "Email")
        assert email["value"] == "person@example.com"
        assert state._observed_refs["refs"]
        assert all("value" not in element for element in state._observed_refs["refs"].values())

    @pytest.mark.asyncio
    async def test_execute_resolves_ref_from_prior_observe_without_session_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The original stdio/local path keeps refs on the shared SessionState."""
        page = _make_page()
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value={"ok": True, "data": None}))

        async with scoped_session(state):
            observe_result = await mcp_browser.skyvern_observe()
            ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Sign In")

        async with scoped_session(state):
            execute_result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

        assert execute_result["ok"] is True
        assert state._observed_refs["refs"][ref]["name"] == "Sign In"
        assert mcp_browser.skyvern_click.call_args.kwargs["selector"] == 'role=button[name="Sign In"]'

    @pytest.mark.asyncio
    async def test_keyless_ref_is_stale_when_url_changes_during_observe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = _make_page()

        async def replace_document_during_observe() -> str:
            page.url = "https://example.com/replaced-document"
            return "Replacement"

        page.title = AsyncMock(side_effect=replace_document_during_observe)
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        click = AsyncMock(return_value={"ok": True, "data": None})
        monkeypatch.setattr(mcp_browser, "skyvern_click", click)

        async with scoped_session(state):
            observe_result = await mcp_browser.skyvern_observe()
            ref = observe_result["data"]["elements"][0]["ref"]

        async with scoped_session(state):
            execute_result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

        assert execute_result["data"]["results"][0]["error"] == (
            f"Unknown ref '{ref}' — call observe first or check ref exists"
        )
        click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_iframe_ref_is_stale_after_frame_navigation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        frame = SimpleNamespace(
            name="payment",
            url="https://example.com/payment-frame",
            title=AsyncMock(return_value="Payment"),
            evaluate=AsyncMock(
                return_value=[
                    {
                        "role": "button",
                        "name": "Frame action",
                        "tag": "button",
                        "selector": "#frame-action",
                    }
                ]
            ),
        )

        async def goto(url: str) -> None:
            frame.url = url

        frame.goto = AsyncMock(side_effect=goto)
        page._working_frame = frame
        ctx = BrowserContext(mode="local")
        state = make_session_state(context=ctx)
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        click = AsyncMock(return_value={"ok": True, "data": None})
        monkeypatch.setattr(mcp_browser, "skyvern_click", click)

        async with scoped_session(state):
            observed = await mcp_browser.skyvern_observe()
            ref = observed["data"]["elements"][0]["ref"]
            await frame.goto("https://example.com/replacement-frame")
            result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

        assert result["data"]["results"][0]["error"] == (
            f"Unknown ref '{ref}' — call observe first or check ref exists"
        )
        click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_does_not_resolve_ref_from_different_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        first_ctx = BrowserContext(mode="cloud_session", session_id="pbs_first")
        second_ctx = BrowserContext(mode="cloud_session", session_id="pbs_second")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, first_ctx)))

        async with scoped_session(make_session_state(context=first_ctx)):
            observe_result = await mcp_browser.skyvern_observe(session_id=first_ctx.session_id)

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, second_ctx)))
        async with scoped_session(make_session_state(context=second_ctx)):
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": observe_result["data"]["elements"][0]["ref"]}}],
                session_id=second_ctx.session_id,
            )

        assert execute_result["data"]["results"][0]["error"] == (
            "Unknown ref 'e0' — call observe first or check ref exists"
        )

    @pytest.mark.asyncio
    async def test_execute_observe_step_persists_refs_for_next_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_batch_observe")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        async with scoped_session(make_session_state(context=ctx)):
            observe_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "observe", "params": {}}],
                session_id=ctx.session_id,
            )
            click_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": "e2"}}],
                session_id=ctx.session_id,
            )

        assert observe_result["ok"] is True
        assert click_result["ok"] is True
        assert mcp_browser.skyvern_click.call_args.kwargs["selector"] == 'role=button[name="Sign In"]'

    @pytest.mark.asyncio
    async def test_second_observe_replaces_prior_session_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first_tree = _make_a11y_tree(
            children=[
                {"role": "button", "name": "First"},
                {"role": "button", "name": "Stale"},
            ]
        )
        second_tree = _make_a11y_tree(children=[{"role": "button", "name": "Current"}])
        page = _make_page(first_tree)
        page.accessibility.snapshot.side_effect = [first_tree, second_tree]
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_replace_refs")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_tool_result = {"ok": True, "data": None}
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_tool_result))

        async with scoped_session(make_session_state(context=ctx)):
            first_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            second_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            stale_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": "e1"}}],
                session_id=ctx.session_id,
            )
            current_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": "e0"}}],
                session_id=ctx.session_id,
            )

        assert [element["ref"] for element in first_result["data"]["elements"]] == ["e0", "e1"]
        assert [element["ref"] for element in second_result["data"]["elements"]] == ["e0"]
        assert stale_result["data"]["results"][0]["error"] == (
            "Unknown ref 'e1' — call observe first or check ref exists"
        )
        assert current_result["ok"] is True
        assert mcp_browser.skyvern_click.call_args.kwargs["selector"] == 'role=button[name="Current"]'

    @pytest.mark.asyncio
    async def test_navigate_clears_session_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_navigate_refs")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(make_session_state(context=ctx)):
            observe_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            navigate_result = await mcp_browser.skyvern_navigate(
                url="https://example.com/next",
                session_id=ctx.session_id,
            )
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": "e2"}}],
                session_id=ctx.session_id,
            )

        assert observe_result["ok"] is True
        assert navigate_result["ok"] is True
        assert execute_result["data"]["results"][0]["error"] == (
            "Unknown ref 'e2' — call observe first or check ref exists"
        )

    @pytest.mark.asyncio
    async def test_navigate_clears_batch_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_batch_navigate_refs")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value={"ok": True, "data": None}))

        async with scoped_session(make_session_state(context=ctx)):
            result = await mcp_browser.skyvern_execute(
                steps=[
                    {"tool": "observe", "params": {}},
                    {"tool": "navigate", "params": {"url": "https://example.com/next"}},
                    {"tool": "click", "params": {"ref": "e2"}},
                ],
                session_id=ctx.session_id,
            )

        assert result["ok"] is False
        assert result["data"]["error_step"] == 2
        assert result["data"]["results"][2]["error"] == ("Unknown ref 'e2' — call observe first or check ref exists")
        mcp_browser.skyvern_click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tab_switch_invalidates_session_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_tab_refs")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
        monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(page, ctx)))

        tab_a = MagicMock()
        tab_a.url = "https://example.com/a"
        tab_a.title = AsyncMock(return_value="A")
        tab_a.is_closed = MagicMock(return_value=False)
        tab_b = MagicMock()
        tab_b.url = "https://example.com/b"
        tab_b.title = AsyncMock(return_value="B")
        tab_b.is_closed = MagicMock(return_value=False)
        tab_b.bring_to_front = AsyncMock()
        browser = MagicMock()
        browser._browser_context.pages = [tab_a, tab_b]

        async with scoped_session(make_session_state(context=ctx, browser=browser)):
            observe_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            ref = observe_result["data"]["elements"][0]["ref"]
            switch_result = await mcp_tabs.skyvern_tab_switch(session_id=ctx.session_id, index=1)
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": ref}}],
                session_id=ctx.session_id,
            )

        assert switch_result["ok"] is True
        assert execute_result["data"]["results"][0]["error"] == (
            f"Unknown ref '{ref}' — call observe first or check ref exists"
        )

    @pytest.mark.asyncio
    async def test_frame_main_clears_session_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        page.frame_main = MagicMock()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_frame_refs")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(make_session_state(context=ctx)):
            observe_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            ref = observe_result["data"]["elements"][0]["ref"]
            frame_result = await mcp_browser.skyvern_frame_main(session_id=ctx.session_id)
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": ref}}],
                session_id=ctx.session_id,
            )

        assert frame_result["ok"] is True
        assert execute_result["data"]["results"][0]["error"] == (
            f"Unknown ref '{ref}' — call observe first or check ref exists"
        )

    @pytest.mark.asyncio
    async def test_popup_page_change_invalidates_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Implicit page transition (popup steals working page) must not resolve old refs."""
        page_a = _make_page()
        page_b = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_popup_refs")

        async with scoped_session(make_session_state(context=ctx)):
            monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page_a, ctx)))
            observe_result = await mcp_browser.skyvern_observe(session_id=ctx.session_id)
            ref = observe_result["data"]["elements"][0]["ref"]

            monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page_b, ctx)))
            execute_result = await mcp_browser.skyvern_execute(
                steps=[{"tool": "click", "params": {"ref": ref}}],
                session_id=ctx.session_id,
            )

        assert execute_result["data"]["results"][0]["error"] == (
            f"Unknown ref '{ref}' — call observe first or check ref exists"
        )

    @pytest.mark.asyncio
    async def test_generation_eviction_does_not_resurrect_refs(self) -> None:
        """ABA guard: eviction of a cleared session's generation must not let a stale commit through."""
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_aba_victim")
        element = {"ref": "e0", "role": "button", "name": "Sign In", "selector": "#signin"}

        async with scoped_session(make_session_state(context=ctx)):
            stale_generation = session_manager.session_ref_generation(session_id=ctx.session_id)
            session_manager.clear_session_ref_map(session_id=ctx.session_id)
            for i in range(session_manager._SESSION_REF_STORE_MAX + 5):
                session_manager.clear_session_ref_map(session_id=f"pbs_aba_churn_{i}")

            committed = session_manager.replace_session_ref_map(
                {"e0": element}, session_id=ctx.session_id, generation=stale_generation
            )
            assert committed is False
            assert session_manager.get_session_ref("e0", session_id=ctx.session_id) is None

    @pytest.mark.asyncio
    async def test_rejected_batch_observe_does_not_install_local_refs(self) -> None:
        """If session publication rejects an observe snapshot, the batch must not act on it either."""

        async def dispatch(step: ExecuteStep, ref_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
            if step.tool == "observe":
                return {"elements": [{"ref": "e0", "role": "button", "name": "Sign In"}], "element_count": 1}
            if ref := step.params.get("ref"):
                if ref not in ref_map:
                    raise ValueError(f"Unknown ref '{ref}' — call observe first or check ref exists")
            return None

        result = await do_execute(
            dispatch,
            [ExecuteStep(tool="observe"), ExecuteStep(tool="click", params={"ref": "e0"})],
            on_ref_map_update=lambda ref_map: False,
        )

        assert result.error_step == 1
        assert "Unknown ref 'e0'" in str(result.results[1].error)

    @pytest.mark.asyncio
    async def test_observe_started_during_navigation_is_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A generation captured while navigation is in flight must not commit after it completes."""
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_nav_race")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        in_flight_generation: list[int] = []

        async def fake_navigate(*args: Any, **kwargs: Any) -> Any:
            in_flight_generation.append(session_manager.session_ref_generation(session_id=ctx.session_id))
            return SimpleNamespace(url="https://example.com/next", title="Next")

        monkeypatch.setattr(mcp_browser, "do_navigate", fake_navigate)

        async with scoped_session(make_session_state(context=ctx)):
            nav_result = await mcp_browser.skyvern_navigate("https://example.com/next", session_id=ctx.session_id)
            committed = session_manager.replace_session_ref_map(
                {"e0": {"ref": "e0", "selector": "#old"}},
                session_id=ctx.session_id,
                generation=in_flight_generation[0],
            )

        assert nav_result["ok"] is True
        assert committed is False
        assert session_manager.get_session_ref("e0", session_id=ctx.session_id) is None

    @pytest.mark.asyncio
    async def test_popup_mid_batch_invalidates_batch_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A page change between an in-batch observe and a ref step must not resolve stale refs."""
        page_a = _make_page()
        page_b = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_popup_batch")
        monkeypatch.setattr(
            mcp_browser,
            "get_page",
            AsyncMock(side_effect=[(page_a, ctx), (page_a, ctx), (page_b, ctx)]),
        )

        async with scoped_session(make_session_state(context=ctx)):
            result = await mcp_browser.skyvern_execute(
                steps=[
                    {"tool": "observe", "params": {}},
                    {"tool": "click", "params": {"ref": "e0"}},
                ],
                session_id=ctx.session_id,
            )

        assert result["data"]["results"][1]["error"] == ("Unknown ref 'e0' — call observe first or check ref exists")

    @pytest.mark.asyncio
    async def test_popup_before_batch_observe_binds_refs_to_observed_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An observe after popup steal binds to the new page it actually inspected."""
        page_a = _make_page()
        page_b = _make_page()
        page_b.url = "https://example.com/popup"
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_popup_before_observe")
        monkeypatch.setattr(
            mcp_browser,
            "get_page",
            AsyncMock(side_effect=[(page_a, ctx), (page_b, ctx), (page_b, ctx)]),
        )
        monkeypatch.setattr(mcp_browser, "skyvern_evaluate", AsyncMock(return_value={"ok": True, "data": None}))
        click = AsyncMock(return_value={"ok": True, "data": None})
        monkeypatch.setattr(mcp_browser, "skyvern_click", click)

        async with scoped_session(make_session_state(context=ctx)):
            result = await mcp_browser.skyvern_execute(
                steps=[
                    {"tool": "evaluate", "params": {"expression": "window.open('about:blank')"}},
                    {"tool": "observe", "params": {}},
                    {"tool": "click", "params": {"ref": "e0"}},
                ],
                session_id=ctx.session_id,
            )

        assert result["ok"] is True
        click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_same_page_url_change_invalidates_batch_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A same-tab document replacement keeps Page identity but invalidates refs by URL."""
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_click_nav_batch")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async def replace_document(**kwargs: Any) -> dict[str, Any]:
            page.url = "https://example.com/replaced-document"
            return {"ok": True, "data": None}

        monkeypatch.setattr(mcp_browser, "skyvern_evaluate", AsyncMock(side_effect=replace_document))
        click = AsyncMock(return_value={"ok": True, "data": None})
        monkeypatch.setattr(mcp_browser, "skyvern_click", click)

        async with scoped_session(make_session_state(context=ctx)):
            result = await mcp_browser.skyvern_execute(
                steps=[
                    {"tool": "observe", "params": {}},
                    {"tool": "evaluate", "params": {"expression": "location.href = '/replaced-document'"}},
                    {"tool": "click", "params": {"ref": "e0"}},
                ],
                session_id=ctx.session_id,
            )

        assert result["data"]["results"][2]["error"] == ("Unknown ref 'e0' — call observe first or check ref exists")
        click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_navigation_still_invalidates_midflight_observe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A navigation that raises can still have replaced the document — mid-flight commits must die."""
        page = _make_page()
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_failed_nav")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        in_flight_generation: list[int] = []

        async def failing_navigate(*args: Any, **kwargs: Any) -> Any:
            in_flight_generation.append(session_manager.session_ref_generation(session_id=ctx.session_id))
            raise RuntimeError("net::ERR_ABORTED")

        monkeypatch.setattr(mcp_browser, "do_navigate", failing_navigate)

        async with scoped_session(make_session_state(context=ctx)):
            nav_result = await mcp_browser.skyvern_navigate("https://example.com/next", session_id=ctx.session_id)
            committed = session_manager.replace_session_ref_map(
                {"e0": {"ref": "e0", "selector": "#old"}},
                session_id=ctx.session_id,
                generation=in_flight_generation[0],
            )

        assert nav_result["ok"] is False
        assert committed is False
        assert session_manager.get_session_ref("e0", session_id=ctx.session_id) is None

    def test_page_identity_key_never_reused_after_gc(self) -> None:
        """Identity tokens must not be reissued to a new page even if id() is reused."""
        page = _make_page()
        old_key = session_manager.page_ref_key(page)
        del page
        gc.collect()
        new_page = _make_page()
        assert session_manager.page_ref_key(new_page) != old_key

    @pytest.mark.asyncio
    async def test_stale_observe_commit_is_discarded_after_clear(self) -> None:
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_stale_gen")
        element = {"ref": "e0", "role": "button", "name": "Sign In", "selector": "#signin"}

        async with scoped_session(make_session_state(context=ctx)):
            stale_generation = session_manager.session_ref_generation(session_id=ctx.session_id)
            session_manager.clear_session_ref_map(session_id=ctx.session_id)
            session_manager.replace_session_ref_map(
                {"e0": element}, session_id=ctx.session_id, generation=stale_generation
            )
            assert session_manager.get_session_ref("e0", session_id=ctx.session_id) is None

            fresh_generation = session_manager.session_ref_generation(session_id=ctx.session_id)
            session_manager.replace_session_ref_map(
                {"e0": element}, session_id=ctx.session_id, generation=fresh_generation
            )
            assert session_manager.get_session_ref("e0", session_id=ctx.session_id) == element

    @pytest.mark.asyncio
    async def test_execute_observe_then_click_native_option_ref_selects_parent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        option_selector = "#region > option:nth-of-type(4)"
        option_locator = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "select_selector": "#region",
                    "value": "east",
                    "label": "East",
                }
            )
        )
        option_locator.first = option_locator
        select_option = AsyncMock()
        select_locator = SimpleNamespace(select_option=select_option)
        page = SimpleNamespace(
            url="https://example.com/form",
            title=AsyncMock(return_value="Form"),
            accessibility=None,
            evaluate=AsyncMock(
                return_value=[
                    {
                        "role": "option",
                        "name": "East",
                        "tag": "option",
                        "selector": option_selector,
                    }
                ]
            ),
            locator=MagicMock(
                side_effect=lambda selector: option_locator if selector == option_selector else select_locator
            ),
            click=AsyncMock(return_value="#unexpected-click"),
        )
        page.page = page
        page._working_frame = None
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        result = await mcp_browser.skyvern_execute(
            steps=[
                {"tool": "observe", "params": {}},
                {"tool": "click", "params": {"ref": "e0"}},
            ]
        )

        assert result["ok"] is True
        assert result["data"]["steps_completed"] == 2
        page.click.assert_not_awaited()
        select_option.assert_awaited_once_with(value="east", timeout=5000)
        assert result["data"]["results"][1]["data"]["resolved_selector"] == "#region"

    @pytest.mark.asyncio
    async def test_execute_unknown_ref_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        async with scoped_session(make_session_state(context=ctx)):
            result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"ref": "e99"}}])

        assert result["ok"] is False
        assert result["data"]["results"][0]["error"] == ("Unknown ref 'e99' — call observe first or check ref exists")

    @pytest.mark.asyncio
    async def test_execute_preserves_structured_direct_failure_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        click_result = {
            "ok": False,
            "error": {
                "code": mcp_browser.ErrorCode.ACTION_FAILED,
                "message": "Selector matched an element that is not visible",
                "hint": "The element exists but is not visible.",
                "details": {"element_state": "hidden", "selector": "#field", "actionability_timeout_ms": 5000},
            },
        }
        monkeypatch.setattr(mcp_browser, "skyvern_click", AsyncMock(return_value=click_result))

        result = await mcp_browser.skyvern_execute(steps=[{"tool": "click", "params": {"selector": "#field"}}])

        assert result["ok"] is False
        step_error = result["data"]["results"][0]["error"]
        assert step_error["code"] == mcp_browser.ErrorCode.ACTION_FAILED
        assert step_error["details"]["element_state"] == "hidden"
        assert step_error["details"]["selector"] == "#field"

    @pytest.mark.asyncio
    async def test_execute_wait_error_does_not_leak_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()
        page.validate = AsyncMock(
            side_effect=InternalServerError(
                body={"error": "Unexpected error: sk-BODY-SECRET"},
                headers={"authorization": "Bearer sk-SECRET"},
            )
        )
        page.wait_for_timeout = make_real_wait_for_timeout()
        ctx = BrowserContext(mode="local")
        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

        result = await mcp_browser.skyvern_execute(
            steps=[
                {
                    "tool": "wait",
                    "params": {
                        "intent": "the spinner disappears",
                        "timeout": 1000,
                        "poll_interval_ms": 500,
                    },
                }
            ]
        )

        assert result["ok"] is False
        step_error = result["data"]["results"][0]["error"]
        assert step_error["message"] == "HTTP 500: InternalServerError"
        assert step_error["details"] == {"exception_type": "InternalServerError", "status_code": 500}
        message = step_error["message"].lower()
        for leaked in ("authorization", "bearer", "sk-secret", "sk-body-secret", "headers", "body"):
            assert leaked not in message

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
