from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.api.llm.tokenless_usage import TokenlessRequestCost, TokenlessRunCost
from skyvern.forge.sdk.workflow import service as workflow_service
from skyvern.forge.sdk.workflow.service import WorkflowService


@pytest.mark.asyncio
async def test_evaluation_cost_uses_tokenless_request_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    database = SimpleNamespace(
        observer=SimpleNamespace(
            get_task_v2_by_workflow_run_id=AsyncMock(return_value=SimpleNamespace(llm_key="OPENAI_COMPATIBLE")),
        ),
        tasks=SimpleNamespace(),
    )
    monkeypatch.setattr(workflow_service, "app", SimpleNamespace(DATABASE=database))
    monkeypatch.setattr(
        workflow_service.tokenless_usage_tracker,
        "resolve",
        AsyncMock(
            return_value=TokenlessRunCost(
                agent_cost_usd=1.25,
                input_tokens=10,
                output_tokens=20,
                tokenless_request_count=2,
                cost_status="exact",
            )
        ),
    )
    monkeypatch.setattr(
        workflow_service.settings,
        "OPENAI_COMPATIBLE_MODEL_KEY",
        "OPENAI_COMPATIBLE",
    )

    result = await WorkflowService().get_workflow_run_evaluation_cost("wr_123", "org_123")

    assert result.agent_cost_usd == 1.25
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert result.tokenless_request_count == 2
    assert result.cost_status == "exact"


@pytest.mark.asyncio
async def test_evaluation_cost_uses_existing_gemini_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = SimpleNamespace(
        observer=SimpleNamespace(
            get_task_v2_by_workflow_run_id=AsyncMock(return_value=SimpleNamespace(llm_key="VERTEX_GEMINI_3.5_FLASH")),
            get_thought_cost_sum_by_workflow_run_id=AsyncMock(return_value=0.25),
            get_block_llm_cost_sum_by_workflow_run_id=AsyncMock(return_value=0.5),
            get_thought_token_sums_by_workflow_run_id=AsyncMock(return_value=(3, 4)),
        ),
        tasks=SimpleNamespace(
            get_tasks_by_workflow_run_id=AsyncMock(return_value=[SimpleNamespace(task_id="task_1")]),
            get_step_cost_sum_by_task_ids=AsyncMock(return_value=1.0),
            get_step_token_sums_by_task_ids=AsyncMock(return_value=(10, 20)),
        ),
    )
    monkeypatch.setattr(workflow_service, "app", SimpleNamespace(DATABASE=database))
    monkeypatch.setattr(
        workflow_service.settings,
        "OPENAI_COMPATIBLE_MODEL_KEY",
        "OPENAI_COMPATIBLE",
    )

    result = await WorkflowService().get_workflow_run_evaluation_cost("wr_123", "org_123")

    assert result.agent_cost_usd == 1.75
    assert result.input_tokens == 13
    assert result.output_tokens == 24
    assert result.tokenless_request_count == 0
    assert result.cost_status == "exact"


@pytest.mark.asyncio
async def test_evaluation_cost_reports_task_v2_call_dimensions_and_resolves_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = SimpleNamespace(
        task_v2_llm_call_id="call_1",
        task_v2_id="task_v2_1",
        organization_id="org_123",
        workflow_id="workflow_1",
        workflow_permanent_id="workflow_perm_1",
        call_type="planner",
        prompt_name="task_v2",
        task_type="navigate",
        iteration=2,
        loop_item_count=4,
        loop_index=1,
        retry_index=1,
        is_speculative=False,
        llm_key="TOKENLESS",
        model="tokenless-pro",
        requested_model="tokenless-pro",
        input_token_count=10,
        output_token_count=20,
        reasoning_token_count=3,
        image_token_count=5,
        cached_token_count=2,
        llm_cost=None,
    )
    update_call = AsyncMock()
    database = SimpleNamespace(
        observer=SimpleNamespace(
            get_task_v2_by_workflow_run_id=AsyncMock(return_value=SimpleNamespace(llm_key="OPENAI_COMPATIBLE")),
            update_task_v2_llm_call=update_call,
            get_task_v2_llm_calls_by_workflow_run_id=AsyncMock(return_value=[call]),
            get_task_v2_run_metrics=AsyncMock(
                return_value=SimpleNamespace(iteration_count=3, loop_item_count=4, retry_count=1)
            ),
        ),
        tasks=SimpleNamespace(),
    )
    monkeypatch.setattr(workflow_service, "app", SimpleNamespace(DATABASE=database))
    monkeypatch.setattr(
        workflow_service.tokenless_usage_tracker,
        "resolve",
        AsyncMock(
            return_value=TokenlessRunCost(
                agent_cost_usd=0.75,
                input_tokens=10,
                output_tokens=20,
                tokenless_request_count=1,
                cost_status="exact",
                resolved_call_costs=(TokenlessRequestCost("req_1", 750_000_000, 10, 20, "call_1"),),
            )
        ),
    )
    monkeypatch.setattr(workflow_service.settings, "OPENAI_COMPATIBLE_MODEL_KEY", "OPENAI_COMPATIBLE")

    result = await WorkflowService().get_workflow_run_evaluation_cost("wr_123", "org_123")

    assert result.planner_call_count == 1
    assert result.iteration_count == 3
    assert result.loop_item_count == 4
    assert result.retry_count == 1
    assert result.reasoning_tokens == 3
    assert result.image_tokens == 5
    assert result.model_call_counts == {"tokenless-pro": 1}
    assert result.prompt_call_counts == {"task_v2": 1}
    assert result.llm_calls[0].workflow_id == "workflow_1"
    update_call.assert_awaited_once_with(
        "call_1",
        "org_123",
        llm_cost=0.75,
        input_token_count=10,
        output_token_count=20,
    )
