from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
import structlog.testing

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputOrSelectContext, SelectOption, SelectOptionAction


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


def test_select_shadow_match_normalizes_curly_apostrophes() -> None:
    matched_index, tier = handler.classify_option_match("Worker’s Compensation", ["Workers Compensation"])

    assert matched_index == 0
    assert tier == "exact"
