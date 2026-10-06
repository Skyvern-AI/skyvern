from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import BlockEngineNotEnabledError, CodeBlockTemplateSyntaxError
from skyvern.forge.sdk.workflow.models.block import CodeBlock, ForLoopBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunEngine


def _output_parameter(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _code_block(label: str, code: str) -> CodeBlock:
    return CodeBlock(label=label, code=code, output_parameter=_output_parameter(label))


def _definition(blocks: list[CodeBlock | ForLoopBlock | TaskBlock]) -> WorkflowDefinition:
    return WorkflowDefinition(parameters=[], blocks=blocks)


def test_save_validator_rejects_nested_code_block_with_line_number() -> None:
    nested = _code_block("nested_code", "value = 1\nraw = r'''{{ }}'''\n")
    loop = ForLoopBlock(label="loop", loop_blocks=[nested], output_parameter=_output_parameter("loop"))

    with pytest.raises(CodeBlockTemplateSyntaxError) as excinfo:
        WorkflowService._validate_code_block_templates(_definition([loop]))

    assert excinfo.value.block_label == "nested_code"
    assert excinfo.value.line == 2


def test_save_validator_allows_syntactically_valid_undefined_reference() -> None:
    WorkflowService._validate_code_block_templates(_definition([_code_block("code", "value = {{ future_value }}")]))


def test_save_validator_rejects_unknown_jinja_filter() -> None:
    definition = _definition([_code_block("code", "value = {{ future_value | missing_filter }}")])

    with pytest.raises(CodeBlockTemplateSyntaxError) as excinfo:
        WorkflowService._validate_code_block_templates(definition)

    assert excinfo.value.block_label == "code"
    assert excinfo.value.line == 1


@pytest.mark.asyncio
async def test_create_workflow_rejects_before_database_write() -> None:
    service = WorkflowService()
    create = AsyncMock()
    definition = _definition([_code_block("code", "value = {{ }}")])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.create_workflow = create
        with pytest.raises(CodeBlockTemplateSyntaxError):
            await service.create_workflow(
                organization_id="org_test",
                title="Invalid code template",
                workflow_definition=definition,
                encrypt_secrets=False,
            )

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_workflow_rejects_before_database_write() -> None:
    service = WorkflowService()
    update = AsyncMock()
    definition = _definition([_code_block("code", "value = {{ }}")])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.organizations.get_organization = AsyncMock(return_value=None)
        mock_app.DATABASE.workflows.update_workflow_and_reconcile_definition_params = update
        with pytest.raises(CodeBlockTemplateSyntaxError):
            await service.update_workflow_definition(
                workflow_id="wf_test",
                organization_id="org_test",
                workflow_definition=definition,
            )

    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_ephemeral_update_can_bypass_code_template_validation() -> None:
    service = WorkflowService()
    saved = MagicMock()
    saved.organization_id = "org_test"
    update = AsyncMock(return_value=saved)
    definition = _definition([_code_block("code", "value = {{ }}")])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.organizations.get_organization = AsyncMock(return_value=None)
        mock_app.DATABASE.workflows.update_workflow_and_reconcile_definition_params = update
        result = await service.update_workflow_definition(
            workflow_id="wf_test",
            organization_id="org_test",
            workflow_definition=definition,
            validate_code_block_templates=False,
            notify_workflow_saved=False,
        )

    assert result is saved
    update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("save_path", ["create", "update"])
async def test_save_rejects_ui_tars_task_block_when_volcengine_disabled(
    save_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENABLE_VOLCENGINE", False)
    service = WorkflowService()
    write = AsyncMock()
    task = TaskBlock(
        label="task",
        url="https://example.com",
        navigation_goal="go",
        engine=RunEngine.ui_tars,
        output_parameter=_output_parameter("task"),
    )
    loop = ForLoopBlock(label="loop", loop_blocks=[task], output_parameter=_output_parameter("loop"))
    definition = _definition([loop])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.organizations.get_organization = AsyncMock(return_value=None)
        mock_app.DATABASE.workflows.create_workflow = write
        mock_app.DATABASE.workflows.update_workflow_and_reconcile_definition_params = write
        with pytest.raises(BlockEngineNotEnabledError):
            if save_path == "create":
                await service.create_workflow(
                    organization_id="org_test", title="t", workflow_definition=definition, encrypt_secrets=False
                )
            else:
                await service.update_workflow_definition(
                    workflow_id="wf_test", organization_id="org_test", workflow_definition=definition
                )

    write.assert_not_awaited()
