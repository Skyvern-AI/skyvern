from pathlib import Path
from typing import Any

import pytest
from jinja2 import Template

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.handler import CustomSelectPromptOptions
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess


@pytest.mark.asyncio
async def test_date_dropdown_tries_emerging_element_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_select_from_emerging_elements(**kwargs: Any) -> ActionSuccess:
        captured.update(kwargs)
        return ActionSuccess(data={"selected": "01.01.2026"})

    monkeypatch.setattr(handler, "select_from_emerging_elements", fake_select_from_emerging_elements)
    options = CustomSelectPromptOptions(
        field_information="From date",
        is_date_related=True,
        required_field=True,
    )

    result = await handler._select_date_from_emerging_elements_or_skip(
        current_element_id="date-anchor",
        options=options,
        page=object(),
        scraped_page=object(),
        step=object(),
        task=object(),
        scraped_page_after_open=object(),
        new_interactable_element_ids=["day-1"],
    )

    assert result.success is True
    assert result.skip_remaining_actions is True
    assert captured["current_element_id"] == "date-anchor"
    assert captured["options"] is options
    assert captured["new_interactable_element_ids"] == ["day-1"]


@pytest.mark.asyncio
async def test_date_dropdown_preserves_skip_behavior_when_selection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_select_from_emerging_elements(**kwargs: Any) -> ActionSuccess:
        raise RuntimeError("no exact date option")

    monkeypatch.setattr(handler, "select_from_emerging_elements", fake_select_from_emerging_elements)

    result = await handler._select_date_from_emerging_elements_or_skip(
        current_element_id="date-anchor",
        options=CustomSelectPromptOptions(field_information="From date", is_date_related=True),
        page=object(),
        scraped_page=object(),
        step=object(),
        task=object(),
        scraped_page_after_open=object(),
        new_interactable_element_ids=["day-1"],
    )

    assert result.success is True
    assert result.skip_remaining_actions is True


@pytest.mark.asyncio
async def test_date_dropdown_preserves_skip_behavior_when_selection_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_select_from_emerging_elements(**kwargs: Any) -> ActionFailure:
        return ActionFailure(exception=RuntimeError("date option was not actionable"))

    monkeypatch.setattr(handler, "select_from_emerging_elements", fake_select_from_emerging_elements)

    result = await handler._select_date_from_emerging_elements_or_skip(
        current_element_id="date-anchor",
        options=CustomSelectPromptOptions(field_information="From date", is_date_related=True),
        page=object(),
        scraped_page=object(),
        step=object(),
        task=object(),
        scraped_page_after_open=object(),
        new_interactable_element_ids=["day-1"],
    )

    assert result.success is True
    assert result.skip_remaining_actions is True


def test_custom_select_date_guidance_renders_without_select_history() -> None:
    template_path = Path("skyvern/forge/prompts/skyvern/custom-select.j2")
    prompt = Template(template_path.read_text()).render(
        field_information="From date",
        required_field=True,
        is_date_related=True,
        navigation_goal="Set the from date to 01.01.2026.",
        navigation_payload_str="{}",
        elements='<button id="day-1">01</button><button>Today</button>',
        new_elements_ids=["day-1"],
        select_history="",
        target_value="01.01.2026",
        support_complete_action=False,
        local_datetime="2026-05-21T12:00:00",
    )

    assert "A date picker might be triggered." in prompt
    assert 'Do not choose "Today" unless the target date is today.' in prompt
    assert "Select History:" not in prompt
