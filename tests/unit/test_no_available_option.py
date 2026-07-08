"""Unit tests for the custom-select no-match path."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import (
    NoAvailableOptionFoundForCustomSelection,
    NoElementMatchedForTargetOption,
    NoIncrementalElementFoundForCustomSelection,
)
from skyvern.forge.agent_functions import AgentFunction
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputOrSelectContext, SelectOption, SelectOptionAction
from skyvern.webeye.actions.handler import (
    _collect_option_texts,
    _custom_select_candidates_from_elements,
    _no_match_exception_for_dropdown,
    _select_deterministic_custom_option,
    _verify_custom_select_option,
)
from tests.unit.helpers import make_organization, make_task


class _FakeCustomElement:
    def __init__(self, *, role: str | None = None, tag_name: str = "div") -> None:
        self.get_attr = AsyncMock(return_value=role)
        self.get_tag_name = MagicMock(return_value=tag_name)
        self.get_frame = MagicMock(return_value=MagicMock())
        self.get_element_handler = AsyncMock(return_value=MagicMock())
        self.scroll_into_view = AsyncMock()
        self.click = AsyncMock()
        self._locator = MagicMock()

    def get_locator(self) -> MagicMock:
        return self._locator


class _FakeAnchorElement:
    def __init__(self, *, tag_name: str = "button") -> None:
        self._locator = MagicMock()
        self._locator.evaluate = AsyncMock(return_value=False)
        self._locator.fill = AsyncMock()
        self.get_attr = AsyncMock(return_value=None)
        self.get_tag_name = MagicMock(return_value=tag_name)
        self.get_id = MagicMock(return_value="field-control")
        self.get_frame = MagicMock(return_value=MagicMock())
        self.get_element_handler = AsyncMock(return_value=MagicMock())
        self.is_custom_option = AsyncMock(return_value=False)
        self.is_selectable = AsyncMock(return_value=True)
        self.is_disabled = AsyncMock(return_value=False)
        self.is_checkbox = AsyncMock(return_value=False)
        self.is_radio = AsyncMock(return_value=False)
        self.is_btn_input = AsyncMock(return_value=False)
        self.is_visible = AsyncMock(return_value=False)
        self.scroll_into_view = AsyncMock()
        self.click = AsyncMock()
        self.coordinate_click = AsyncMock()
        self.press_key = AsyncMock()
        self.blur = AsyncMock()

    def get_locator(self) -> MagicMock:
        return self._locator


def _stub_evaluate(
    *,
    matched_state: dict | list[dict | None] | None = None,
    committed: bool | None = None,
) -> AsyncMock:
    """Stub handler.SkyvernFrame.evaluate, dispatching by which PR JS body is being run.

    matched_state may be a single value (repeated) or a list consumed one entry per call, so a
    test can distinguish the pre-click idempotence read from the post-click verification read.
    """
    matched_states = list(matched_state) if isinstance(matched_state, list) else None

    async def _evaluate(*, frame: object, expression: str, arg: object = None) -> object:
        if "return { label," in expression:
            if matched_states is not None:
                return matched_states.pop(0) if matched_states else None
            return matched_state
        if "anchorIsComboboxInput" in expression:
            return committed
        return None

    return AsyncMock(side_effect=_evaluate)


class _FakeIncrementalScrapePage:
    def __init__(self, element_trees: list[list[dict]]) -> None:
        self._element_trees = list(element_trees)
        self.start_listen_dom_increment = AsyncMock()
        self.stop_listen_dom_increment = AsyncMock()
        self.set_element_tree_trimmed = MagicMock()
        self.build_element_tree = MagicMock(return_value="<div></div>")

    async def get_incremental_element_tree(self, *_args: object, **_kwargs: object) -> list[dict]:
        return self._element_trees.pop(0)


class _FakeValueFallbackScrapePage:
    def __init__(self) -> None:
        self.get_incremental_element_tree = AsyncMock(return_value=[])
        self.select_one_element_by_value = AsyncMock(return_value=None)


class _FakeDropdownMenuElement:
    def __init__(self) -> None:
        self.get_element_handler = AsyncMock(return_value=MagicMock())


def _task() -> object:
    now = datetime.now(UTC)
    organization = make_organization(now)
    return make_task(now, organization)


def _select_action(label: str = "Choice") -> SelectOptionAction:
    return SelectOptionAction(
        element_id="field-control",
        option=SelectOption(label=label),
        input_or_select_context=InputOrSelectContext(field="Field", is_required=True),
    )


class TestCollectOptionTexts:
    def test_extracts_li_option_texts(self) -> None:
        tree = [
            {
                "tagName": "ul",
                "attributes": {"role": "listbox"},
                "children": [
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Charlie"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo", "Charlie"]

    def test_extracts_native_option_elements(self) -> None:
        tree = [
            {
                "tagName": "select",
                "children": [
                    {"tagName": "option", "text": "First"},
                    {"tagName": "option", "text": "Second"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["First", "Second"]

    def test_ignores_non_option_nodes(self) -> None:
        tree = [
            {"tagName": "div", "text": "header copy"},
            {"tagName": "button", "text": "Submit"},
            {"tagName": "span", "text": "label"},
        ]
        assert _collect_option_texts(tree) == []

    def test_returns_empty_for_empty_tree(self) -> None:
        assert _collect_option_texts([]) == []

    def test_handles_missing_optional_fields(self) -> None:
        tree = [
            {"tagName": "li"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": ""},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "  "},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Real"},
        ]
        assert _collect_option_texts(tree) == ["Real"]

    def test_dedupes_repeated_option_text(self) -> None:
        tree = [
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]

    def test_walks_nested_children(self) -> None:
        tree = [
            {
                "tagName": "div",
                "children": [
                    {
                        "tagName": "ul",
                        "attributes": {"role": "listbox"},
                        "children": [
                            {"tagName": "li", "attributes": {"role": "option"}, "text": "Inner"},
                        ],
                    }
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Inner"]

    def test_extracts_div_role_option(self) -> None:
        tree = [
            {
                "tagName": "div",
                "attributes": {"role": "listbox"},
                "children": [
                    {"tagName": "div", "attributes": {"role": "option"}, "text": "Alpha"},
                    {"tagName": "div", "attributes": {"role": "option"}, "text": "Bravo"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]

    def test_extracts_native_select_from_options_field(self) -> None:
        # Scraper stores native <select> options on the element itself and
        # skips child <option> nodes.
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "January", "value": "1"},
                    {"optionIndex": 1, "text": "February", "value": "2"},
                    {"optionIndex": 2, "text": "March", "value": "3"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["January", "February", "March"]

    def test_falls_back_to_value_when_options_text_is_empty(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "", "value": "Q1"},
                    {"optionIndex": 1, "text": "Two", "value": "2"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Q1", "Two"]

    def test_falls_back_to_value_when_options_text_is_whitespace_only(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "   ", "value": "Q1"},
                    {"optionIndex": 1, "text": "\t\n", "value": "Q2"},
                    {"optionIndex": 2, "text": "Real", "value": "x"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Q1", "Q2", "Real"]

    def test_dedupes_across_li_and_options_field(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [{"optionIndex": 0, "text": "Alpha", "value": "a"}],
            },
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]


class TestCustomSelectCandidates:
    def test_extracts_role_option_dedupes_checkbox_and_skips_listbox_container(self) -> None:
        tree = [
            {
                "id": "source-panel",
                "tagName": "div",
                "attributes": {"role": "listbox"},
                "text": "Job Board Referral",
                "interactable": True,
                "children": [
                    {
                        "id": "source-job-board",
                        "tagName": "div",
                        "attributes": {"role": "option", "value": "job-board"},
                        "text": "Job Board",
                        "interactable": True,
                    },
                    {
                        "id": "source-help-link",
                        "tagName": "a",
                        "attributes": {"href": "/help"},
                        "text": "Job Board",
                        "interactable": True,
                    },
                ],
            },
            {
                "id": "county-panel",
                "tagName": "div",
                "children": [
                    {
                        "id": "county-a-label",
                        "tagName": "label",
                        "text": "County A",
                        "interactable": True,
                        "children": [
                            {
                                "id": "county-a-input",
                                "tagName": "input",
                                "attributes": {"type": "checkbox", "value": "County A"},
                                "interactable": True,
                            }
                        ],
                    }
                ],
            },
            {
                "id": "nav-job-board",
                "tagName": "a",
                "attributes": {"href": "/jobs"},
                "text": "Job Board",
                "interactable": True,
            },
            {
                "id": "nav-list",
                "tagName": "ul",
                "children": [
                    {
                        "id": "nav-list-job-board",
                        "tagName": "li",
                        "text": "Job Board",
                        "interactable": True,
                    }
                ],
            },
        ]

        assert _custom_select_candidates_from_elements(tree) == [
            {"label": "Job Board", "element_id": "source-job-board", "value": "job-board", "is_choice_input": False},
            {"label": "County A", "element_id": "county-a-label", "value": "County A", "is_choice_input": True},
        ]

    def test_extracts_menuitemradio_with_aria_checked_state(self) -> None:
        tree = [
            {
                "id": "choice-menu",
                "tagName": "div",
                "attributes": {"role": "menu"},
                "children": [
                    {
                        "id": "choice-radio",
                        "tagName": "div",
                        "attributes": {"role": "menuitemradio", "aria-checked": "false"},
                        "text": "Choice",
                        "interactable": True,
                    }
                ],
            }
        ]

        assert _custom_select_candidates_from_elements(tree) == [
            {"label": "Choice", "element_id": "choice-radio", "value": None, "is_choice_input": True}
        ]

    def test_role_option_wrapping_checkbox_is_choice_input_shaped(self) -> None:
        tree = [
            {
                "id": "opt-panel",
                "tagName": "div",
                "attributes": {"role": "listbox"},
                "children": [
                    {
                        "id": "opt-choice",
                        "tagName": "div",
                        "attributes": {"role": "option"},
                        "text": "Choice",
                        "interactable": True,
                        "children": [
                            {
                                "id": "opt-choice-input",
                                "tagName": "input",
                                "attributes": {"type": "checkbox"},
                                "interactable": True,
                            }
                        ],
                    }
                ],
            }
        ]

        assert _custom_select_candidates_from_elements(tree) == [
            {"label": "Choice", "element_id": "opt-choice", "value": None, "is_choice_input": True}
        ]


class TestDeterministicCustomSelect:
    @pytest.mark.asyncio
    async def test_ambiguous_match_returns_none_for_llm_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        get_skyvern_element = AsyncMock()
        get_readback_scope_element = AsyncMock()

        result = await _select_deterministic_custom_option(
            target_value="United States",
            get_option_candidates=lambda: [
                {"label": "United States", "element_id": "us-1", "value": "US"},
                {"label": "United States", "element_id": "us-2", "value": "USA"},
            ],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=get_skyvern_element,
            get_readback_scope_element=get_readback_scope_element,
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        get_skyvern_element.assert_not_awaited()
        get_readback_scope_element.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_feature_flag_off_does_not_resolve_readback_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=False)),
        )
        get_skyvern_element = AsyncMock()
        get_readback_scope_element = AsyncMock()

        result = await _select_deterministic_custom_option(
            target_value="Job Board",
            get_option_candidates=lambda: [{"label": "Job Board", "element_id": "source-job-board", "value": None}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=get_skyvern_element,
            get_readback_scope_element=get_readback_scope_element,
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        get_skyvern_element.assert_not_awaited()
        get_readback_scope_element.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_date_related_context_skips_deterministic_path(self) -> None:
        get_skyvern_element = AsyncMock()

        result = await _select_deterministic_custom_option(
            target_value="15",
            get_option_candidates=lambda: [{"label": "15", "element_id": "day-15", "value": None}],
            field_context={"is_date_related": True},
            page=MagicMock(),
            get_skyvern_element=get_skyvern_element,
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        get_skyvern_element.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_listbox_container_candidate_returns_none_without_clicking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        selected_element = _FakeCustomElement(role="listbox")

        result = await _select_deterministic_custom_option(
            target_value="Job Board",
            get_option_candidates=lambda: [{"label": "Job Board", "element_id": "source-panel", "value": None}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        selected_element.scroll_into_view.assert_not_awaited()
        selected_element.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aria_checked_menuitemradio_returns_success_without_clicking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={
                    "label": "Choice",
                    "role": "menuitemradio",
                    "ariaSelected": False,
                    "ariaChecked": True,
                    "selectedAttr": False,
                    "checked": False,
                }
            ),
        )
        selected_element = _FakeCustomElement(role="menuitemradio")
        selected_element.get_locator().count = AsyncMock(return_value=1)

        result = await _select_deterministic_custom_option(
            target_value="Choice",
            get_option_candidates=lambda: [{"label": "Choice", "element_id": "choice-radio", "value": None}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert action_result.success
        assert matched_label == "Choice"
        selected_element.click.assert_not_awaited()
        selected_element.scroll_into_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_selected_target_returns_success_without_clicking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={"label": "WAKE", "ariaChecked": False, "selectedAttr": False, "checked": True}
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)

        result = await _select_deterministic_custom_option(
            target_value="WAKE",
            get_option_candidates=lambda: [{"label": "WAKE", "element_id": "county-wake", "value": "WAKE"}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert action_result.success
        assert matched_label == "WAKE"
        selected_element.click.assert_not_awaited()
        selected_element.scroll_into_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_highlighted_role_option_with_bare_aria_selected_proceeds_to_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        matched_states = [
            {
                "label": "Job Board",
                "role": "option",
                "ariaSelected": True,
                "ariaChecked": False,
                "selectedAttr": False,
                "checked": False,
            },
            {
                "label": "Job Board",
                "role": "option",
                "ariaSelected": True,
                "ariaChecked": False,
                "selectedAttr": False,
                "checked": False,
            },
        ]
        scope_args: list[dict[str, object]] = []

        async def evaluate(*, frame: object, expression: str, arg: object = None) -> object:
            if "return { label," in expression:
                return matched_states.pop(0)
            if "anchorIsComboboxInput" in expression:
                assert isinstance(arg, list)
                assert isinstance(arg[1], dict)
                scope_args.append(arg[1])
                return False
            return None

        monkeypatch.setattr(handler.SkyvernFrame, "evaluate", AsyncMock(side_effect=evaluate))
        selected_element = _FakeCustomElement(role="option")
        selected_element.get_locator().count = AsyncMock(return_value=1)
        readback_scope_element = _FakeAnchorElement()

        result = await _select_deterministic_custom_option(
            target_value="Job Board",
            get_option_candidates=lambda: [{"label": "Job Board", "element_id": "source-job-board", "value": None}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            get_readback_scope_element=AsyncMock(return_value=readback_scope_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert action_result.success
        assert matched_label == "Job Board"
        selected_element.click.assert_awaited_once()
        assert scope_args[0]["allowAriaSelectedOptionTokens"] is False

    @pytest.mark.asyncio
    async def test_unchecked_target_proceeds_to_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state=[
                    {"label": "WAKE", "ariaChecked": False, "selectedAttr": False, "checked": False},
                    {"label": "WAKE", "ariaChecked": False, "selectedAttr": False, "checked": True},
                ]
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)

        result = await _select_deterministic_custom_option(
            target_value="WAKE",
            get_option_candidates=lambda: [{"label": "WAKE", "element_id": "county-wake", "value": "WAKE"}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert action_result.success
        assert matched_label == "WAKE"
        selected_element.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_readback_scope_resolution_failure_still_verifies_matched_element(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state=[
                    {"label": "Job Board", "ariaSelected": False, "selectedAttr": False, "checked": False},
                    {"label": "Job Board", "ariaSelected": True, "selectedAttr": False, "checked": False},
                ]
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)

        result = await _select_deterministic_custom_option(
            target_value="Job Board",
            get_option_candidates=lambda: [{"label": "Job Board", "element_id": "source-job-board", "value": None}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            get_readback_scope_element=AsyncMock(side_effect=RuntimeError("anchor disappeared")),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert action_result.success
        assert matched_label == "Job Board"
        selected_element.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clicked_but_unverified_checkbox_returns_failure_without_llm_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={"label": "Job Board", "ariaSelected": False, "selectedAttr": False, "checked": False},
                committed=False,
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)
        # A checkbox-panel anchor (button) is non-resettable, so an unverified click hard-fails.
        readback_scope_element = _FakeAnchorElement(tag_name="button")

        result = await _select_deterministic_custom_option(
            target_value="Job Board",
            get_option_candidates=lambda: [
                {"label": "Job Board", "element_id": "source-job-board", "value": None, "is_choice_input": True}
            ],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            get_readback_scope_element=AsyncMock(return_value=readback_scope_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is not None
        action_result, matched_label = result
        assert not action_result.success
        assert action_result.skip_remaining_actions is True
        assert matched_label == "Job Board"
        assert action_result.exception_message is not None
        assert "could not be verified" in action_result.exception_message
        selected_element.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clicked_but_unverified_non_choice_option_soft_fails_to_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={"label": "Choice", "ariaSelected": False, "selectedAttr": False, "checked": False},
                committed=False,
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)
        # A button/div-anchored single-select listbox (role=option, not checkbox/radio) can be
        # safely replayed by the LLM mini-agent, so an unverified click must soft-fail.
        readback_scope_element = _FakeAnchorElement(tag_name="button")

        result = await _select_deterministic_custom_option(
            target_value="Choice",
            get_option_candidates=lambda: [
                {"label": "Choice", "element_id": "choice-option", "value": None, "is_choice_input": False}
            ],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            get_readback_scope_element=AsyncMock(return_value=readback_scope_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        selected_element.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inconclusive_combobox_routes_to_llm_fallback_after_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={"label": "WAKE", "ariaSelected": False, "selectedAttr": False, "checked": False},
                committed=False,
            ),
        )
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)
        # A text-input combobox anchor is resettable, so an inconclusive read-back must NOT hard-fail.
        readback_scope_element = _FakeAnchorElement(tag_name="input")

        result = await _select_deterministic_custom_option(
            target_value="WAKE",
            get_option_candidates=lambda: [{"label": "WAKE", "element_id": "source-wake", "value": "WAKE"}],
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected_element),
            get_readback_scope_element=AsyncMock(return_value=readback_scope_element),
            task=_task(),  # type: ignore[arg-type]
        )

        assert result is None
        selected_element.click.assert_awaited_once()
        readback_scope_element.get_locator().fill.assert_awaited_once_with("")
        readback_scope_element.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_readback_accepts_aria_checked_matched_menuitemradio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        matched_element = _FakeCustomElement()
        matched_element.get_locator().count = AsyncMock(return_value=1)
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={
                    "label": "Choice",
                    "ariaSelected": False,
                    "ariaChecked": True,
                    "selectedAttr": False,
                    "checked": False,
                },
                committed=False,
            ),
        )
        readback_scope_element = _FakeAnchorElement()

        assert (
            await _verify_custom_select_option(
                matched_element=matched_element,  # type: ignore[arg-type]
                readback_scope_element=readback_scope_element,  # type: ignore[arg-type]
                anchor_is_combobox_input=False,
                matched_element_id="choice-radio",
                matched_label="Choice",
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_readback_accepts_scoped_trigger_reflection_without_synthetic_token(self) -> None:
        matched_element = _FakeCustomElement()
        matched_element.get_locator().count = AsyncMock(return_value=0)
        captured: dict[str, object] = {}

        async def evaluate(*, frame: object, expression: str, arg: object = None) -> object:
            captured["expression"] = expression
            captured["arg"] = arg
            if "return { label," in expression:
                return None
            return True

        readback_scope_element = _FakeAnchorElement()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(handler.SkyvernFrame, "evaluate", AsyncMock(side_effect=evaluate))
            outcome = await _verify_custom_select_option(
                matched_element=matched_element,  # type: ignore[arg-type]
                readback_scope_element=readback_scope_element,  # type: ignore[arg-type]
                anchor_is_combobox_input=False,
                matched_element_id="choice-radio",
                matched_label="Choice",
            )

        assert outcome is True
        script = str(captured["expression"])
        assert "document.querySelectorAll" not in script
        assert "data-selected-option-id" not in script
        assert "data-selected-element-id" not in script
        assert "data-skyvern-selected-id" not in script
        assert "unique_id" not in script
        assert "scopeRoot.querySelectorAll(triggerSelector)" in script
        assert "aria-valuetext" in script
        assert "aria-activedescendant" in script

    @pytest.mark.asyncio
    async def test_combobox_filter_text_value_is_not_a_committed_signal(self) -> None:
        # A combobox <input> whose .value still holds the typed filter text must NOT be accepted as
        # committed; the JS returns False so the caller routes to the safe LLM fallback.
        matched_element = _FakeCustomElement()
        matched_element.get_locator().count = AsyncMock(return_value=0)
        readback_scope_element = _FakeAnchorElement(tag_name="input")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                handler.SkyvernFrame,
                "evaluate",
                _stub_evaluate(matched_state=None, committed=False),
            )
            outcome = await _verify_custom_select_option(
                matched_element=matched_element,  # type: ignore[arg-type]
                readback_scope_element=readback_scope_element,  # type: ignore[arg-type]
                anchor_is_combobox_input=True,
                matched_element_id="source-wake",
                matched_label="WAKE",
            )

        assert outcome is False

    @pytest.mark.asyncio
    async def test_handle_select_option_action_does_not_value_fallback_after_deterministic_readback_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state={"label": "Choice", "ariaSelected": False, "selectedAttr": False, "checked": False},
                committed=False,
            ),
        )

        anchor_element = _FakeAnchorElement()
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)
        fake_frame = MagicMock()
        fake_frame.safe_wait_for_animation_end = AsyncMock()
        # A checkbox-shaped option is required here: only checkbox/radio panels hard-fail on an
        # unverified click (see SKY-11527), and this test asserts hard-fail behavior propagates
        # through handle_select_option_action without falling back to a value-based re-select.
        fake_incremental = _FakeIncrementalScrapePage(
            [
                [{"id": "opened-option"}],
                [
                    {
                        "id": "choice-option",
                        "tagName": "input",
                        "attributes": {"type": "checkbox"},
                        "text": "Choice",
                        "interactable": True,
                    }
                ],
            ]
        )

        class FakeDomUtil:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def get_skyvern_element_by_id(self, _element_id: str) -> _FakeAnchorElement:
                return anchor_element

        select_from_dropdown_by_value = AsyncMock(return_value=handler.ActionSuccess())
        monkeypatch.setattr(handler, "DomUtil", FakeDomUtil)
        monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=fake_frame))
        monkeypatch.setattr(handler, "IncrementalScrapePage", MagicMock(return_value=fake_incremental))
        monkeypatch.setattr(
            handler,
            "_get_input_or_select_context",
            AsyncMock(return_value=InputOrSelectContext(field="Field", is_required=True)),
        )
        monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=None))
        monkeypatch.setattr(handler.SkyvernElement, "create_from_incremental", AsyncMock(return_value=selected_element))
        monkeypatch.setattr(handler, "select_from_dropdown_by_value", select_from_dropdown_by_value)

        results = await handler.handle_select_option_action(
            action=_select_action(),
            page=MagicMock(),
            scraped_page=SimpleNamespace(
                id_to_element_dict={"field-control": {"id": "field-control"}},
                id_to_css_dict={},
            ),
            task=_task(),  # type: ignore[arg-type]
            step=MagicMock(),
        )

        assert len(results) == 1
        assert not results[0].success
        assert results[0].skip_remaining_actions is True
        selected_element.click.assert_awaited_once()
        select_from_dropdown_by_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_select_from_dropdown_deterministic_success_skips_custom_select_prompt_and_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            handler.app,
            "EXPERIMENTATION_PROVIDER",
            SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True)),
        )
        monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
        monkeypatch.setattr(
            handler.SkyvernFrame,
            "evaluate",
            _stub_evaluate(
                matched_state=[
                    {"label": "Choice", "ariaSelected": False, "selectedAttr": False, "checked": False},
                    {"label": "Choice", "ariaSelected": True, "selectedAttr": False, "checked": False},
                ]
            ),
        )
        load_prompt = MagicMock(return_value="prompt")
        custom_select_llm = AsyncMock(return_value={})
        monkeypatch.setattr(handler.prompt_engine, "load_prompt", load_prompt)
        monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", custom_select_llm)
        monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=None))

        anchor_element = _FakeAnchorElement()
        selected_element = _FakeCustomElement()
        selected_element.get_locator().count = AsyncMock(return_value=1)
        fake_incremental = _FakeIncrementalScrapePage(
            [
                [
                    {
                        "id": "choice-option",
                        "tagName": "div",
                        "attributes": {"role": "option"},
                        "text": "Choice",
                        "interactable": True,
                    }
                ]
            ]
        )
        monkeypatch.setattr(handler.SkyvernElement, "create_from_incremental", AsyncMock(return_value=selected_element))

        result = await handler.select_from_dropdown(
            context=InputOrSelectContext(field="Field", is_required=True),
            page=MagicMock(),
            skyvern_element=anchor_element,  # type: ignore[arg-type]
            skyvern_frame=MagicMock(),
            incremental_scraped=fake_incremental,  # type: ignore[arg-type]
            check_filter_funcs=[],
            step=MagicMock(),
            task=_task(),  # type: ignore[arg-type]
            force_select=True,
            target_value="Choice",
        )

        assert isinstance(result.action_result, handler.ActionSuccess)
        assert result.value == "Choice"
        selected_element.click.assert_awaited_once()
        load_prompt.assert_not_called()
        custom_select_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_readback_scope_scanned_via_skyvernframe_only(self) -> None:
        matched_element = _FakeCustomElement()
        matched_element.get_locator().count = AsyncMock(return_value=0)
        captured: dict[str, object] = {}

        async def evaluate(*, frame: object, expression: str, arg: object = None) -> object:
            if "anchorIsComboboxInput" in expression:
                captured["expression"] = expression
            if "return { label," in expression:
                return None
            return False

        readback_scope_element = _FakeAnchorElement()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(handler.SkyvernFrame, "evaluate", AsyncMock(side_effect=evaluate))
            outcome = await _verify_custom_select_option(
                matched_element=matched_element,  # type: ignore[arg-type]
                readback_scope_element=readback_scope_element,  # type: ignore[arg-type]
                anchor_is_combobox_input=False,
                matched_element_id="matched-option",
                matched_label="Job Board",
            )

        assert outcome is False
        script = str(captured["expression"])
        assert script.count('"output"') == 0
        assert script.count("document.querySelectorAll") == 0
        assert script.count("scopeRoot.querySelectorAll") >= 1
        assert script.count("triggerSelector") >= 1
        assert script.count("aria-valuetext") >= 1
        assert script.count("data-selected-option-id") == 0
        assert script.count("data-selected-element-id") == 0
        assert script.count("data-skyvern-selected-id") == 0


class TestNoAvailableOptionFoundForCustomSelection:
    def test_message_includes_code_target_count_excerpt_and_reason(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(
            reason="not present in the list",
            target_value="Target Value",
            observed_options=["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"],
        )
        msg = str(exc)
        assert "code=OPTION_NOT_AVAILABLE" in msg
        assert "target_value='Target Value'" in msg
        assert "observed_options_count=6" in msg
        assert "['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo']" in msg
        assert "Foxtrot" not in msg  # excerpt is capped at 5
        assert "not present in the list" in msg

    def test_attributes_are_accessible_for_downstream_consumers(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(
            reason="not in dropdown",
            target_value="Target",
            observed_options=["Alpha", "Bravo"],
        )
        assert exc.code == "OPTION_NOT_AVAILABLE"
        assert exc.target_value == "Target"
        assert exc.observed_options_count == 2
        assert exc.observed_options_excerpt == ["Alpha", "Bravo"]
        assert exc.reason == "not in dropdown"

    def test_omits_optional_fields_when_not_supplied(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(reason=None)
        msg = str(exc)
        assert "code=OPTION_NOT_AVAILABLE" in msg
        assert "target_value" not in msg
        assert "observed_options_count" not in msg
        assert "observed_options_excerpt" not in msg
        assert exc.target_value is None
        assert exc.observed_options_count == 0
        assert exc.observed_options_excerpt == []

    def test_no_value_error_when_constructed_from_empty_no_match_payload(self) -> None:
        # Regression: previously ActionType("") fired on this payload before the
        # OPTION_NOT_AVAILABLE branch could run.
        json_response = {"action_type": "", "id": "", "reasoning": "not present", "relevant": False}
        try:
            raise NoAvailableOptionFoundForCustomSelection(
                reason=json_response["reasoning"],
                target_value="Anything",
                observed_options=["Alpha"],
            )
        except ValueError:
            pytest.fail("ValueError leaked from no-match exception construction")
        except NoAvailableOptionFoundForCustomSelection as exc:
            assert exc.code == "OPTION_NOT_AVAILABLE"


class TestNoMatchExceptionForDropdown:
    def test_returns_transient_when_no_options_and_fallback_id_given(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning="dropdown empty",
            target_value="Target",
            observed_options=[],
            transient_fallback_element_id="element-123",
        )
        assert isinstance(exc, NoIncrementalElementFoundForCustomSelection)
        assert "element-123" in str(exc)

    def test_returns_permanent_when_options_observed(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="Target",
            observed_options=["Alpha", "Bravo"],
            transient_fallback_element_id="element-123",
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.target_value == "Target"
        assert exc.observed_options_count == 2
        assert exc.observed_options_excerpt == ["Alpha", "Bravo"]
        assert exc.reason == "target not in list"

    def test_returns_permanent_when_no_options_but_no_fallback_id(self) -> None:
        # The emerging-element path passes None: an upstream guard handles the
        # zero-options case there, so this branch must surface as permanent.
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="Target",
            observed_options=[],
            transient_fallback_element_id=None,
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.observed_options_count == 0
        assert exc.observed_options_excerpt == []

    def test_normalizes_empty_target_value_to_none(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning=None,
            target_value="",
            observed_options=["Alpha"],
            transient_fallback_element_id=None,
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.target_value is None

    def test_native_select_populated_routes_to_permanent_not_transient(self) -> None:
        # Regression: a native <select> populated via element["options"]
        # must NOT be misread as zero-options and routed to the transient
        # exception. Walker first, then helper, end-to-end on the F-guard.
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "January", "value": "1"},
                    {"optionIndex": 1, "text": "February", "value": "2"},
                ],
            }
        ]
        observed = _collect_option_texts(tree)
        assert observed == ["January", "February"]
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="December",
            observed_options=observed,
            transient_fallback_element_id="select-element-id",
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.observed_options_count == 2


class TestSelectFromDropdownByValueNoMatch:
    @pytest.mark.asyncio
    async def test_returns_failure_when_no_dropdown_menu_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=None))

        result = await handler.select_from_dropdown_by_value(
            value="Missing",
            page=MagicMock(),
            skyvern_element=_FakeAnchorElement(),  # type: ignore[arg-type]
            skyvern_frame=MagicMock(),
            dom=MagicMock(),
            incremental_scraped=_FakeValueFallbackScrapePage(),  # type: ignore[arg-type]
            task=_task(),  # type: ignore[arg-type]
            step=MagicMock(),
        )

        assert isinstance(result, handler.ActionFailure)
        assert result.exception_type == NoElementMatchedForTargetOption.__name__
        assert "No value matched" in (result.exception_message or "")

    @pytest.mark.asyncio
    async def test_returns_failure_when_dropdown_cannot_scroll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dropdown_menu = _FakeDropdownMenuElement()
        skyvern_frame = MagicMock()
        skyvern_frame.get_element_scrollable = AsyncMock(return_value=False)

        monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=dropdown_menu))
        monkeypatch.setattr(handler, "try_to_find_potential_scrollable_element", AsyncMock(return_value=dropdown_menu))

        result = await handler.select_from_dropdown_by_value(
            value="Missing",
            page=MagicMock(),
            skyvern_element=_FakeAnchorElement(),  # type: ignore[arg-type]
            skyvern_frame=skyvern_frame,
            dom=MagicMock(),
            incremental_scraped=_FakeValueFallbackScrapePage(),  # type: ignore[arg-type]
            task=_task(),  # type: ignore[arg-type]
            step=MagicMock(),
        )

        assert isinstance(result, handler.ActionFailure)
        assert result.exception_type == NoElementMatchedForTargetOption.__name__
        assert "can't scroll" in (result.exception_message or "")

    @pytest.mark.asyncio
    async def test_returns_failure_after_scrolling_without_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dropdown_menu = _FakeDropdownMenuElement()
        skyvern_frame = MagicMock()
        skyvern_frame.get_element_scrollable = AsyncMock(return_value=True)
        scroll_down_to_load_all_options = AsyncMock()

        monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=dropdown_menu))
        monkeypatch.setattr(handler, "try_to_find_potential_scrollable_element", AsyncMock(return_value=dropdown_menu))
        monkeypatch.setattr(handler, "scroll_down_to_load_all_options", scroll_down_to_load_all_options)

        result = await handler.select_from_dropdown_by_value(
            value="Missing",
            page=MagicMock(),
            skyvern_element=_FakeAnchorElement(),  # type: ignore[arg-type]
            skyvern_frame=skyvern_frame,
            dom=MagicMock(),
            incremental_scraped=_FakeValueFallbackScrapePage(),  # type: ignore[arg-type]
            task=_task(),  # type: ignore[arg-type]
            step=MagicMock(),
        )

        assert isinstance(result, handler.ActionFailure)
        assert result.exception_type == NoElementMatchedForTargetOption.__name__
        assert "after scrolling" in (result.exception_message or "")
        scroll_down_to_load_all_options.assert_awaited_once()
