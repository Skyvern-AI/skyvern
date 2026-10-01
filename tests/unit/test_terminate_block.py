from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.forge import app
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowRunAttemptModel, WorkflowRunModel
from skyvern.forge.sdk.db.repositories.observer import ObserverRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.repositories.workflow_run_attempts import WorkflowRunAttemptsRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block, ForLoopBlock, WaitBlock
from skyvern.forge.sdk.workflow.models.terminate_block import TerminateBlock
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import on_terminal_transition
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockStatus, BlockType, TerminateBlockYAML, WorkflowRetryPolicy
from tests.unit.conftest import make_block_output_parameter


def _terminate_block(reason: str, **fields: Any) -> TerminateBlock:
    # continue_on_failure and next_loop_on_failure are set to prove the block ignores both.
    block = block_yaml_to_block(
        TerminateBlockYAML(label="stop", reason=reason, continue_on_failure=True, next_loop_on_failure=True, **fields),
        {"stop_output": make_block_output_parameter("stop_output")},
    )
    assert isinstance(block, TerminateBlock)
    return block


@pytest.fixture
def run_context(monkeypatch: pytest.MonkeyPatch) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="Terminate test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    context.values["account_number"] = "A-42"
    monkeypatch.setattr(TerminateBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(app.DATABASE.observer, "update_workflow_run_block", AsyncMock())
    return context


@pytest.mark.asyncio
async def test_terminate_ends_the_run_with_the_rendered_reason(run_context: WorkflowRunContext) -> None:
    block = _terminate_block("ACCOUNT_NOT_FOUND: {{ account_number }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "ACCOUNT_NOT_FOUND: A-42"
    run_status, run_failure_reason, _ = WorkflowService._resolve_block_terminal_outcome(
        block=block, block_result=result
    )
    assert run_status == WorkflowRunStatus.terminated
    assert run_failure_reason is not None and "ACCOUNT_NOT_FOUND: A-42" in run_failure_reason


@pytest.mark.asyncio
async def test_blank_rendered_reason_still_terminates_with_a_fallback_reason(run_context: WorkflowRunContext) -> None:
    run_context.values["reason_code"] = "  "
    block = _terminate_block("{{ reason_code }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "Terminated by the stop block"
    assert result.can_continue_after_failure is False


@pytest.mark.asyncio
async def test_unrenderable_reason_still_stops_the_run(run_context: WorkflowRunContext) -> None:
    block = _terminate_block("{{ account_number ")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.failed
    assert result.error_codes == []
    assert result.can_continue_after_failure is False
    run_status, _, _ = WorkflowService._resolve_block_terminal_outcome(block=block, block_result=result)
    assert run_status == WorkflowRunStatus.failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        pytest.param("{{ account_number ", id="unrenderable"),
        pytest.param("{{ oversized_code }}", id="too_long"),
        pytest.param("{{ control_code }}", id="control_character"),
        pytest.param("{{ percent_code }}", id="percent_escape"),
        pytest.param("{{ html_code }}", id="html_escape"),
        pytest.param("{{ nested_percent_code }}", id="nested_percent_escape"),
        pytest.param("{{ json_code }}", id="json_escape"),
    ],
)
async def test_unusable_rendered_code_does_not_turn_termination_into_failure(
    run_context: WorkflowRunContext, error_code: str
) -> None:
    run_context.values.update(
        oversized_code="X" * 129,
        control_code="INVALID\x00CODE",
        percent_code="AUTH%3cab",
        html_code="AUTH&amp;lt;ab",
        nested_percent_code="AUTH%253cab",
        json_code=r"AUTH\u003cab",
    )
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.can_continue_after_failure is False
    assert result.output_parameter_value["error_code_dropped"] is True
    assert result.output_parameter_value["reason"] == result.failure_reason
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()
    run_status, _, _ = WorkflowService._resolve_block_terminal_outcome(block=block, block_result=result)
    assert run_status == WorkflowRunStatus.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["ACCOUNT_NOT_FOUND", "{{ outcome_code }}", "{{ padded_outcome_code }}"])
async def test_error_code_reaches_the_block_result(run_context: WorkflowRunContext, error_code: str) -> None:
    run_context.values.update(outcome_code="ACCOUNT_NOT_FOUND", padded_outcome_code="  ACCOUNT_NOT_FOUND\n")
    block = _terminate_block("No account matches {{ account_number }}", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == ["ACCOUNT_NOT_FOUND"]
    assert result.failure_reason == "No account matches A-42"
    assert result.can_continue_after_failure is False


@pytest.mark.asyncio
async def test_long_template_source_can_render_a_short_error_code(run_context: WorkflowRunContext) -> None:
    template = "{% if " + "true and " * 20 + "true %}ACCOUNT_LOCKED{% endif %}"
    assert len(template) > 128
    block = _terminate_block("stop", error_code=template)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == ["ACCOUNT_LOCKED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", ["yaml_without_the_field", "stored_without_the_field", "renders_blank"])
async def test_without_an_error_code_the_result_is_unchanged(run_context: WorkflowRunContext, definition: str) -> None:
    run_context.values["blank_code"] = "  "
    reason = "No account matches {{ account_number }}"
    if definition == "renders_blank":
        block = _terminate_block(reason, error_code="{{ blank_code }}")
    else:
        block = _terminate_block(reason)
    if definition == "stored_without_the_field":
        stored = block.model_dump(mode="json")
        stored.pop("error_code", None)
        block = TerminateBlock.model_validate(stored)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "No account matches A-42"
    assert result.output_parameter_value == {"reason": "No account matches A-42"}
    assert result.error_codes == []
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_name, secret, error_code",
    [
        ("pin", "1234", "AUTH{{ pin }}"),
        ("token", "hunter2", "ERR{{ token }}"),
        ("token_with_newline", "hunter2\n", "AUTH{{ token_with_newline }}"),
        ("password", "hunter2-portal-password", "LOGIN_FAILED_{{ password }}"),
        ("encoded", "<ab", "AUTH{{ encoded }}"),
    ],
)
async def test_embedded_registered_secret_never_reaches_error_codes(
    run_context: WorkflowRunContext, secret_name: str, secret: str, error_code: str
) -> None:
    run_context.secrets[secret_name] = secret
    run_context.values[secret_name] = "&lt;ab" if secret_name == "encoded" else secret
    block = _terminate_block("stop", error_code=error_code)
    if secret_name == "encoded":
        assert block.render_templatable_field("error_code", error_code, run_context) == "AUTH&lt;ab"

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert secret not in (result.failure_reason or "")
    assert result.output_parameter_value["error_code_dropped"] is True
    assert result.output_parameter_value["reason"] == result.failure_reason
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded_code",
    [
        pytest.param("%3cab", id="lowercase_percent"),
        pytest.param("%253cab", id="double_percent"),
        pytest.param("&amp;lt;ab", id="nested_html"),
        pytest.param("%" + "25" * 5 + "3cab", id="deep_percent"),
        pytest.param(r"\u003cab", id="json_unicode_escape"),
    ],
)
async def test_nested_encoded_secret_never_reaches_error_codes(
    run_context: WorkflowRunContext, encoded_code: str
) -> None:
    run_context.secrets["code"] = "<ab"
    run_context.values["code"] = encoded_code
    block = _terminate_block("stop", error_code="AUTH{{ code }}")
    assert block.render_templatable_field("error_code", "AUTH{{ code }}", run_context) == f"AUTH{encoded_code}"

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields, retry_on, retries",
    [
        pytest.param(
            {"error_code": "ACCOUNT_LOCKED"},
            [{"status": "terminated", "error_codes": ["ACCOUNT_LOCKED"]}],
            True,
            id="with_the_rule_code",
        ),
        pytest.param({}, [{"status": "terminated", "error_codes": ["ACCOUNT_LOCKED"]}], False, id="without_a_code"),
        pytest.param(
            {"error_code": "{{ oversized_code }}"},
            [{"status": "failed"}],
            False,
            id="bad_code_does_not_retry_as_failed",
        ),
    ],
)
async def test_retry_policy_uses_terminate_status_and_code(
    run_context: WorkflowRunContext,
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, str],
    retry_on: list[dict[str, Any]],
    retries: bool,
) -> None:
    policy = WorkflowRetryPolicy(retry_on=retry_on)
    run_context.values["oversized_code"] = "X" * 129
    now = datetime.now(UTC).replace(tzinfo=None)
    run = WorkflowRun(
        workflow_run_id="run-id",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        organization_id="org-id",
        status=WorkflowRunStatus.running,
        created_at=now,
        modified_at=now,
    )
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=run.workflow_run_id,
                workflow_id=run.workflow_id,
                workflow_permanent_id=run.workflow_permanent_id,
                organization_id=run.organization_id,
                status="running",
            )
        )
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=run.workflow_run_id,
                organization_id=run.organization_id,
                attempt_number=1,
                status="running",
            )
        )
        session.add(
            WorkflowModel(
                workflow_id=run.workflow_id,
                workflow_permanent_id=run.workflow_permanent_id,
                organization_id=run.organization_id,
                title="Terminate with a retry rule",
                version=1,
                is_saved_task=False,
                workflow_definition=WorkflowDefinition(parameters=[], blocks=[], retry_policy=policy).model_dump(
                    mode="json"
                ),
            )
        )
        await session.commit()
    # The block row is written and read back through the real repository, so the code travels the
    # same persisted column the retry decision reads.
    tasks = TasksRepository(session_factory)
    observer = ObserverRepository(session_factory, task_reader=tasks)
    monkeypatch.setattr(app.DATABASE, "tasks", tasks)
    monkeypatch.setattr(app.DATABASE, "observer", observer)
    monkeypatch.setattr(app.DATABASE, "workflows", WorkflowsRepository(session_factory))
    monkeypatch.setattr(app.DATABASE, "workflow_run_attempts", WorkflowRunAttemptsRepository(session_factory))
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
    block_row = await observer.create_workflow_run_block(
        workflow_run_id=run.workflow_run_id,
        organization_id=run.organization_id,
        label="stop",
        block_type=BlockType.TERMINATE,
    )
    block = _terminate_block("The account is locked", **fields)

    result = await block.execute(run.workflow_run_id, block_row.workflow_run_block_id, organization_id="org-id")
    run_status, run_failure_reason, _ = WorkflowService._resolve_block_terminal_outcome(
        block=block, block_result=result
    )
    assert run_status == WorkflowRunStatus.terminated
    decision = await on_terminal_transition(run, run_status, run_failure_reason, None)

    assert decision.retry is retries


@pytest.mark.asyncio
async def test_terminate_inside_a_loop_stops_the_loop_helper_on_that_iteration(run_context: WorkflowRunContext) -> None:
    stop = _terminate_block("ACCOUNT_NOT_FOUND: {{ account_number }}")
    pause = WaitBlock(
        label="pause", output_parameter=make_block_output_parameter(), wait_sec=1, continue_on_failure=True
    )
    loop = ForLoopBlock(
        label="each_account",
        output_parameter=make_block_output_parameter(),
        loop_blocks=[stop, pause],
        continue_on_failure=True,
        next_loop_on_failure=True,
    )
    pause_result = await pause.build_block_result(success=True, failure_reason=None, status=BlockStatus.completed)

    async def run_child(self: Block, workflow_run_id: str, **kwargs: Any) -> Any:
        # The terminate block runs for real; the wait block is stubbed so the test needs no browser.
        if isinstance(self, TerminateBlock):
            return await self.execute(workflow_run_id, "stop-block-id", organization_id=kwargs["organization_id"])
        return pause_result

    with (
        patch.object(Block, "execute_safe", autospec=True, side_effect=run_child),
        patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
        patch.object(ForLoopBlock, "_snapshot_loop_baseline_pages", new_callable=AsyncMock, return_value=None),
        patch.object(ForLoopBlock, "_reset_browser_tabs_for_iteration", new_callable=AsyncMock),
        patch.object(ForLoopBlock, "_persist_partial_loop_output", new_callable=AsyncMock),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
    ):
        mock_skyvern_ctx.current.return_value = None
        run_context.cancel_failure_evidence_capture = AsyncMock()  # type: ignore[method-assign]
        result = await loop.execute_loop_helper(
            workflow_run_id="run-id",
            workflow_run_block_id="loop-block-id",
            workflow_run_context=run_context,
            loop_over_values=["A-1", "A-42"],
            organization_id="org-id",
        )

    # The first iteration's terminate ended the loop: no wait block ran and no second iteration started.
    assert [r.status for r in result.block_outputs] == [BlockStatus.terminated]
    assert len(result.outputs_with_loop_values) == 1
    assert result.can_continue_after_failure() is False
    loop_status, _, _ = result.resolve_status(parent_next_loop_on_failure=True)
    assert loop_status is BlockStatus.terminated


@pytest.mark.parametrize("reason", ["", "   "])
def test_blank_reason_is_rejected_at_the_schema(reason: str) -> None:
    with pytest.raises(ValidationError):
        TerminateBlockYAML(label="stop", reason=reason)


@pytest.mark.parametrize("error_code", ["", "   ", "X" * 129, "ACCOUNT\nNOT_FOUND", "ACCOUNT LOCKED"])
def test_unusable_error_code_is_rejected_at_the_schema(error_code: str) -> None:
    with pytest.raises(ValidationError):
        TerminateBlockYAML(label="stop", reason="stop", error_code=error_code)


@pytest.mark.parametrize(
    "error_code",
    [
        "{{ outcome_code }}",
        "{% if locked %}ACCOUNT_LOCKED{% else %}ACCOUNT_MISSING{% endif %}",
        "{# note #}ACCOUNT_LOCKED",
    ],
)
def test_templated_error_code_is_accepted_at_the_schema(error_code: str) -> None:
    assert TerminateBlockYAML(label="stop", reason="stop", error_code=error_code).error_code == error_code


def test_error_code_survives_yaml_to_model_to_yaml() -> None:
    stored = _terminate_block("stop", error_code="  ACCOUNT_NOT_FOUND ").model_dump(mode="json")

    assert stored["error_code"] == "ACCOUNT_NOT_FOUND"
    assert TerminateBlock.model_validate(stored).error_code == "ACCOUNT_NOT_FOUND"
    resaved = TerminateBlockYAML(label=stored["label"], reason=stored["reason"], error_code=stored["error_code"])
    assert resaved.error_code == "ACCOUNT_NOT_FOUND"
