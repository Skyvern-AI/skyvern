from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from zoneinfo import ZoneInfo

import pytest
import structlog.testing

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputOrSelectContext, SelectOption, SelectOptionAction

_SHADOW_OPTION = {
    "id": "matched-id",
    "tagName": "div",
    "attributes": {"role": "option"},
    "text": "Committed Value",
    "interactable": True,
}


def _shadow_task() -> SimpleNamespace:
    return SimpleNamespace(navigation_goal="goal", navigation_payload={}, llm_key=None)


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
                task=SimpleNamespace(navigation_goal="goal", navigation_payload={}, organization_id=None),
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
    llm_handler = AsyncMock(
        return_value={"id": "different-id", "value": "  committed   value  ", "action_type": "click"}
    )
    monkeypatch.setattr(handler, "DomUtil", Mock(return_value=dom_after_open))
    monkeypatch.setattr(handler, "json_to_html", Mock(return_value="<div>Committed Value</div>"))
    monkeypatch.setattr(handler, "_select_deterministic_custom_option", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        Mock(return_value=llm_handler),
    )
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
