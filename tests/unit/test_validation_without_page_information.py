"""Tests for the ValidationBlock opt-in ``without_page_information`` flag
(SKY-10593): the block-level field, its YAML round-trip through the workflow
converter, and the context threading done by ``ValidationBlock.execute()``.

The prompt-builder side (the flag OR'd into the validation prompt) is covered
by ``test_validation_evidence_router_wiring.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, ValidationBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import ValidationBlockYAML


def _output_parameter(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        output_parameter_id="op_1",
        workflow_id="w_1",
        created_at=now,
        modified_at=now,
    )


def _validation_block(without_page_information: bool) -> ValidationBlock:
    return ValidationBlock(
        label="v",
        output_parameter=_output_parameter("v"),
        complete_criterion="billing_date within range",
        without_page_information=without_page_information,
    )


def test_field_defaults_false_on_block_and_yaml() -> None:
    block = ValidationBlock(label="v", output_parameter=_output_parameter("v"), complete_criterion="x")
    assert block.without_page_information is False
    assert ValidationBlockYAML(label="v", complete_criterion="x").without_page_information is False


def test_field_settable_true() -> None:
    assert _validation_block(True).without_page_information is True


@pytest.mark.parametrize("flag", [True, False])
def test_converter_threads_flag_from_yaml(flag: bool) -> None:
    block_yaml = ValidationBlockYAML(label="v", complete_criterion="x", without_page_information=flag)
    parameters = {"v_output": _output_parameter("v")}
    block = block_yaml_to_block(block_yaml, parameters)
    assert isinstance(block, ValidationBlock)
    assert block.without_page_information is flag


def test_converter_defaults_flag_false_when_omitted() -> None:
    block_yaml = ValidationBlockYAML(label="v", complete_criterion="x")
    block = block_yaml_to_block(block_yaml, {"v_output": _output_parameter("v")})
    assert block.without_page_information is False


@pytest.mark.asyncio
async def test_execute_sets_and_restores_context_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute() must expose the block's flag on the context for the duration of
    the block, then restore the previous value so it can't leak to later blocks."""
    captured: dict[str, bool] = {}

    async def fake_get_task_order(workflow_run_id: str, current_retry: int) -> tuple[int, int]:
        return 1, 0  # non-first task, so execute proceeds past the guard

    async def fake_super_execute(self: BaseTaskBlock, **kwargs: object) -> str:
        captured["during"] = skyvern_context.current().validation_without_page_information
        return "ok"

    monkeypatch.setattr(ValidationBlock, "get_task_order", staticmethod(fake_get_task_order))
    monkeypatch.setattr(BaseTaskBlock, "execute", fake_super_execute)

    ctx = SkyvernContext(tz_info=None)
    ctx.validation_without_page_information = True  # a prior value that must be restored
    token = skyvern_context._context.set(ctx)
    try:
        result = await _validation_block(False).execute(
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
        )
    finally:
        skyvern_context._context.reset(token)

    assert result == "ok"
    assert captured["during"] is False  # block's own flag while it runs
    assert ctx.validation_without_page_information is True  # prior value restored
