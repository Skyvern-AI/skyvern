from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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
from skyvern.forge.sdk.workflow.models.block import Block, CodeBlock, ForLoopBlock, WaitBlock
from skyvern.forge.sdk.workflow.models.terminate_block import TerminateBlock
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import on_terminal_transition
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, TerminateBlockYAML, WorkflowRetryPolicy
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
async def test_reason_referencing_registered_secret_uses_fallback(run_context: WorkflowRunContext) -> None:
    run_context.secrets["token"] = "hunter2"
    run_context.values["token"] = "hunter2"
    block = _terminate_block("{{ token|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("Terminated by the stop block.")
    assert "Reason was dropped" in result.failure_reason
    assert "2retnuh" not in result.failure_reason
    assert result.output_parameter_value["reason"] == result.failure_reason


@pytest.mark.asyncio
async def test_dropping_reason_does_not_mutate_block_metadata_from_context_values(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["token"] = "hunter2"
    run_context.values["stop"] = {"token": "hunter2"}
    run_context.update_block_metadata("stop", {"existing": "metadata"})
    block = _terminate_block("{{ stop.token|reverse }}")
    metadata_before = run_context.get_block_metadata(block.label).copy()

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.failure_reason is not None and "Reason was dropped" in result.failure_reason
    assert run_context.get_block_metadata(block.label) == metadata_before


@pytest.mark.asyncio
async def test_reason_fallback_does_not_persist_a_secret_bearing_label(run_context: WorkflowRunContext) -> None:
    run_context.secrets["token"] = "hunter2"
    run_context.values["token"] = "hunter2"
    block = _terminate_block("{{ token|reverse }}")
    block.label = "HUNTER2"

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    persisted_output = app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter.await_args.kwargs[
        "value"
    ]
    assert "hunter2" not in (result.failure_reason or "").casefold()
    assert "hunter2" not in persisted_output["reason"].casefold()
    assert persisted_output["reason"] == result.failure_reason == result.output_parameter_value["reason"]


@pytest.mark.asyncio
async def test_reason_assembling_registered_secret_uses_fallback(run_context: WorkflowRunContext) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.values.update(prefix="hunt", suffix="er2")
    block = _terminate_block("{{ (prefix ~ suffix)|upper }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert (
        result.failure_reason
        == "Terminated by the stop block. Reason was dropped because it was unusable after rendering."
    )
    assert result.output_parameter_value["reason"] == result.failure_reason
    assert "HUNTER2" not in result.failure_reason


@pytest.mark.asyncio
async def test_short_secret_collision_does_not_drop_rendered_reason(run_context: WorkflowRunContext) -> None:
    run_context.secrets["card_expiry"] = "09"
    block = _terminate_block("Checkout declined: HTTP 409 Conflict")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "Checkout declined: HTTP 409 Conflict"


@pytest.mark.asyncio
async def test_short_secret_collision_in_referenced_value_does_not_drop_error_code(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["card_expiry"] = "09"
    run_context.values["order"] = {"message": "HTTP 409 Conflict"}
    block = _terminate_block("stop", error_code="{% if order.message %}PAYMENT_FAILED{% endif %}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == ["PAYMENT_FAILED"]
    assert "error_code_dropped" not in result.output_parameter_value


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
    "secret, error_code",
    [
        ("True", "{{ true|string|reverse }}"),
        ("None", "{{ none|string|reverse }}"),
    ],
)
async def test_boolean_and_none_constants_do_not_render_registered_secrets(
    run_context: WorkflowRunContext, secret: str, error_code: str
) -> None:
    run_context.secrets["registered"] = secret
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, value, error_code",
    [
        ("token", "hunter2", "AUTH_{{ token|upper }}"),
        ("token", "hunter2", "{{ token|reverse }}"),
        ("token", "secret_token_id", "AUTH_{{ token|upper }}"),
        ("token", "AUTH-hunter2", "{{ token|upper }}"),
        ("credential", {"password": "secret_token_id"}, "{{ credential.password|upper }}"),
    ],
)
async def test_templated_error_code_referencing_registered_secret_is_dropped(
    run_context: WorkflowRunContext, name: str, value: Any, error_code: str
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.values[name] = value
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret, value",
    [
        ("1234", 1234),
        ("123456", 123456),
        ("12.34", 12.34),
        ("True", True),
        ("12.34", Decimal("12.34")),
    ],
)
async def test_templated_error_code_drops_non_string_secret_references(
    run_context: WorkflowRunContext, secret: str, value: int | float | bool | Decimal
) -> None:
    run_context.secrets["pin"] = secret
    run_context.values["resp_num"] = {"pin": value}
    block = _terminate_block("stop", error_code="{{ resp_num.pin|string|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("template_kind", ["reference", "literal"])
async def test_short_secret_encoding_is_dropped_before_rendering(
    run_context: WorkflowRunContext, template_kind: str
) -> None:
    run_context.secrets["pin"] = "123"
    if template_kind == "reference":
        run_context.values["response"] = {"pin": "MTIz"}
        template = "{{ response.pin|reverse }}"
    else:
        template = "{{ 'MTIz'|reverse }}"
    block = _terminate_block(template, error_code=template)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.failure_reason.startswith("Terminated by the stop block.")
    assert "Reason was dropped" in result.failure_reason
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "{{ response.error_code }}",
        '{{ response.get("error_code") }}',
        '{{ response.get("error_code", "") }}',
        "{{ response[field] }}",
    ],
)
async def test_error_code_template_ignores_unreferenced_secret_sibling(
    run_context: WorkflowRunContext, error_code: str
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.values["response"] = {"error_code": "ACCOUNT_LOCKED", "access_token": "secret_token_id"}
    run_context.values["field"] = "error_code"
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == ["ACCOUNT_LOCKED"]
    assert "error_code_dropped" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_error_code_template_ignores_secret_collision_in_variable_name(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["secret"] = "code"
    run_context.values["error_code"] = "ACCOUNT_LOCKED"
    block = _terminate_block("stop", error_code="{{ error_code }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == ["ACCOUNT_LOCKED"]
    assert "error_code_dropped" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_error_code_template_drops_secret_from_mapping_method(run_context: WorkflowRunContext) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.values["response"] = {"access_token": "hunter2"}
    block = _terminate_block("stop", error_code='{{ response.get("access_token")|upper }}')

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_error_code_template_drops_condition_only_secret_name_reference(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["pw"] = "hunter2"
    run_context.values["pw"] = "placeholder"
    block = _terminate_block("stop", error_code="{% if pw %}LOCKED{% endif %}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    persisted_output = app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter.await_args.kwargs[
        "value"
    ]
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "error code" in (result.failure_reason or "").lower() and "dropped" in result.failure_reason.lower()
    assert persisted_output["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_error_code_template_drops_secret_from_loop_metadata(run_context: WorkflowRunContext) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.update_block_metadata("stop", {"current_item": "hunter2"})
    block = _terminate_block("stop", error_code="{{ current_item|upper }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "{{ 'hunter2'|upper }}",
        "{{ missing|default('hunter2')|upper }}",
        r'{{ "hunter\x32"|upper }}',
        r'{{ "hunter\x32"|replace("hunter\x32", "SAFE_CODE") }}',
        "{# hunter2 #}SAFE_CODE",
    ],
)
async def test_error_code_template_drops_registered_secret_literals(
    run_context: WorkflowRunContext, error_code: str
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()


@pytest.mark.asyncio
async def test_error_code_template_drops_numeric_registered_secret_literal(run_context: WorkflowRunContext) -> None:
    run_context.secrets["pin"] = "123456"
    block = _terminate_block("stop", error_code="{{ 123456|string|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "654321" not in result.failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code, expected_code",
    [
        ("{{ 'HTTP 409 Conflict'|replace('HTTP 409 Conflict', 'PAYMENT_FAILED') }}", "PAYMENT_FAILED"),
        ("{{ 409|string|reverse }}", "904"),
    ],
)
async def test_short_secret_source_literal_collision_does_not_drop_error_code(
    run_context: WorkflowRunContext, error_code: str, expected_code: str
) -> None:
    run_context.secrets["card_expiry"] = "09"
    block = _terminate_block("stop", error_code=error_code)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == [expected_code]
    assert "error_code_dropped" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_short_secret_in_condition_does_not_drop_reason_or_error_code(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["card_expiry"] = "12"
    run_context.values["attempt"] = 120
    template = "{% if attempt == 12 %}ACCOUNT_LOCKED{% else %}ACCOUNT_FAILED{% endif %}"
    block = _terminate_block(template, error_code=template)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "ACCOUNT_FAILED"
    assert result.error_codes == ["ACCOUNT_FAILED"]
    assert "error_code_dropped" not in result.output_parameter_value


@pytest.mark.asyncio
async def test_short_secret_equal_integer_reference_drops_error_code(run_context: WorkflowRunContext) -> None:
    run_context.secrets["card_expiry"] = "12"
    run_context.values["attempt"] = 12
    template = "{% if attempt == 12 %}ACCOUNT_LOCKED{% else %}ACCOUNT_FAILED{% endif %}"
    block = _terminate_block("stop", error_code=template)

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    persisted_output = app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter.await_args.kwargs[
        "value"
    ]
    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert "error code" in result.failure_reason.lower() and "dropped" in result.failure_reason.lower()
    assert persisted_output == result.output_parameter_value
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("secret, literal", [("12", "12"), ("123", "MTIz")])
async def test_short_secret_in_source_data_token_drops_reason(
    run_context: WorkflowRunContext, secret: str, literal: str
) -> None:
    run_context.secrets["registered"] = secret
    run_context.values["prefix"] = "PREFIX"
    block = _terminate_block(f"{{{{ prefix }}}}{literal}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason.startswith("Terminated by the stop block.")
    assert "Reason was dropped" in result.failure_reason
    assert result.output_parameter_value["reason"] == result.failure_reason


@pytest.mark.asyncio
async def test_error_code_template_drops_exact_short_numeric_output_constant(run_context: WorkflowRunContext) -> None:
    run_context.secrets["pin"] = "1234"
    block = _terminate_block("stop", error_code="{{ 1234|string|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
async def test_reason_drops_floored_secret_contained_in_reference(run_context: WorkflowRunContext) -> None:
    run_context.secrets["token"] = "hunter2-token"
    run_context.values["resp"] = {"auth": "Bearer hunter2-token"}
    block = _terminate_block("{{ resp.auth|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.failure_reason.startswith("Terminated by the stop block.")
    assert "Reason was dropped" in result.failure_reason
    assert result.output_parameter_value["reason"] == result.failure_reason


@pytest.mark.asyncio
async def test_short_registered_secret_reference_is_still_dropped(run_context: WorkflowRunContext) -> None:
    run_context.secrets["card_expiry"] = "12"
    run_context.values["token"] = "12"
    block = _terminate_block("stop", error_code="{{ token|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_short_secret_in_decoded_constant_is_dropped(run_context: WorkflowRunContext) -> None:
    run_context.secrets["card_expiry"] = "12"
    block = _terminate_block("{{ '12'|reverse }}", error_code="{{ '12'|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason.startswith("Terminated by the stop block.")
    assert "21" not in result.failure_reason
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template_variable",
    ["workflow_title", "workflow_id", "workflow_permanent_id", "workflow_run_id", "browser_session_id"],
)
async def test_error_code_template_drops_secret_from_renderer_builtins(
    run_context: WorkflowRunContext, template_variable: str
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    setattr(run_context, template_variable, "hunter2")
    block = _terminate_block("stop", error_code=f"{{{{ {template_variable}|upper }}}}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_error_code_template_guard_uses_renderer_workflow_title(run_context: WorkflowRunContext) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.workflow_title = "hunter2"
    block = _terminate_block("stop", error_code="{{ workflow_title|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_error_code_template_guard_checks_referenced_workflow_run_summary(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.workflow_run_outputs["previous"] = {"failure_reason": "hunter2"}
    block = _terminate_block("stop", error_code="{{ workflow_run_summary.failure_reason|reverse }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_post_render_error_code_check_ignores_case_for_combined_values(
    run_context: WorkflowRunContext,
) -> None:
    run_context.secrets["secret_token_id"] = "hunter2"
    run_context.values.update(prefix="hunt", suffix="er2")
    block = _terminate_block("stop", error_code="{{ (prefix ~ suffix)|upper }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


@pytest.mark.asyncio
async def test_error_code_template_drops_encoded_secret_before_filter(run_context: WorkflowRunContext) -> None:
    run_context.secrets["secret_token_id"] = "abcdef"
    run_context.values["token"] = "YWJjZGVm"
    block = _terminate_block("stop", error_code="{{ token|upper }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.error_codes == []
    assert result.output_parameter_value["error_code_dropped"] is True


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


def test_a_declared_error_on_a_sign_in_form_keeps_its_category_first() -> None:
    output_parameter = make_block_output_parameter("code_1")
    block = CodeBlock(label="code_1", code="pass", output_parameter=output_parameter)
    declared = [{"category": "LOGIN_FAILED", "confidence_float": 1.0, "reasoning": "login page never loaded"}]
    result = BlockResult(
        success=False,
        output_parameter=output_parameter,
        output_parameter_value={"failure_category": declared},
        status=BlockStatus.failed,
        failure_reason="login page never loaded",
        sign_in_form_visible=True,
    )

    _, _, run_category = WorkflowService._resolve_block_terminal_outcome(block=block, block_result=result)

    assert run_category is not None
    assert run_category[:-1] == declared
    assert (run_category[-1]["category"], run_category[-1]["reason_code"]) == (
        "WRONG_PAGE_STATE",
        "sign_in_form_visible",
    )
