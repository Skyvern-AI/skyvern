"""Data captured inside a navigate block must reach task_history.

The _get_navigate_complete_output tests exercise the helper in isolation, so some
pass block_result shapes the run_task_v2_helper guard would screen out upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockResult
from skyvern.services import task_v2_service
from skyvern.services.task_v2_service import (
    NAVIGATE_STRUCTURED_OUTPUT_MAX_CHARS,
    NAVIGATE_TERMINAL_OUTPUT_MAX_CHARS,
    _get_extracted_data_from_block_result,
    _get_navigate_complete_output,
)
from skyvern.webeye.actions.actions import CompleteAction, WaitAction


def _output_parameter() -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="nav_output",
        output_parameter_id="op_nav",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _block_result(output_parameter_value: object, success: bool = True) -> BlockResult:
    return BlockResult(
        success=success,
        output_parameter=_output_parameter(),
        output_parameter_value=output_parameter_value,
    )


def test_navigate_block_extracted_information_is_credited() -> None:
    block_result = _block_result({"task_id": "tsk_1", "extracted_information": {"price": "$12,952"}})
    assert _get_extracted_data_from_block_result(block_result, "navigate") == {"price": "$12,952"}


def test_navigate_block_without_extracted_information_returns_none() -> None:
    block_result = _block_result({"task_id": "tsk_1", "extracted_information": None})
    assert _get_extracted_data_from_block_result(block_result, "navigate") is None


@pytest.mark.asyncio
async def test_navigate_complete_output_prefers_response_then_output_then_reasoning(monkeypatch) -> None:
    actions = [
        WaitAction(task_id="tsk_1"),
        CompleteAction(task_id="tsk_1", reasoning="reached the page", response="RAV4 2015 XLE AWD $12,952"),
    ]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1", "extracted_information": None})
    result = await _get_navigate_complete_output(block_result, organization_id="o_1")
    assert result == "RAV4 2015 XLE AWD $12,952"


@pytest.mark.asyncio
async def test_navigate_complete_output_uses_output_when_response_empty(monkeypatch) -> None:
    actions = [
        CompleteAction(task_id="tsk_1", response=None, output={"price": "$5,522"}, reasoning="on the page"),
    ]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    result = await _get_navigate_complete_output(block_result, organization_id="o_1")
    assert result == {"price": "$5,522"}


@pytest.mark.asyncio
async def test_navigate_complete_output_falls_back_to_reasoning(monkeypatch) -> None:
    actions = [
        CompleteAction(task_id="tsk_1", reasoning="Found cheapest RAV4: $12,952 at a dealer"),
    ]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    result = await _get_navigate_complete_output(block_result, organization_id="o_1")
    assert result == "Found cheapest RAV4: $12,952 at a dealer"


@pytest.mark.asyncio
async def test_navigate_complete_output_uses_terminal_complete_action(monkeypatch) -> None:
    actions = [
        CompleteAction(task_id="tsk_1", reasoning="first attempt, wrong page"),
        WaitAction(task_id="tsk_1"),
        CompleteAction(task_id="tsk_1", reasoning="final answer: $5,522 Prius"),
    ]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    result = await _get_navigate_complete_output(block_result, organization_id="o_1")
    assert result == "final answer: $5,522 Prius"


@pytest.mark.asyncio
async def test_navigate_complete_output_does_not_fall_back_past_dataless_terminal(monkeypatch) -> None:
    # Only the terminal COMPLETE is authoritative: a data-less terminal action
    # returns None even if an earlier COMPLETE carried data.
    actions = [
        CompleteAction(task_id="tsk_1", reasoning="earlier answer: $5,522 Prius"),
        CompleteAction(task_id="tsk_1", response=None, output=None, reasoning=None),
    ]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    assert await _get_navigate_complete_output(block_result, organization_id="o_1") is None


@pytest.mark.asyncio
async def test_navigate_complete_output_caps_long_reasoning(monkeypatch) -> None:
    long_reasoning = "x" * (NAVIGATE_TERMINAL_OUTPUT_MAX_CHARS + 500)
    actions = [CompleteAction(task_id="tsk_1", reasoning=long_reasoning)]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    result = await _get_navigate_complete_output(block_result, organization_id="o_1")
    assert result == "x" * NAVIGATE_TERMINAL_OUTPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_navigate_complete_output_drops_oversized_structured_output(monkeypatch) -> None:
    big_list = ["row"] * NAVIGATE_STRUCTURED_OUTPUT_MAX_CHARS  # str() far exceeds the cap
    actions = [CompleteAction(task_id="tsk_1", output=big_list)]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    assert await _get_navigate_complete_output(block_result, organization_id="o_1") is None


@pytest.mark.asyncio
async def test_navigate_complete_output_keeps_small_structured_output(monkeypatch) -> None:
    actions = [CompleteAction(task_id="tsk_1", output={"price": "$5,522"})]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    assert await _get_navigate_complete_output(block_result, organization_id="o_1") == {"price": "$5,522"}


@pytest.mark.asyncio
async def test_navigate_complete_output_none_without_complete_action(monkeypatch) -> None:
    actions = [WaitAction(task_id="tsk_1")]
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(return_value=actions)))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    assert await _get_navigate_complete_output(block_result, organization_id="o_1") is None


@pytest.mark.asyncio
async def test_navigate_complete_output_none_on_db_error(monkeypatch) -> None:
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task_actions=AsyncMock(side_effect=RuntimeError("db down"))))
    )
    monkeypatch.setattr(task_v2_service, "app", fake_app)

    block_result = _block_result({"task_id": "tsk_1"})
    assert await _get_navigate_complete_output(block_result, organization_id="o_1") is None


@pytest.mark.asyncio
async def test_navigate_complete_output_none_when_no_task_id() -> None:
    assert await _get_navigate_complete_output(_block_result(None), organization_id="o_1") is None
    assert await _get_navigate_complete_output(_block_result({}), organization_id="o_1") is None
