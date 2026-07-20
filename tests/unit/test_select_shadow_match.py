import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from zoneinfo import ZoneInfo

import pytest
import structlog.testing

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction, InputOrSelectContext, SelectOption, SelectOptionAction

_SHADOW_OPTION = {
    "id": "matched-id",
    "tagName": "div",
    "attributes": {"role": "option"},
    "text": "Committed Value",
    "interactable": True,
}


def _shadow_task() -> SimpleNamespace:
    return SimpleNamespace(
        navigation_goal="goal", navigation_payload={}, llm_key=None, organization_id=None, workflow_permanent_id=None
    )


class _FakeSelectLocator:
    def __init__(self) -> None:
        self.click = AsyncMock()
        self.select_option = AsyncMock()


class _FakeSelectElement:
    def __init__(self, options: list[dict[str, object]]) -> None:
        self._options = options
        self._locator = _FakeSelectLocator()

    async def get_attr(self, *_args: object, **_kwargs: object) -> str | None:
        return None

    async def refresh_select_options(self) -> tuple[list[dict[str, object]], str]:
        return self._options, ""

    def build_HTML(self) -> str:
        return "<select><option>stub</option></select>"

    def get_locator(self) -> _FakeSelectLocator:
        return self._locator

    def get_options(self) -> list[dict[str, object]]:
        return self._options


async def _run_normal_select_with_shadow_log(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_label: str,
    llm_response: dict[str, object],
    options: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    options = options or [
        {"optionIndex": 0, "text": "Years", "value": "years"},
        {"optionIndex": 1, "text": "Months", "value": "months"},
    ]
    action = SelectOptionAction(
        element_id="select-1",
        reasoning="choose duration",
        intention="duration",
        option=SelectOption(label=target_label),
        input_or_select_context=InputOrSelectContext(field="Duration", is_required=True),
    )
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(handler.app, "NORMAL_SELECT_AGENT_LLM_API_HANDLER", AsyncMock(return_value=llm_response))
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    context = SkyvernContext(tz_info=ZoneInfo("UTC"))
    with skyvern_context.scoped(context):
        with structlog.testing.capture_logs() as logs:
            results = await handler.normal_select(
                action=action,
                skyvern_element=_FakeSelectElement(options),
                task=SimpleNamespace(
                    navigation_goal="goal", navigation_payload={}, organization_id=None, workflow_permanent_id=None
                ),
                step=SimpleNamespace(step_id="step-1"),
                builder=SimpleNamespace(),
            )

    assert len(results) == 1
    return [log for log in logs if log.get("event") == "select_shadow_match"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_label", "llm_response", "expected_tier", "expected_agrees"),
    [
        ("Years", {"index": 0, "value": "years"}, "exact", True),
        ("Year", {"index": 1, "value": "months"}, "stem", False),
    ],
)
async def test_normal_select_logs_shadow_match_tier_and_llm_agreement(
    monkeypatch: pytest.MonkeyPatch,
    target_label: str,
    llm_response: dict[str, object],
    expected_tier: str,
    expected_agrees: bool,
) -> None:
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label=target_label,
        llm_response=llm_response,
    )

    assert len(logs) == 1
    assert logs[0]["prompt_name"] == "normal-select"
    assert logs[0]["option_count"] == 2
    assert logs[0]["match_tier"] == expected_tier
    assert logs[0]["match_found"] is True
    assert logs[0]["match_agrees_with_llm"] is expected_agrees


@pytest.mark.asyncio
async def test_normal_select_shadow_disagreement_emits_rich_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label="Years",
        llm_response={"index": 1, "value": "months"},
    )

    assert len(logs) == 1
    event = logs[0]
    assert event["match_found"] is True
    assert event["match_agrees_with_llm"] is False
    assert event["target_value"] == "Years"
    assert event["matched_index"] == 0
    assert event["matched_label"] == "Years"
    assert event["matched_value"] == "years"
    assert event["llm_index"] == 1
    assert event["llm_value"] == "months"
    assert event["normalized_target_value"] == "years"
    assert event["normalized_matched_label"] == "years"
    assert event["normalized_matched_value"] == "years"
    assert event["normalized_llm_value"] == "months"
    assert "matched_element_id" not in event
    assert "llm_element_id" not in event


@pytest.mark.asyncio
async def test_normal_select_agreement_stays_lean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label="Years",
        llm_response={"index": 0, "value": "years"},
    )

    assert len(logs) == 1
    event = logs[0]
    assert event["match_found"] is True
    assert event["match_agrees_with_llm"] is True
    for lean_only in (
        "target_value",
        "matched_index",
        "matched_label",
        "matched_value",
        "matched_element_id",
        "llm_index",
        "llm_value",
        "llm_element_id",
        "normalized_target_value",
        "normalized_matched_label",
        "normalized_matched_value",
        "normalized_llm_value",
    ):
        assert lean_only not in event


@pytest.mark.asyncio
async def test_normal_select_disagreement_truncates_free_text_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_target = "  " + "A’   B " * 40
    long_option = "  " + "C’   D " * 40
    long_llm_value = "  " + "E’   F " * 40
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label=long_target,
        llm_response={"index": 1, "value": long_llm_value},
        options=[
            {"optionIndex": 0, "text": long_target, "value": long_option},
            {"optionIndex": 1, "text": "Other", "value": "other"},
        ],
    )

    assert len(logs) == 1
    event = logs[0]
    bound = handler.SELECT_SHADOW_MATCH_FIELD_MAX_CHARS
    assert event["match_agrees_with_llm"] is False
    for field in ("target_value", "matched_label", "matched_value", "llm_value"):
        value = event[field]
        assert isinstance(value, str)
        assert len(value) == bound + 1
        assert value.endswith("…")
    for field, raw_value in (
        ("normalized_target_value", long_target),
        ("normalized_matched_label", long_target),
        ("normalized_matched_value", long_option),
        ("normalized_llm_value", long_llm_value),
    ):
        assert event[field] == handler._truncate_select_shadow_field(handler._normalize_select_shadow_text(raw_value))


@pytest.mark.asyncio
async def test_normal_select_shadow_match_keeps_blank_placeholder_index_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label="Years",
        llm_response={"index": 1, "value": "years"},
        options=[
            {"optionIndex": 0, "text": "", "value": ""},
            {"optionIndex": 1, "text": "Years", "value": "years"},
        ],
    )

    assert len(logs) == 1
    assert logs[0]["option_count"] == 2
    assert logs[0]["match_tier"] == "exact"
    assert logs[0]["match_found"] is True
    assert logs[0]["match_agrees_with_llm"] is True


def test_shadow_match_disabled_skips_candidate_work(monkeypatch: pytest.MonkeyPatch) -> None:
    get_candidates = Mock(side_effect=AssertionError("should not build shadow candidates"))
    agreement = Mock()
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", False)

    with structlog.testing.capture_logs() as logs:
        handler._log_select_shadow_match(
            prompt_name="normal-select",
            target_value="Years",
            get_candidates=get_candidates,
            agreement=agreement,
        )

    get_candidates.assert_not_called()
    agreement.assert_not_called()
    assert [log for log in logs if log.get("event") == "select_shadow_match"] == []


def test_shadow_match_candidate_errors_do_not_escape_live_path(monkeypatch: pytest.MonkeyPatch) -> None:
    get_candidates = Mock(side_effect=RuntimeError("extractor failed"))
    agreement = Mock()
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    handler._log_select_shadow_match(
        prompt_name="normal-select",
        target_value="Years",
        get_candidates=get_candidates,
        agreement=agreement,
    )

    get_candidates.assert_called_once()
    agreement.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_llm_values_are_coerced_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = await _run_normal_select_with_shadow_log(
        monkeypatch,
        target_label="Years",
        llm_response={"index": 1, "value": 2024},
    )

    assert len(logs) == 1
    event = logs[0]
    assert event["match_agrees_with_llm"] is False
    assert event["llm_value"] == "2024"


def test_select_shadow_agreement_coerces_malformed_llm_fields() -> None:
    agreement = handler.SelectShadowAgreement(
        agrees=None,
        llm_index="2",
        llm_value=2024,
        llm_element_id=97,
    )
    assert agreement.llm_index == 2
    assert agreement.llm_value == "2024"
    assert agreement.llm_element_id == "97"

    non_numeric_index = handler.SelectShadowAgreement(agrees=None, llm_index="not-a-number")
    assert non_numeric_index.llm_index is None


def test_select_shadow_match_normalizes_curly_apostrophes() -> None:
    matched_index, tier = handler.classify_option_match("Worker’s Compensation", ["Workers Compensation"])

    assert matched_index == 0
    assert tier == "exact"


@pytest.mark.parametrize(
    ("matched_index", "llm_element_id", "llm_value", "expected_agrees"),
    [
        pytest.param(0, "other-id", "  COMMITTED   VALUE  ", True, id="value-match-overrides-different-id"),
        pytest.param(0, "matched-id", "paraphrased value", False, id="value-decides-despite-matching-id"),
        pytest.param(0, "other-id", "different value", False, id="different-value-and-id"),
        pytest.param(0, "matched-id", None, None, id="id-never-decides-without-value"),
        pytest.param(0, "other-id", None, None, id="differing-ids-unjudgeable-without-value"),
        pytest.param(0, None, None, None, id="no-signals"),
        pytest.param(0, None, " ‘ ` ’ ", None, id="normalized-empty-value-is-unavailable"),
        pytest.param(None, "matched-id", "committed value", False, id="no-matcher-choice"),
        pytest.param(1, "matched-id", "committed value", None, id="matcher-index-out-of-range"),
    ],
)
def test_element_choice_shadow_agreement_uses_available_value_and_id_signals(
    matched_index: int | None,
    llm_element_id: str | None,
    llm_value: str | None,
    expected_agrees: bool | None,
) -> None:
    candidates: list[dict[str, str | None]] = [
        {"label": "Display Label", "value": "committed value", "element_id": "matched-id"}
    ]

    agreement = handler._select_shadow_agrees_with_element_choice(
        candidates,
        matched_index,
        llm_element_id=llm_element_id,
        llm_value=llm_value,
    )

    assert agreement.agrees is expected_agrees


def test_element_choice_shadow_agreement_uses_exact_normalizer() -> None:
    agreement = handler._select_shadow_agrees_with_element_choice(
        [{"label": "Worker’s   Compensation", "value": None, "element_id": "matched-id"}],
        0,
        llm_element_id="other-id",
        llm_value="  WORKER'S\tCOMPENSATION ",
    )

    assert agreement.agrees is True


@pytest.mark.parametrize(
    ("matched_index", "llm_index", "llm_value", "expected_agrees"),
    [
        pytest.param(0, 1, "same value", True, id="value-match-overrides-different-index"),
        pytest.param(0, 0, "paraphrased value", False, id="value-decides-despite-matching-index"),
        pytest.param(0, 1, "different value", False, id="different-value-and-index"),
        pytest.param(0, None, None, None, id="no-signals"),
        pytest.param(0, -1, None, False, id="llm-abstained"),
        pytest.param(None, 0, "same value", False, id="no-matcher-choice"),
        pytest.param(2, 2, "same value", None, id="invalid-candidate-index-with-value-stays-unjudged"),
    ],
)
def test_native_choice_shadow_agreement_uses_available_value_and_index_signals(
    matched_index: int | None,
    llm_index: int | None,
    llm_value: str | None,
    expected_agrees: bool | None,
) -> None:
    candidates: list[dict[str, str | None]] = [
        {"label": "Duplicate", "value": "same value"},
        {"label": "Duplicate", "value": "same value"},
    ]

    agreement = handler._select_shadow_agrees_with_native_choice(
        candidates,
        matched_index,
        llm_index=llm_index,
        llm_value=llm_value,
    )

    assert agreement.agrees is expected_agrees


def test_custom_select_same_value_different_element_id_logs_agreement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    with structlog.testing.capture_logs() as logs:
        handler._log_select_shadow_match(
            prompt_name="custom-select/dropdown",
            target_value="Workers' Compensation",
            get_candidates=lambda: [{"label": "Worker’s   Compensation", "value": None, "element_id": "el-A"}],
            agreement=lambda candidates, matched_index: handler._select_shadow_agrees_with_element_choice(
                candidates,
                matched_index,
                llm_element_id="el-B",
                llm_value="workers compensation",
            ),
        )

    (event,) = (entry for entry in logs if entry.get("event") == "select_shadow_match")
    assert event["match_tier"] == "exact"
    assert event["match_found"] is True
    assert event["match_agrees_with_llm"] is True
    assert "matched_element_id" not in event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_value", "expected_agrees"),
    [
        pytest.param("  committed   value  ", True, id="normalized-value-match"),
        pytest.param("Different Value", False, id="value-mismatch"),
    ],
)
async def test_auto_completion_call_path_forwards_committed_value_to_shadow_match(
    monkeypatch: pytest.MonkeyPatch,
    llm_value: str,
    expected_agrees: bool,
) -> None:
    frame = MagicMock()
    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())
    incremental_scraped = MagicMock(
        start_listen_dom_increment=AsyncMock(),
        stop_listen_dom_increment=AsyncMock(),
        get_incremental_elements_num=AsyncMock(return_value=1),
        get_incremental_element_tree=AsyncMock(return_value=[_SHADOW_OPTION]),
    )
    incremental_scraped.build_html_tree.return_value = "<div>Committed Value</div>"
    skyvern_element = MagicMock(
        get_frame=Mock(return_value=frame),
        get_element_handler=AsyncMock(return_value=MagicMock()),
        press_fill=AsyncMock(),
        press_key=AsyncMock(),
        is_visible=AsyncMock(return_value=True),
        input_clear=AsyncMock(),
    )
    monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=skyvern_frame))
    monkeypatch.setattr(handler, "IncrementalScrapePage", Mock(return_value=incremental_scraped))
    monkeypatch.setattr(handler, "get_slim_output_template_value", AsyncMock(return_value=""))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.app,
        "AUTO_COMPLETION_LLM_API_HANDLER",
        AsyncMock(
            return_value={
                "id": "different-id",
                "value": llm_value,
                "direct_searching": True,
            }
        ),
    )
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with structlog.testing.capture_logs() as logs:
            await handler.choose_auto_completion_dropdown(
                context=InputOrSelectContext(field="Field", is_search_bar=False),
                page=MagicMock(),
                scraped_page=MagicMock(),
                dom=MagicMock(),
                text="Committed Value",
                skyvern_element=skyvern_element,
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )

    events = [log for log in logs if log.get("event") == "select_shadow_match"]
    assert len(events) == 1
    event = events[0]
    assert event["prompt_name"] == "auto-completion-choose-option"
    assert event["match_agrees_with_llm"] is expected_agrees
    if not expected_agrees:
        assert event["normalized_matched_label"] == "committed value"
        assert event["normalized_llm_value"] == "different value"


@pytest.mark.asyncio
async def test_emerging_select_call_path_forwards_committed_value_to_shadow_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_element = MagicMock(
        get_attr=AsyncMock(return_value=None),
        scroll_into_view=AsyncMock(),
        click=AsyncMock(),
    )
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=selected_element))
    llm_handler = AsyncMock(return_value={"id": "matched-id", "value": "  committed   value  ", "action_type": "click"})
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div>Committed Value</div>"))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    scraped_page = SimpleNamespace(id_to_css_dict={})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={"matched-id": "[data-id=matched-id]"},
        element_tree_trimmed=[_SHADOW_OPTION],
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with structlog.testing.capture_logs() as logs:
            await handler.select_from_emerging_elements(
                current_element_id="field-id",
                options=handler.CustomSelectPromptOptions(target_value="Committed Value"),
                page=MagicMock(),
                scraped_page=scraped_page,
                scraped_page_after_open=scraped_page_after_open,
                new_interactable_element_ids=["matched-id"],
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )

    events = [log for log in logs if log.get("event") == "select_shadow_match"]
    assert len(events) == 1
    assert events[0]["prompt_name"] == "custom-select/emerging"
    assert events[0]["match_agrees_with_llm"] is True


@pytest.mark.asyncio
async def test_emerging_select_falls_back_to_visible_controlled_listbox_when_no_ids_are_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = {
        "id": "field-id",
        "tagName": "button",
        "attributes": {
            "aria-controls": "menu-dom-id",
            "aria-expanded": "true",
            "role": "combobox",
        },
        "interactable": True,
        "children": [],
    }
    listbox = {
        "id": "menu-id",
        "tagName": "div",
        "attributes": {"id": "menu-dom-id", "role": "listbox"},
        "children": [
            {
                "id": "option-a",
                "tagName": "button",
                "attributes": {"role": "option"},
                "text": "Alpha",
                "interactable": True,
                "children": [],
            },
            {
                "id": "option-hidden",
                "tagName": "button",
                "attributes": {"role": "option"},
                "text": "Beta",
                "interactable": False,
                "children": [],
            },
            {
                "id": "option-b",
                "tagName": "button",
                "attributes": {"role": "option"},
                "text": "Beta",
                "interactable": True,
                "children": [],
            },
        ],
    }
    tree = [anchor, listbox]
    ids = {
        "field-id": "[unique_id=field-id]",
        "menu-id": "[unique_id=menu-id]",
        "option-a": "[unique_id=option-a]",
        "option-hidden": "[unique_id=option-hidden]",
        "option-b": "[unique_id=option-b]",
    }

    def element_for_id(element_id: str) -> MagicMock:
        return MagicMock(
            is_interactable=Mock(return_value=element_id.startswith("option-") and element_id != "option-hidden")
        )

    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock(side_effect=element_for_id))

    async def select_deterministically(**kwargs: object) -> tuple[handler.ActionSuccess, str]:
        get_option_candidates = kwargs["get_option_candidates"]
        assert callable(get_option_candidates)
        assert [candidate["label"] for candidate in get_option_candidates()] == ["Alpha", "Beta"]
        return handler.ActionSuccess(), "Beta"

    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    deterministic_select = AsyncMock(side_effect=select_deterministically)
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", deterministic_select)
    llm_handler = AsyncMock()
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    scraped_page = SimpleNamespace(id_to_css_dict=ids)
    scraped_page_after_open = SimpleNamespace(id_to_css_dict=ids, element_tree=tree, element_tree_trimmed=tree)
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        result = await handler.select_from_emerging_elements(
            current_element_id="field-id",
            options=handler.CustomSelectPromptOptions(target_value="Beta"),
            page=MagicMock(),
            scraped_page=scraped_page,
            scraped_page_after_open=scraped_page_after_open,
            step=SimpleNamespace(step_id="step-1"),
            task=_shadow_task(),
        )

    assert isinstance(result, handler.ActionSuccess)
    deterministic_select.assert_awaited_once()
    llm_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_emerging_select_fallback_resolves_owned_listbox_when_trim_strips_ownership_attrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A custom combobox whose listbox is linked by aria-controls. trim_element_tree drops
    # aria-controls / aria-owns / the DOM id from the trimmed tree, so ownership must be resolved
    # from the untrimmed element_tree — resolving it from the trimmed tree leaves the fallback inert.
    anchor = {
        "id": "field-id",
        "tagName": "button",
        "attributes": {"aria-controls": "menu-dom-id", "aria-expanded": "true", "role": "combobox"},
        "interactable": True,
        "children": [],
    }
    listbox = {
        "id": "menu-id",
        "tagName": "div",
        "attributes": {"id": "menu-dom-id", "role": "listbox"},
        "children": [
            {
                "id": "option-a",
                "tagName": "button",
                "attributes": {"role": "option"},
                "text": "Alpha",
                "interactable": True,
                "children": [],
            },
            {
                "id": "option-b",
                "tagName": "button",
                "attributes": {"role": "option"},
                "text": "Beta",
                "interactable": True,
                "children": [],
            },
        ],
    }
    element_tree = [anchor, listbox]
    element_tree_trimmed = handler.trim_element_tree(copy.deepcopy(element_tree))
    # Guard the premise: trim really removes the ownership signal the fallback keys off.
    assert "aria-controls" not in (element_tree_trimmed[0].get("attributes") or {})
    assert "id" not in (element_tree_trimmed[1].get("attributes") or {})

    ids = {
        "field-id": "[unique_id=field-id]",
        "menu-id": "[unique_id=menu-id]",
        "option-a": "[unique_id=option-a]",
        "option-b": "[unique_id=option-b]",
    }

    def element_for_id(element_id: str) -> MagicMock:
        return MagicMock(is_interactable=Mock(return_value=element_id.startswith("option-")))

    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock(side_effect=element_for_id))

    async def select_deterministically(**kwargs: object) -> tuple[handler.ActionSuccess, str]:
        get_option_candidates = kwargs["get_option_candidates"]
        assert callable(get_option_candidates)
        assert [candidate["label"] for candidate in get_option_candidates()] == ["Alpha", "Beta"]
        return handler.ActionSuccess(), "Beta"

    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    deterministic_select = AsyncMock(side_effect=select_deterministically)
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", deterministic_select)
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=AsyncMock()),
    )

    scraped_page = SimpleNamespace(id_to_css_dict=ids)
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict=ids,
        element_tree=element_tree,
        element_tree_trimmed=element_tree_trimmed,
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        result = await handler.select_from_emerging_elements(
            current_element_id="field-id",
            options=handler.CustomSelectPromptOptions(target_value="Beta"),
            page=MagicMock(),
            scraped_page=scraped_page,
            scraped_page_after_open=scraped_page_after_open,
            step=SimpleNamespace(step_id="step-1"),
            task=_shadow_task(),
        )

    assert isinstance(result, handler.ActionSuccess)
    deterministic_select.assert_awaited_once()


@pytest.mark.asyncio
async def test_emerging_select_keeps_off_list_candidate_for_deterministic_match_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Option nodes in custom widgets are often not flagged interactable. With the verify flag off we
    # must not pre-filter deterministic candidates to the interactable set (main's behavior) — dropping
    # them there would regress selections that used to succeed.
    tree = [
        {
            "id": "option-x",
            "tagName": "div",
            "attributes": {"role": "option"},
            "text": "Alpha",
            "interactable": True,
            "children": [],
        },
        {
            "id": "option-y",
            "tagName": "div",
            "attributes": {"role": "option"},
            "text": "Beta",
            "children": [],
        },
    ]
    monkeypatch.setattr(handler, "_is_verify_emerging_select_pick_enabled", AsyncMock(return_value=False))
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock())
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div role='option'>option</div>"))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    llm_handler = AsyncMock()
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    async def select_deterministically(**kwargs: object) -> tuple[handler.ActionSuccess, str]:
        get_option_candidates = kwargs["get_option_candidates"]
        assert callable(get_option_candidates)
        assert [candidate["element_id"] for candidate in get_option_candidates()] == ["option-x", "option-y"]
        return handler.ActionSuccess(), "Beta"

    deterministic_select = AsyncMock(side_effect=select_deterministically)
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", deterministic_select)

    scraped_page = SimpleNamespace(id_to_css_dict={})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={"option-x": "[unique_id=option-x]", "option-y": "[unique_id=option-y]"},
        element_tree_trimmed=tree,
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        result = await handler.select_from_emerging_elements(
            current_element_id="field-id",
            options=handler.CustomSelectPromptOptions(target_value="Beta"),
            page=MagicMock(),
            scraped_page=scraped_page,
            scraped_page_after_open=scraped_page_after_open,
            new_interactable_element_ids=["option-x"],
            step=SimpleNamespace(step_id="step-1"),
            task=_shadow_task(),
        )

    assert isinstance(result, handler.ActionSuccess)
    deterministic_select.assert_awaited_once()
    llm_handler.assert_not_awaited()


@pytest.mark.parametrize(("anchor_expanded", "expected"), [(False, []), (True, ["listbox-id"])])
def test_custom_select_fallback_requires_open_anchor_for_sole_listbox(
    anchor_expanded: bool,
    expected: list[str],
) -> None:
    tree = [
        {
            "id": "field-id",
            "tagName": "button",
            "attributes": {"aria-expanded": str(anchor_expanded).lower()},
            "children": [],
        },
        {
            "id": "listbox-id",
            "tagName": "div",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "id": "option-id",
                    "tagName": "button",
                    "attributes": {"role": "option"},
                    "text": "Alpha",
                    "children": [],
                }
            ],
        },
    ]

    result = handler._custom_select_fallback_subtrees(tree, "field-id")

    assert [element["id"] for element in result] == expected


def test_custom_select_fallback_accepts_sole_listbox_for_expanded_combobox_ancestor() -> None:
    tree = [
        {
            "id": "combobox-id",
            "tagName": "div",
            "attributes": {"aria-expanded": "true", "role": "combobox"},
            "children": [
                {
                    "id": "field-id",
                    "tagName": "input",
                    "attributes": {},
                    "children": [],
                }
            ],
        },
        {
            "id": "listbox-id",
            "tagName": "div",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "id": "option-id",
                    "tagName": "button",
                    "attributes": {"role": "option"},
                    "text": "Alpha",
                    "children": [],
                }
            ],
        },
    ]

    result = handler._custom_select_fallback_subtrees(tree, "field-id")

    assert [element["id"] for element in result] == ["listbox-id"]


@pytest.mark.asyncio
async def test_emerging_select_rejects_page_global_options_without_owned_listbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = [
        {
            "id": "field-id",
            "tagName": "button",
            "attributes": {"aria-expanded": "true", "role": "combobox"},
            "children": [],
        },
        {
            "id": "unrelated-option",
            "tagName": "button",
            "attributes": {"role": "option"},
            "text": "Wrong field",
            "children": [],
        },
    ]
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock())
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    scraped_page = SimpleNamespace(id_to_css_dict={"field-id": "[unique_id=field-id]"})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={"field-id": "[unique_id=field-id]"},
        element_tree=tree,
        element_tree_trimmed=tree,
    )

    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with pytest.raises(handler.NoIncrementalElementFoundForCustomSelection):
            await handler.select_from_emerging_elements(
                current_element_id="field-id",
                options=handler.CustomSelectPromptOptions(target_value="Wrong field"),
                page=MagicMock(),
                scraped_page=scraped_page,
                scraped_page_after_open=scraped_page_after_open,
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )


@pytest.mark.asyncio
async def test_emerging_select_rejects_hidden_candidate_from_deterministic_and_llm_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = [
        {
            "id": "hidden-id",
            "tagName": "button",
            "attributes": {"role": "option"},
            "text": "Exact target",
            "children": [],
        },
        {
            "id": "visible-id",
            "tagName": "button",
            "attributes": {"role": "option"},
            "text": "Visible alternative",
            "children": [],
        },
    ]
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock())
    monkeypatch.setattr(handler, "_is_verify_emerging_select_pick_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<button role='option'>option</button>"))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    llm_handler = AsyncMock(return_value={"id": "hidden-id", "value": "Exact target", "action_type": "click"})
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    async def select_deterministically(**kwargs: object) -> None:
        get_option_candidates = kwargs["get_option_candidates"]
        assert callable(get_option_candidates)
        assert [candidate["element_id"] for candidate in get_option_candidates()] == ["visible-id"]
        return None

    monkeypatch.setattr(
        handler,
        "_select_deterministic_custom_option",
        AsyncMock(side_effect=select_deterministically),
    )
    scraped_page = SimpleNamespace(id_to_css_dict={})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={
            "hidden-id": "[unique_id=hidden-id]",
            "visible-id": "[unique_id=visible-id]",
        },
        element_tree_trimmed=tree,
    )

    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with pytest.raises(handler.NoAvailableOptionFoundForCustomSelection):
            await handler.select_from_emerging_elements(
                current_element_id="field-id",
                options=handler.CustomSelectPromptOptions(target_value="Exact target"),
                page=MagicMock(),
                scraped_page=scraped_page,
                scraped_page_after_open=scraped_page_after_open,
                new_interactable_element_ids=["visible-id"],
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )

    dom_after_open.get_skyvern_element_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_emerging_select_allows_input_text_on_anchor_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_input = MagicMock(
        scroll_into_view=AsyncMock(),
        get_tag_name=Mock(return_value="input"),
        get_locator=Mock(return_value=MagicMock()),
        is_readonly=AsyncMock(return_value=False),
        input_clear=AsyncMock(),
        input_sequentially=AsyncMock(),
    )
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=anchor_input))
    llm_handler = AsyncMock(return_value={"id": "field-id", "value": "filter text", "action_type": "input_text"})
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div>Option</div>"))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler, "get_input_value", AsyncMock(return_value=""))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    scraped_page = SimpleNamespace(id_to_css_dict={"field-id": "[unique_id=field-id]"})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={
            "field-id": "[unique_id=field-id]",
            "option-x": "[unique_id=option-x]",
        },
        element_tree_trimmed=[
            {
                "id": "option-x",
                "tagName": "div",
                "attributes": {"role": "option"},
                "text": "Other Option",
                "interactable": True,
            }
        ],
    )
    task = SimpleNamespace(
        navigation_goal="goal",
        navigation_payload={},
        llm_key=None,
        workflow_run_id=None,
        organization_id=None,
        workflow_permanent_id=None,
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        result = await handler.select_from_emerging_elements(
            current_element_id="field-id",
            options=handler.CustomSelectPromptOptions(target_value="filter text"),
            page=MagicMock(),
            scraped_page=scraped_page,
            scraped_page_after_open=scraped_page_after_open,
            new_interactable_element_ids=["option-x"],
            step=SimpleNamespace(step_id="step-1"),
            task=task,
        )

    assert isinstance(result, handler.ActionSuccess)
    anchor_input.input_clear.assert_awaited_once()
    anchor_input.input_sequentially.assert_awaited_once_with("filter text")


@pytest.mark.asyncio
async def test_emerging_select_rejects_input_text_on_element_outside_anchor_and_new_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock())
    llm_handler = AsyncMock(return_value={"id": "stranger-id", "value": "filter text", "action_type": "input_text"})
    monkeypatch.setattr(handler, "_is_verify_emerging_select_pick_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div>Option</div>"))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    scraped_page = SimpleNamespace(id_to_css_dict={"field-id": "[unique_id=field-id]"})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={
            "field-id": "[unique_id=field-id]",
            "option-x": "[unique_id=option-x]",
        },
        element_tree_trimmed=[
            {
                "id": "option-x",
                "tagName": "div",
                "attributes": {"role": "option"},
                "text": "Other Option",
                "interactable": True,
            }
        ],
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with pytest.raises(handler.NoAvailableOptionFoundForCustomSelection):
            await handler.select_from_emerging_elements(
                current_element_id="field-id",
                options=handler.CustomSelectPromptOptions(target_value="filter text"),
                page=MagicMock(),
                scraped_page=scraped_page,
                scraped_page_after_open=scraped_page_after_open,
                new_interactable_element_ids=["option-x"],
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )

    dom_after_open.get_skyvern_element_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_emerging_select_off_list_pick_still_proceeds_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flag off keeps today's behavior: an off-list pick is still acted on rather than rejected, so
    # merging this cannot change any org's outcomes until the flag is ramped.
    selected_element = MagicMock(
        get_tag_name=Mock(return_value="div"),
        get_attr=AsyncMock(return_value=None),
        scroll_into_view=AsyncMock(),
        click=AsyncMock(),
    )
    dom_after_open = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=selected_element))
    llm_handler = AsyncMock(return_value={"id": "stranger-id", "value": None, "action_type": "click"})
    monkeypatch.setattr(handler, "_is_verify_emerging_select_pick_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div>Option</div>"))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
    monkeypatch.setattr(handler.app, "CUSTOM_SELECT_AGENT_LLM_API_HANDLER", llm_handler)

    scraped_page = SimpleNamespace(id_to_css_dict={"field-id": "[unique_id=field-id]"})
    scraped_page_after_open = SimpleNamespace(
        id_to_css_dict={
            "field-id": "[unique_id=field-id]",
            "option-x": "[unique_id=option-x]",
        },
        element_tree_trimmed=[
            {
                "id": "option-x",
                "tagName": "div",
                "attributes": {"role": "option"},
                "text": "Other Option",
                "interactable": True,
            }
        ],
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with structlog.testing.capture_logs() as logs:
            result = await handler.select_from_emerging_elements(
                current_element_id="field-id",
                options=handler.CustomSelectPromptOptions(target_value="filter text"),
                page=MagicMock(),
                scraped_page=scraped_page,
                scraped_page_after_open=scraped_page_after_open,
                new_interactable_element_ids=["option-x"],
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
            )

    assert isinstance(result, handler.ActionSuccess)
    selected_element.click.assert_awaited()
    # Flag off still emits telemetry so the reject rate is measurable before ramping the flag on.
    off_list_warnings = [
        log for log in logs if log.get("log_level") == "warning" and log.get("element_id") == "stranger-id"
    ]
    assert off_list_warnings


@pytest.mark.asyncio
async def test_dropdown_select_call_path_forwards_committed_value_to_shadow_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incremental_scraped = MagicMock(
        get_incremental_element_tree=AsyncMock(return_value=[_SHADOW_OPTION]),
    )
    incremental_scraped.build_element_tree.return_value = "<div>Committed Value</div>"
    selected_element = MagicMock(
        get_tag_name=Mock(return_value="div"),
        get_attr=AsyncMock(return_value=None),
        scroll_into_view=AsyncMock(),
        click=AsyncMock(),
    )
    monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=None))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.app,
        "CUSTOM_SELECT_AGENT_LLM_API_HANDLER",
        AsyncMock(return_value={"id": "different-id", "value": "  committed   value  ", "action_type": "click"}),
    )
    monkeypatch.setattr(handler.SkyvernElement, "create_from_incremental", AsyncMock(return_value=selected_element))
    monkeypatch.setattr(handler.settings, "SKYVERN_SELECT_SHADOW_MATCH", True)

    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with structlog.testing.capture_logs() as logs:
            await handler.select_from_dropdown(
                context=InputOrSelectContext(field="Field", is_required=True),
                page=MagicMock(),
                skyvern_element=MagicMock(get_id=Mock(return_value="field-id")),
                skyvern_frame=MagicMock(),
                incremental_scraped=incremental_scraped,
                check_filter_funcs=[],
                step=SimpleNamespace(step_id="step-1"),
                task=_shadow_task(),
                force_select=True,
                target_value="Committed Value",
            )

    events = [log for log in logs if log.get("event") == "select_shadow_match"]
    assert len(events) == 1
    assert events[0]["prompt_name"] == "custom-select/dropdown"
    assert events[0]["match_agrees_with_llm"] is True


_OUTCOME_TASK = SimpleNamespace(
    workflow_run_id="wr",
    task_id="task",
    organization_id="org",
    url="https://test",
    navigation_goal="goal",
    navigation_payload={},
    llm_key=None,
    workflow_permanent_id=None,
)


async def _run_outcome_case(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    *,
    family_enabled: bool = True,
    assigned: bool = False,
    entry_action_type: str = "select_option",
    selection_group_id: str = "group-1",
    select_depth: int = 0,
) -> tuple[object, dict[str, object], SimpleNamespace, AsyncMock]:
    handler._COLLAPSE_XP_ASSIGNMENT_MEMO.clear()
    family = (
        AsyncMock(side_effect=RuntimeError("gate")) if case == "gate_error" else AsyncMock(return_value=family_enabled)
    )
    provider = SimpleNamespace(
        is_feature_enabled_cached=family,
        resolve_feature_enabled_unrecorded=AsyncMock(return_value=assigned),
    )
    resolution = SimpleNamespace(
        fallback_to_llm=case == "no_match",
        matched_index=None if case == "no_match" else 2 if case == "bad_index" else 0,
        matched_label="Choice",
        matched_tier=None if case == "no_match" else "exact",
    )
    resolver = AsyncMock(side_effect=RuntimeError("matcher") if case == "matcher_error" else None)
    resolver.return_value = resolution
    selected = SimpleNamespace(
        get_attr=AsyncMock(return_value="listbox" if case == "listbox" else None),
        scroll_into_view=AsyncMock(),
        click=AsyncMock(side_effect=RuntimeError("after dispatch") if case == "post_click" else None),
    )
    monkeypatch.setattr(handler.app, "EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(handler.app, "AGENT_FUNCTION", SimpleNamespace(resolve_field_option=resolver))
    monkeypatch.setattr(
        handler,
        "_read_custom_select_matched_state",
        AsyncMock(side_effect=RuntimeError("before click") if case == "pre_click" else None, return_value=None),
    )
    monkeypatch.setattr(handler, "_resolve_custom_select_readback_scope_element", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(handler, "_anchor_is_combobox_input", AsyncMock(return_value=case.startswith("reset")))
    monkeypatch.setattr(
        handler, "_custom_select_matched_state_confirms_pre_click", Mock(return_value=case == "precommit")
    )
    monkeypatch.setattr(handler, "_custom_select_scope_confirms_committed", AsyncMock(return_value=False))
    monkeypatch.setattr(handler, "_verify_custom_select_option_with_settle", AsyncMock(return_value=case == "verified"))
    monkeypatch.setattr(handler, "_reset_custom_select_combobox_input", AsyncMock(return_value=case == "reset_ok"))
    candidates = (
        Mock(side_effect=RuntimeError("walker"))
        if case == "walker_error"
        else Mock(
            return_value=[
                {
                    "label": "Choice",
                    "element_id": None if case == "no_element_id" else "choice-1",
                    "value": "choice",
                    "is_choice_input": case == "toggle",
                }
            ]
        )
    )
    with structlog.testing.capture_logs() as logs:
        result = await handler._select_deterministic_custom_option(
            target_value="Choice",
            get_option_candidates=candidates,
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(return_value=selected),
            task=_OUTCOME_TASK,
            step=SimpleNamespace(step_id="step"),
            entry_action_type=entry_action_type,
            selection_group_id=selection_group_id,
            select_depth=select_depth,
        )
    events = [log for log in logs if log.get("event") == "custom_select_family_outcome"]
    assert len(events) == 1
    return result, events[0], provider, resolver


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_action_type", ["select_option", "input_text", "input_text_converted"])
async def test_outcome_event_emitted_once_per_opportunity(
    monkeypatch: pytest.MonkeyPatch, entry_action_type: str
) -> None:
    _, event, _, _ = await _run_outcome_case(monkeypatch, "control", entry_action_type=entry_action_type)
    assert event["outcome"] == "llm_fallback_control"
    assert event["entry_action_type"] == entry_action_type


@pytest.mark.asyncio
async def test_click_route_without_target_emits_nothing_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = AsyncMock()
    monkeypatch.setattr(handler, "_resolve_collapse_gate", gate)

    with structlog.testing.capture_logs() as logs:
        result = await handler._select_deterministic_custom_option(
            target_value=None,
            get_option_candidates=Mock(),
            field_context={},
            page=MagicMock(),
            get_skyvern_element=AsyncMock(),
            task=_OUTCOME_TASK,
            entry_action_type="click",
        )

    assert result is None
    assert [log for log in logs if log.get("event") == "custom_select_family_outcome"] == []
    gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_click_ingress_routes_no_target_and_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    select_from_emerging_elements = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(handler, "select_from_emerging_elements", select_from_emerging_elements)
    monkeypatch.setattr(handler, "_build_after_click_verify_prompt", AsyncMock(return_value="prompt"))
    monkeypatch.setattr(
        handler,
        "resolve_check_user_goal_handler",
        AsyncMock(
            return_value=AsyncMock(
                return_value={"thoughts": "continue", "user_goal_achieved": False, "should_terminate": False}
            )
        ),
    )
    monkeypatch.setattr(handler, "locate_dropdown_menu", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        handler,
        "_get_input_or_select_context",
        AsyncMock(return_value=InputOrSelectContext(field="Field", is_required=True, is_date_related=False)),
    )
    monkeypatch.setattr(
        handler,
        "DomUtil",
        Mock(
            return_value=SimpleNamespace(
                get_skyvern_element_by_id=AsyncMock(
                    return_value=SimpleNamespace(is_interactable=Mock(return_value=True))
                )
            )
        ),
    )

    scraped_page_after_open = SimpleNamespace(id_to_css_dict={"choice-id": "[data-id=choice-id]"})
    scraped_page = SimpleNamespace(
        url="https://test",
        id_to_css_dict={},
        generate_scraped_page_without_screenshots=AsyncMock(return_value=scraped_page_after_open),
    )
    incremental_scraped = SimpleNamespace(
        get_incremental_elements_num=AsyncMock(return_value=1),
        get_incremental_element_tree=AsyncMock(return_value=[_SHADOW_OPTION]),
    )

    with structlog.testing.capture_logs() as logs:
        await handler.handle_sequential_click_for_dropdown(
            action=ClickAction(element_id="field-id", reasoning="click", intention="choose"),
            action_history=[],
            anchor_element=MagicMock(get_id=Mock(return_value="field-id")),
            dom=MagicMock(),
            page=SimpleNamespace(url="https://test"),
            skyvern_frame=SimpleNamespace(safe_wait_for_animation_end=AsyncMock()),
            scraped_page=scraped_page,
            incremental_scraped=incremental_scraped,
            task=_OUTCOME_TASK,
            step=SimpleNamespace(step_id="step"),
        )

    options = select_from_emerging_elements.await_args.kwargs["options"]
    assert options.target_value is None
    assert [log for log in logs if log.get("event") == "custom_select_family_outcome"] == []


@pytest.mark.asyncio
async def test_outcome_event_once_per_dropdown_level_with_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    handler._COLLAPSE_XP_ASSIGNMENT_MEMO.clear()
    monkeypatch.setattr(
        handler.app,
        "EXPERIMENTATION_PROVIDER",
        SimpleNamespace(
            is_feature_enabled_cached=AsyncMock(return_value=True),
            resolve_feature_enabled_unrecorded=AsyncMock(return_value=False),
        ),
    )
    monkeypatch.setattr(
        handler.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            resolve_field_option=AsyncMock(
                return_value=SimpleNamespace(
                    fallback_to_llm=False,
                    matched_index=0,
                    matched_label="Choice",
                    matched_tier="exact",
                )
            )
        ),
    )
    monkeypatch.setattr(
        handler.app,
        "CUSTOM_SELECT_AGENT_LLM_API_HANDLER",
        AsyncMock(return_value={"id": "matched-id", "value": "Choice", "action_type": "input_text"}),
    )
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(handler, "get_input_value", AsyncMock(return_value=""))
    monkeypatch.setattr(
        handler, "get_actual_value_of_parameter_if_secret_with_task", Mock(side_effect=lambda _, value: value)
    )

    dropdown = MagicMock(
        get_element_handler=AsyncMock(return_value=MagicMock()),
        get_locator=Mock(return_value=MagicMock(count=AsyncMock(return_value=1))),
    )
    input_element = MagicMock(
        get_tag_name=Mock(return_value="input"),
        get_locator=Mock(return_value=MagicMock()),
        scroll_into_view=AsyncMock(),
        is_readonly=AsyncMock(return_value=False),
        input_clear=AsyncMock(),
        input_sequentially=AsyncMock(),
    )
    skyvern_frame = MagicMock(
        safe_wait_for_animation_end=AsyncMock(),
        get_element_scrollable=AsyncMock(return_value=False),
        get_element_visible=AsyncMock(return_value=True),
    )
    incremental_scraped = MagicMock(get_incremental_element_tree=AsyncMock(return_value=[_SHADOW_OPTION]))
    incremental_scraped.build_element_tree.return_value = "<div>Choice</div>"
    monkeypatch.setattr(handler, "try_to_find_potential_scrollable_element", AsyncMock(return_value=dropdown))
    monkeypatch.setattr(handler.SkyvernElement, "create_from_incremental", AsyncMock(return_value=input_element))

    action = SelectOptionAction(
        element_id="field-id",
        option=SelectOption(label="Choice"),
        input_or_select_context=InputOrSelectContext(field="Field"),
    )
    with skyvern_context.scoped(SkyvernContext(tz_info=ZoneInfo("UTC"))):
        with structlog.testing.capture_logs() as logs:
            await handler.sequentially_select_from_dropdown(
                action=action,
                input_or_select_context=InputOrSelectContext(field="Field"),
                page=MagicMock(),
                dom=MagicMock(),
                skyvern_element=MagicMock(),
                skyvern_frame=skyvern_frame,
                incremental_scraped=incremental_scraped,
                step=SimpleNamespace(step_id="step"),
                task=_OUTCOME_TASK,
                dropdown_menu_element=dropdown,
                force_select=True,
                target_value="Choice",
            )

    events = [log for log in logs if log.get("event") == "custom_select_family_outcome"]
    assert len(events) == 3
    assert [event["outcome"] for event in events] == ["llm_fallback_control"] * 3
    assert events[0]["selection_group_id"]
    assert len({event["selection_group_id"] for event in events}) == 1
    assert [event["select_depth"] for event in events] == [0, 1, 2]


@pytest.mark.asyncio
async def test_outcome_event_family_off_emits_without_assignment_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _, event, provider, resolver = await _run_outcome_case(monkeypatch, "family_off", family_enabled=False)
    assert event["outcome"] == "llm_fallback_family_off"
    assert (event["assigned"], event["eligible"], event["match_tier"]) == (None, True, "exact")
    provider.resolve_feature_enabled_unrecorded.assert_not_awaited()
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_outcome_event_key_allowlist_and_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    _, event, _, _ = await _run_outcome_case(monkeypatch, "control")
    expected_keys = set(
        "event log_level family workflow_run_id task_id organization_id step_id entry_action_type selection_group_id "
        "select_depth family_gate_enabled assigned gate_error encountered eligible match_tier option_count attempted "
        "click_attempted verified_success outcome llm_fallback_requested duration_ms".split()
    )
    expected_outcomes = set(
        "llm_fallback_gate_error llm_fallback_eval_error llm_fallback_family_off llm_fallback_control "
        "llm_fallback_no_match llm_fallback_match_unactionable llm_fallback_pre_click_error "
        "llm_fallback_reset_verified llm_fallback_post_click_unverified success_precommit success_verified "
        "terminal_post_click_exception terminal_unverified_reset terminal_unverified_click "
        "terminal_unverified_toggle".split()
    )
    assert event["outcome"] == "llm_fallback_control"
    assert set(event) == expected_keys
    assert {outcome.value for outcome in handler.CustomSelectFamilyOutcome} == expected_outcomes


@pytest.mark.asyncio
async def test_outcome_event_gate_error_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _, event, _, _ = await _run_outcome_case(monkeypatch, "gate_error")
    assert event["outcome"] == "llm_fallback_gate_error"
    assert (event["option_count"], event["eligible"], event["match_tier"]) == (None, False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "family_enabled", "assigned"),
    [
        (case, family, assigned)
        for case in ("walker_error", "matcher_error")
        for family, assigned in ((False, False), (True, False), (True, True))
    ],
)
async def test_outcome_event_eval_error_emits_on_all_arms(
    monkeypatch: pytest.MonkeyPatch, case: str, family_enabled: bool, assigned: bool
) -> None:
    result, event, _, _ = await _run_outcome_case(monkeypatch, case, family_enabled=family_enabled, assigned=assigned)
    assert result is None
    assert event["outcome"] == "llm_fallback_eval_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "outcome", "attempted"),
    [
        ("no_match", "llm_fallback_no_match", False),
        ("bad_index", "llm_fallback_match_unactionable", False),
        ("no_element_id", "llm_fallback_match_unactionable", False),
        ("listbox", "llm_fallback_match_unactionable", False),
        ("pre_click", "llm_fallback_pre_click_error", True),
        ("precommit", "success_precommit", True),
        ("verified", "success_verified", True),
        ("nonchoice", "llm_fallback_post_click_unverified", True),
        ("toggle", "terminal_unverified_toggle", True),
    ],
)
async def test_outcome_event_exact_for_each_treatment_exit(
    monkeypatch: pytest.MonkeyPatch, case: str, outcome: str, attempted: bool
) -> None:
    _, event, _, _ = await _run_outcome_case(monkeypatch, case, assigned=True)
    assert event["outcome"] == outcome
    assert event["attempted"] is attempted


@pytest.mark.asyncio
async def test_click_attempted_true_when_click_dispatches_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    result, event, _, _ = await _run_outcome_case(monkeypatch, "post_click", assigned=True)
    assert result is None
    assert event["outcome"] == "llm_fallback_post_click_unverified"
    assert event["click_attempted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "outcome"),
    [("reset_ok", "llm_fallback_reset_verified"), ("reset_failed", "llm_fallback_post_click_unverified")],
)
async def test_reset_outcome_recorded_without_behavior_change(
    monkeypatch: pytest.MonkeyPatch, case: str, outcome: str
) -> None:
    result, event, _, _ = await _run_outcome_case(monkeypatch, case, assigned=True)
    assert result is None
    assert event["outcome"] == outcome


@pytest.mark.asyncio
async def test_converted_route_reports_input_text_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    element = MagicMock(is_disabled=AsyncMock(return_value=False), get_selectable=AsyncMock(return_value=True))
    element.get_tag_name.return_value, element.get_id.return_value = "input", "field"
    monkeypatch.setattr(
        handler, "DomUtil", Mock(return_value=MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=element)))
    )
    monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(handler, "IncrementalScrapePage", MagicMock())
    monkeypatch.setattr(handler, "get_input_value", AsyncMock(return_value=""))
    select = AsyncMock(return_value=[handler.ActionSuccess()])
    monkeypatch.setattr(handler, "handle_select_option_action", select)
    await handler.handle_input_text_action(
        handler.InputTextAction(element_id="field", text="Choice"),
        MagicMock(),
        SimpleNamespace(id_to_element_dict={"field": {"tagName": "input"}}),
        SimpleNamespace(workflow_run_id=None),
        MagicMock(),
    )
    assert select.await_args.kwargs["entry_action_type"] == "input_text_converted"
