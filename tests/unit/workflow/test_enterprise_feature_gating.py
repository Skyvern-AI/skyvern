from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import DisabledBlockExecutionError
from skyvern.forge.sdk.enterprise_features import collect_enterprise_gated_run_features
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.block import (
    BlockTypeVar,
    BranchCondition,
    ConditionalBlock,
    ForLoopBlock,
    NavigationBlock,
    SplitPdfBlock,
    TaskV2Block,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition, WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService, _collect_enterprise_gated_workflow_features
from skyvern.schemas.runs import RunEngine


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id",
        key=key,
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


def _navigation_block(
    label: str,
    *,
    engine: RunEngine = RunEngine.skyvern_v1,
    model: dict[str, str] | None = None,
) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_parameter(f"{label}_output"),
        engine=engine,
        model=model,
    )


def _workflow(blocks: list[BlockTypeVar], model: dict[str, str] | None = None) -> Workflow:
    now = datetime.now(UTC)
    return Workflow(
        workflow_id="wf",
        organization_id="org",
        title="workflow",
        workflow_permanent_id="wpid",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=blocks),
        model=model,
        created_at=now,
        modified_at=now,
    )


def test_collects_cua_engines_and_opus_46_models() -> None:
    workflow = _workflow(
        [
            _navigation_block("openai", engine=RunEngine.openai_cua),
            _navigation_block("opus", model={"model_name": "claude-opus-4-6"}),
        ],
    )

    assert _collect_enterprise_gated_workflow_features(workflow) == {
        "Anthropic Claude 4.6 Opus",
        "OpenAI CUA",
    }


def test_collects_nested_anthropic_cua_engine() -> None:
    loop = ForLoopBlock(
        label="loop",
        output_parameter=_output_parameter("loop_output"),
        loop_blocks=[_navigation_block("anthropic", engine=RunEngine.anthropic_cua)],
    )
    workflow = _workflow([loop])

    assert _collect_enterprise_gated_workflow_features(workflow) == {"Anthropic CUA"}


def test_block_label_filter_ignores_unselected_gated_blocks() -> None:
    workflow = _workflow(
        [
            _navigation_block("standard"),
            _navigation_block("openai", engine=RunEngine.openai_cua),
        ],
    )

    assert _collect_enterprise_gated_workflow_features(workflow, block_labels=["standard"]) == set()


def test_block_label_filter_includes_selected_nested_gated_blocks() -> None:
    loop = ForLoopBlock(
        label="loop",
        output_parameter=_output_parameter("loop_output"),
        loop_blocks=[_navigation_block("anthropic", engine=RunEngine.anthropic_cua)],
    )
    workflow = _workflow([_navigation_block("standard"), loop])

    assert _collect_enterprise_gated_workflow_features(workflow, block_labels=["loop"]) == {"Anthropic CUA"}


def test_ignores_unused_workflow_level_gated_model() -> None:
    workflow = _workflow(
        [_navigation_block("standard")],
        model={"model_name": "claude-opus-4-6"},
    )

    assert _collect_enterprise_gated_workflow_features(workflow) == set()
    assert _collect_enterprise_gated_workflow_features(workflow, block_labels=["standard"]) == set()


def test_collects_conditional_branch_target_gated_blocks() -> None:
    conditional = ConditionalBlock(
        label="choose",
        output_parameter=_output_parameter("choose_output"),
        branch_conditions=[BranchCondition(is_default=True, next_block_label="openai")],
    )
    workflow = _workflow([conditional, _navigation_block("openai", engine=RunEngine.openai_cua)])

    assert _collect_enterprise_gated_workflow_features(workflow) == {"OpenAI CUA"}


def test_ignores_non_enterprise_models_and_engines() -> None:
    workflow = _workflow(
        [_navigation_block("standard", model={"model_name": "gpt-4o"})],
        model={"model_name": "gemini-2.5-flash"},
    )

    assert _collect_enterprise_gated_workflow_features(workflow) == set()


def test_ignores_stale_model_on_task_v2_block() -> None:
    workflow = _workflow(
        [
            TaskV2Block(
                label="task-v2",
                prompt="goal",
                output_parameter=_output_parameter("task_v2_output"),
                model={"model_name": "claude-opus-4-6"},
            )
        ]
    )

    assert _collect_enterprise_gated_workflow_features(workflow) == set()


def test_collects_gated_model_on_split_pdf_block() -> None:
    split_block = SplitPdfBlock(
        label="split",
        file_url="{{ source }}",
        prompt="Split by document.",
        output_parameter=_output_parameter("split_output"),
        model={"model_name": "claude-opus-4-6"},
    )
    workflow = _workflow([split_block])

    assert _collect_enterprise_gated_workflow_features(workflow) == {"Anthropic Claude 4.6 Opus"}


@pytest.mark.parametrize(
    ("model_name", "feature_name"),
    [
        ("us.anthropic.claude-opus-4-20250514-v1:0", "Anthropic Claude 4 Opus"),
        ("claude-opus-4-5-20251101", "Anthropic Claude 4.5 Opus"),
        ("claude-opus-4-6", "Anthropic Claude 4.6 Opus"),
        ("claude-opus-4-7", "Anthropic Claude 4.7 Opus"),
        ("claude-opus-4-8", "Anthropic Claude 4.8 Opus"),
        ("claude-fable-5", "Anthropic Claude Fable 5"),
    ],
)
def test_collects_enterprise_model_alias_features(model_name: str, feature_name: str) -> None:
    assert collect_enterprise_gated_run_features(model={"model_name": model_name}) == {feature_name}


def test_collects_direct_run_enterprise_features() -> None:
    assert collect_enterprise_gated_run_features(
        engine=RunEngine.anthropic_cua,
        model={"model_name": "claude-opus-4-6"},
    ) == {
        "Anthropic CUA",
        "Anthropic Claude 4.6 Opus",
    }


@pytest.mark.asyncio
async def test_execute_workflow_cleans_up_after_enterprise_gate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _workflow([_navigation_block("openai", engine=RunEngine.openai_cua)])
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_1",
        workflow_id=workflow.workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        browser_profile_id=None,
        browser_session_id=None,
        browser_address=None,
        status=WorkflowRunStatus.created,
    )
    failed_workflow_run = SimpleNamespace(
        workflow_run_id="wr_1",
        workflow_permanent_id=workflow.workflow_permanent_id,
        status=WorkflowRunStatus.failed,
    )
    organization = SimpleNamespace(organization_id="org")
    agent_function = SimpleNamespace(
        validate_enterprise_feature_access=AsyncMock(
            side_effect=DisabledBlockExecutionError("Enterprise plan required for OpenAI CUA")
        )
    )
    monkeypatch.setattr(service_module.app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    mark_workflow_run_as_failed = AsyncMock(return_value=failed_workflow_run)
    clean_up_workflow = AsyncMock()
    monkeypatch.setattr(svc, "mark_workflow_run_as_failed", mark_workflow_run_as_failed)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key="api_key",
        organization=organization,
    )

    assert result is failed_workflow_run
    agent_function.validate_enterprise_feature_access.assert_awaited_once_with(
        organization_id="org",
        feature_names={"OpenAI CUA"},
    )
    mark_workflow_run_as_failed.assert_awaited_once()
    clean_up_workflow.assert_awaited_once_with(
        workflow=workflow,
        workflow_run=failed_workflow_run,
        api_key="api_key",
        browser_session_id=None,
        close_browser_on_completion=True,
        need_call_webhook=True,
    )
