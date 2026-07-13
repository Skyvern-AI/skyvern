from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputOrSelectContext, SelectOption, SelectOptionAction
from tests.unit.helpers import make_organization, make_task


class _FakeSelectElement:
    def __init__(self, options: list[dict], locator: MagicMock, selected_attr: str | None = None) -> None:
        self._options = options
        self._locator = locator
        self.get_attr = AsyncMock(return_value=selected_attr)
        self.refresh_select_options = AsyncMock(return_value=(options, ""))

    def get_locator(self) -> MagicMock:
        return self._locator

    def get_options(self) -> list[dict]:
        return self._options

    def build_HTML(self) -> str:
        return "<select></select>"


def _select_action(label: str) -> SelectOptionAction:
    return SelectOptionAction(
        element_id="select-1",
        option=SelectOption(label=label),
        input_or_select_context=InputOrSelectContext(field="Department", is_required=True),
    )


def _task() -> object:
    now = datetime.now(UTC)
    organization = make_organization(now)
    return make_task(now, organization)


def _selected_option_readback(
    options: list[dict],
    selected_value: str,
    *,
    selected_label: str | None = None,
    selected_index: int | None = None,
) -> dict:
    for index, option in enumerate(options):
        if option.get("value") == selected_value:
            return {
                "index": option.get("optionIndex", index) if selected_index is None else selected_index,
                "label": option.get("text") if selected_label is None else selected_label,
                "value": selected_value,
            }
    return {"index": selected_index, "label": selected_label or selected_value, "value": selected_value}


def _locator(
    options: list[dict],
    selected_value: str,
    *,
    selected_label: str | None = None,
    selected_index: int | None = None,
) -> MagicMock:
    locator = MagicMock()
    locator.click = AsyncMock()
    locator.select_option = AsyncMock()
    locator.input_value = AsyncMock(return_value=selected_value)
    locator.evaluate = AsyncMock(
        return_value=_selected_option_readback(
            options,
            selected_value,
            selected_label=selected_label,
            selected_index=selected_index,
        )
    )
    return locator


async def _run_normal_select(
    monkeypatch: pytest.MonkeyPatch,
    *,
    feature_enabled: bool,
    action: SelectOptionAction,
    options: list[dict],
    selected_value: str,
    selected_attr: str | None = None,
    selected_label: str | None = None,
    selected_index: int | None = None,
    llm_response: dict | None = None,
) -> tuple[list, MagicMock, AsyncMock]:
    provider = SimpleNamespace(
        is_feature_enabled_cached=AsyncMock(return_value=feature_enabled),
        resolve_feature_enabled_unrecorded=AsyncMock(return_value=feature_enabled),
    )
    normal_select_llm = AsyncMock(return_value=llm_response or {"value": "fallback", "index": None})

    monkeypatch.setattr(handler.app, "EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(handler.app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(handler.app, "NORMAL_SELECT_AGENT_LLM_API_HANDLER", normal_select_llm)
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", MagicMock(return_value="prompt"))
    monkeypatch.setattr(handler.skyvern_context, "ensure_context", MagicMock(return_value=SimpleNamespace(tz_info=UTC)))

    locator = _locator(options, selected_value, selected_label=selected_label, selected_index=selected_index)
    result = await handler.normal_select(
        action=action,
        skyvern_element=_FakeSelectElement(options, locator, selected_attr),  # type: ignore[arg-type]
        task=_task(),  # type: ignore[arg-type]
        step=MagicMock(),
        builder=MagicMock(),
    )
    return result, locator, normal_select_llm


@pytest.mark.parametrize(
    ("target", "options", "selected_value"),
    [
        (
            "United States",
            [
                {"optionIndex": 0, "text": "Canada", "value": "CA"},
                {"optionIndex": 1, "text": "United States", "value": "US"},
            ],
            "US",
        ),
        (
            "Departments",
            [
                {"optionIndex": 0, "text": "Department", "value": "department"},
                {"optionIndex": 1, "text": "Role", "value": "role"},
            ],
            "department",
        ),
    ],
)
@pytest.mark.asyncio
async def test_normal_select_exact_or_stem_match_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    options: list[dict],
    selected_value: str,
) -> None:
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=True,
        action=_select_action(target),
        options=options,
        selected_value=selected_value,
    )

    assert handler._normal_select_successful(result)
    locator.select_option.assert_awaited_once_with(
        value=selected_value,
        timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS,
    )
    normal_select_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_select_duplicate_value_selects_matched_label(monkeypatch: pytest.MonkeyPatch) -> None:
    action = _select_action("Target Department")
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=True,
        action=action,
        options=[
            {"optionIndex": 0, "text": "Wrong Department", "value": "shared"},
            {"optionIndex": 1, "text": "Target Department", "value": "shared"},
        ],
        selected_value="shared",
        selected_label="Target Department",
        selected_index=1,
    )

    assert handler._normal_select_successful(result)
    locator.select_option.assert_awaited_once_with(
        label="Target Department",
        timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS,
    )
    locator.evaluate.assert_awaited_once()
    normal_select_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_select_duplicate_value_wrong_label_falls_back_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _select_action("Target Department")
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=True,
        action=action,
        options=[
            {"optionIndex": 0, "text": "Wrong Department", "value": "shared"},
            {"optionIndex": 1, "text": "Target Department", "value": "shared"},
        ],
        selected_value="shared",
        selected_label="Wrong Department",
        selected_index=0,
        llm_response={"value": "Target Department", "index": 1},
    )

    assert handler._normal_select_successful(result)
    assert action.has_mini_agent is True
    assert locator.select_option.await_args_list[0] == call(
        label="Target Department",
        timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS,
    )
    locator.evaluate.assert_awaited_once()
    normal_select_llm.assert_awaited_once()


@pytest.mark.parametrize(
    ("target", "options", "llm_value"),
    [
        (
            "Senior",
            [
                {"optionIndex": 0, "text": "Senior Vice President", "value": "svp"},
                {"optionIndex": 1, "text": "Associate", "value": "associate"},
            ],
            "svp",
        ),
        (
            "United States",
            [
                {"optionIndex": 0, "text": "United States", "value": "US"},
                {"optionIndex": 1, "text": "United States", "value": "USA"},
            ],
            "USA",
        ),
    ],
)
@pytest.mark.asyncio
async def test_normal_select_unsafe_match_falls_back_to_llm(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    options: list[dict],
    llm_value: str,
) -> None:
    action = _select_action(target)
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=True,
        action=action,
        options=options,
        selected_value=llm_value,
        llm_response={"value": llm_value, "index": None},
    )

    assert handler._normal_select_successful(result)
    assert action.has_mini_agent is True
    locator.select_option.assert_awaited_once_with(value=llm_value, timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS)
    normal_select_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_select_readback_failure_falls_back_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    action = _select_action("United States")
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=True,
        action=action,
        options=[
            {"optionIndex": 0, "text": "Canada", "value": "CA"},
            {"optionIndex": 1, "text": "United States", "value": "US"},
        ],
        selected_value="CA",
        llm_response={"value": "US", "index": None},
    )

    assert handler._normal_select_successful(result)
    assert action.has_mini_agent is True
    locator.select_option.assert_has_awaits(
        [
            call(value="US", timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS),
            call(value="US", timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS),
        ]
    )
    normal_select_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_select_flag_off_uses_llm_path(monkeypatch: pytest.MonkeyPatch) -> None:
    action = _select_action("United States")
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=False,
        action=action,
        options=[{"optionIndex": 0, "text": "United States", "value": "US"}],
        selected_value="US",
        llm_response={"value": "US", "index": None},
    )

    assert handler._normal_select_successful(result)
    assert action.has_mini_agent is True
    locator.select_option.assert_awaited_once_with(value="US", timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS)
    normal_select_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_select_flag_off_marks_mini_agent_before_already_selected_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _select_action("United States")
    result, locator, normal_select_llm = await _run_normal_select(
        monkeypatch,
        feature_enabled=False,
        action=action,
        options=[{"optionIndex": 0, "text": "United States", "value": "US"}],
        selected_attr="United States",
        selected_value="US",
        llm_response={"value": "US", "index": None},
    )

    assert handler._normal_select_successful(result)
    assert action.has_mini_agent is True
    locator.select_option.assert_not_awaited()
    normal_select_llm.assert_not_awaited()
