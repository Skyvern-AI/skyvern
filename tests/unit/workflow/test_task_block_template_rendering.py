from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock, HumanInteractionBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.utils.templating import MAX_AVAILABLE_KEY_LENGTH, get_available_keys


def _make_output_parameter(key: str = "task_output") -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id="op_task_template_test",
        workflow_id="w_task_template_test",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_workflow_run_context(values: dict[str, Any] | None = None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_task_template_test",
        workflow_permanent_id="wpid_task_template_test",
        workflow_run_id="wr_task_template_test",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


def test_format_potential_template_parameters_renders_error_code_mapping() -> None:
    block = TaskBlock(
        label="task_with_error_codes",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping={
            "ERR_{{ region }}": "{{ reason }} for {{ region }}",
            "STATIC_CODE": "static description",
        },
    )
    ctx = _make_workflow_run_context({"region": "US", "reason": "login failed"})

    block.format_potential_template_parameters(ctx)

    assert block.error_code_mapping == {
        "ERR_US": "login failed for US",
        "STATIC_CODE": "static description",
    }


def test_format_potential_template_parameters_with_no_error_code_mapping() -> None:
    block = TaskBlock(
        label="task_without_error_codes",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping=None,
    )
    ctx = _make_workflow_run_context({"region": "US"})

    block.format_potential_template_parameters(ctx)

    assert block.error_code_mapping is None


def test_malformed_jinja_in_title_raises_with_template_context() -> None:
    """Syntax error in title template should raise FailedToFormatJinjaStyleParameter with the template string."""
    block = TaskBlock(
        label="bad_title",
        output_parameter=_make_output_parameter(),
        title="{{ unclosed",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="unclosed"):
        block.format_potential_template_parameters(ctx)


def test_malformed_jinja_in_navigation_goal_raises_with_template_context() -> None:
    """Syntax error in navigation_goal should raise FailedToFormatJinjaStyleParameter."""
    block = TaskBlock(
        label="bad_nav",
        output_parameter=_make_output_parameter(),
        title="ok title",
        navigation_goal="{{ {% bad }}",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="bad"):
        block.format_potential_template_parameters(ctx)


def test_malformed_jinja_in_error_code_mapping_raises_with_template_context() -> None:
    """Syntax error in error_code_mapping value should raise FailedToFormatJinjaStyleParameter."""
    block = TaskBlock(
        label="bad_ecm",
        output_parameter=_make_output_parameter(),
        title="ok title",
        error_code_mapping={"ERR_1": "{{ unclosed"},
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="unclosed"):
        block.format_potential_template_parameters(ctx)


def test_render_error_raises_with_template_context() -> None:
    """A template that compiles but fails at render time should also raise with template context."""
    block = TaskBlock(
        label="render_err",
        output_parameter=_make_output_parameter(),
        title="{{ foo | no_such_filter }}",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="no_such_filter"):
        block.format_potential_template_parameters(ctx)


def test_jinja_render_failure_carries_available_keys() -> None:
    block = TaskBlock(
        label="missing_ref",
        output_parameter=_make_output_parameter(),
        title="{{ a_output.missing_key.id }}",
    )
    ctx = _make_workflow_run_context({"a_output": {"status": "completed", "url": "https://example.test/"}})

    with pytest.raises(FailedToFormatJinjaStyleParameter) as exc_info:
        block.format_potential_template_parameters(ctx)

    assert {"a_output.status", "a_output.url"}.issubset(set(exc_info.value.available_keys))
    assert str(exc_info.value).startswith(
        "Failed to format Jinja style parameter '{{ a_output.missing_key.id }}'. Reason: "
    )
    assert "available_keys" not in str(exc_info.value)


def test_jinja_render_failure_available_keys_walks_list_index() -> None:
    block = TaskBlock(
        label="missing_list_ref",
        output_parameter=_make_output_parameter(),
        title="{{ items[0].missing.id }}",
    )
    ctx = _make_workflow_run_context({"items": [{"sku": "a", "qty": 2}]})

    with pytest.raises(FailedToFormatJinjaStyleParameter) as exc_info:
        block.format_potential_template_parameters(ctx)

    assert {"items[0].sku", "items[0].qty"}.issubset(set(exc_info.value.available_keys))


def test_jinja_literal_braces_survive_undeclared_field_and_parameter_value() -> None:
    block = TaskBlock(
        label="literal_{{ not_a_var }}",
        output_parameter=_make_output_parameter(),
        title="ok title",
        navigation_goal="{{ start_url }}",
    )
    parameter_value = "https://example.test/?q=%7B%7Bx%7D%7D&r={%22a%22}&s={{ not_a_var }}"
    ctx = _make_workflow_run_context({"start_url": parameter_value})

    assert block.render_templatable_field("label", block.label, ctx) == "literal_{{ not_a_var }}"

    block.format_potential_template_parameters(ctx)
    assert block.navigation_goal == parameter_value

    with pytest.raises(ValueError, match="no field named"):
        block.render_templatable_field("not_a_real_field", "x", ctx)


def test_jinja_inherited_templatable_fields_still_render() -> None:
    human = HumanInteractionBlock(
        label="human",
        output_parameter=_make_output_parameter("human_output"),
        title="{{ region }} title",
        instructions="{{ region }} instructions",
    )
    download = FileDownloadBlock(
        label="download",
        output_parameter=_make_output_parameter("download_output"),
        title="{{ region }} title",
        path="/tmp/{{ region }}",
    )
    ctx = _make_workflow_run_context({"region": "US"})

    human.format_potential_template_parameters(ctx)
    download.format_potential_template_parameters(ctx)
    download._format_destination_template_parameters(ctx)

    assert (human.title, human.instructions) == ("US title", "US instructions")
    assert (download.title, download.path) == ("US title", "/tmp/US")


def test_jinja_failure_output_never_merges_across_loop_iterations() -> None:
    ctx = _make_workflow_run_context()
    output_parameter = _make_output_parameter("task_output")
    failure = {"failure_reason": "Failed to format jinja template: boom", "available_keys": ["status"]}

    ctx.register_block_reference_variable_from_output_parameter(
        output_parameter, {"status": "completed", "extracted_information": {"sku": "a"}}
    )
    ctx.register_block_reference_variable_from_output_parameter(output_parameter, failure)
    assert ctx.values["task"] == failure

    ctx.register_block_reference_variable_from_output_parameter(
        output_parameter, {"status": "completed", "extracted_information": {"sku": "b"}}
    )
    assert "failure_reason" not in ctx.values["task"]
    assert "available_keys" not in ctx.values["task"]
    assert ctx.values["task"]["output"] == {"sku": "b"}


def test_jinja_available_keys_are_paste_able_paths_not_bare_names() -> None:
    secret = "parameter-secret-value-15419"
    long_key = "k" * (MAX_AVAILABLE_KEY_LENGTH + 1)
    template_data = {
        "a": {"token": secret, long_key: 1, "items": 2, "by region": {"north-west": 3}, 7: "seven"},
        "rows": [{"sku": "x"}],
        "top": secret,
    }

    keys = get_available_keys("{{ a.missing }} {{ rows[0].missing }}", template_data)
    env = SandboxedEnvironment(undefined=StrictUndefined)

    assert {"a.token", 'a["items"]', "rows[0].sku", "top"}.issubset(set(keys))
    # A bare child name reads as a top-level binding and renders nothing, which is what the model
    # pasted in production, so it is never published.
    assert "token" not in keys and "sku" not in keys
    assert secret not in " ".join(keys)
    assert not any(long_key in key for key in keys)
    for key in keys:
        env.from_string("{{ " + key + " }}").render(template_data)
    assert env.from_string('{{ a["items"] }}').render(template_data) == "2"
    assert "a[7]" in keys and 'a["7"]' not in keys
    assert env.from_string("{{ a[7] }}").render(template_data) == "seven"


@pytest.mark.asyncio
async def test_jinja_failure_result_surfaces_redacted_payload_on_the_block_result() -> None:
    secret = "smtp-credential-15419"
    ctx = _make_workflow_run_context()
    ctx.secrets["smtp_password"] = secret
    block = TaskBlock(label="task", output_parameter=_make_output_parameter(), title="t")
    exc = FailedToFormatJinjaStyleParameter("{{ a.missing }}", f"boom {secret}", available_keys=[secret, "status"])

    with patch.object(TaskBlock, "record_output_parameter_value", AsyncMock()) as record_output:
        result = await block._template_format_failure_result(
            exc, f"Failed to format jinja template: boom {secret}", ctx, "wr-1", None, None
        )

    assert result.output_parameter_value == record_output.await_args.args[2]
    assert result.output_parameter_value["failure_reason"] == "Failed to format jinja template: boom [redacted]"
    assert result.output_parameter_value["available_keys"] == ["[redacted]", "status"]


@pytest.mark.asyncio
async def test_jinja_failure_result_preserves_available_keys_when_exception_is_wrapped() -> None:
    secret_value = "secret-value-abc"
    ctx = _make_workflow_run_context()
    ctx.secrets["sensitive_key"] = secret_value
    block = TaskBlock(label="task", output_parameter=_make_output_parameter(), title="t")

    inner_exc = FailedToFormatJinjaStyleParameter(
        "{{ extract.missing }}", "boom", available_keys=[secret_value, "result"]
    )
    middle_exc = ValueError("wrapped error message")
    middle_exc.__cause__ = inner_exc
    wrapped_exc = RuntimeError("wrapped twice")
    wrapped_exc.__cause__ = middle_exc

    with patch.object(TaskBlock, "record_output_parameter_value", AsyncMock()) as record_output:
        result = await block._template_format_failure_result(
            wrapped_exc, "Failed to format jinja template: boom", ctx, "wr-1", None, None
        )

    assert result.output_parameter_value == record_output.await_args.args[2]
    assert result.output_parameter_value["available_keys"] == ["[redacted]", "result"]


async def _scoped_context(block_outputs: dict[str, Any]) -> WorkflowRunContext:
    return await WorkflowRunContext.init(
        aws_client=MagicMock(),
        organization=Organization(
            organization_id="o_task_template_test",
            organization_name="test",
            created_at=datetime.now(timezone.utc),
            modified_at=datetime.now(timezone.utc),
        ),
        workflow_run_id="wr_task_template_test",
        workflow_title="test",
        workflow_id="w_task_template_test",
        workflow_permanent_id="wpid_task_template_test",
        workflow_parameter_tuples=[],
        workflow_output_parameters=[],
        context_parameters=[],
        secret_parameters=[],
        block_outputs=block_outputs,
    )


def _full_run_context(label: str, value: dict[str, Any]) -> WorkflowRunContext:
    ctx = _make_workflow_run_context()
    ctx.register_block_reference_variable_from_output_parameter(_make_output_parameter(f"{label}_output"), value)
    return ctx


CARRIED_EXTRACTION_OUTPUT = {
    "status": "completed",
    "extracted_information": {"failure_rate": "25.65%"},
}


@pytest.mark.asyncio
async def test_scoped_rerun_renders_block_references_identically_to_a_full_run() -> None:
    block = TaskBlock(label="write_row", output_parameter=_make_output_parameter(), title="t")
    scoped = await _scoped_context({"extract_failure_rate": CARRIED_EXTRACTION_OUTPUT})
    full_run = _full_run_context("extract_failure_rate", CARRIED_EXTRACTION_OUTPUT)

    for expression in (
        "{{ extract_failure_rate.output.failure_rate }}",
        "{{ extract_failure_rate.extracted_information.failure_rate }}",
        "{{ extract_failure_rate.status }}",
    ):
        scoped_rendered = block.format_block_parameter_template_from_workflow_run_context(expression, scoped)
        full_run_rendered = block.format_block_parameter_template_from_workflow_run_context(expression, full_run)
        assert scoped_rendered == full_run_rendered
        assert scoped_rendered

    assert (
        block.format_block_parameter_template_from_workflow_run_context(
            "{{ extract_failure_rate.output.failure_rate }}", scoped
        )
        == "25.65%"
    )


@pytest.mark.asyncio
async def test_scoped_rerun_leaves_a_label_without_a_carried_output_undefined() -> None:
    block = TaskBlock(label="write_row", output_parameter=_make_output_parameter(), title="t")
    scoped = await _scoped_context({"extract_failure_rate": CARRIED_EXTRACTION_OUTPUT})

    assert "never_ran" not in scoped.values
    with pytest.raises((FailedToFormatJinjaStyleParameter, MissingJinjaVariables)):
        block.format_block_parameter_template_from_workflow_run_context("{{ never_ran.output }}", scoped)


@pytest.mark.asyncio
async def test_scoped_rerun_keeps_carried_labels_out_of_workflow_run_outputs() -> None:
    scoped = await _scoped_context({"extract_failure_rate": CARRIED_EXTRACTION_OUTPUT})

    assert scoped.values["extract_failure_rate"]["output"] == {"failure_rate": "25.65%"}
    assert "extract_failure_rate" not in scoped.workflow_run_outputs
    summary = scoped.build_workflow_run_summary()
    assert summary["status"] is None
    assert summary["output"] == {"extracted_information": {}}


@pytest.mark.asyncio
async def test_scoped_rerun_never_reports_a_carried_payload_as_this_runs_result() -> None:
    block = TaskBlock(label="write_row", output_parameter=_make_output_parameter(), title="t")
    carried_failure = {
        "status": "failed",
        "failure_reason": "the prior run could not open the sheet",
        "errors": [{"reason": "prior run error"}],
        "downloaded_files": [{"path": "/tmp/prior-run.csv"}],
        "extracted_information": {"failure_rate": "25.65%"},
    }
    scoped = await _scoped_context({"extract_failure_rate": carried_failure})

    summary = scoped.build_workflow_run_summary()
    assert summary["status"] is None
    assert summary["failure_reason"] is None
    assert summary["errors"] == []
    assert summary["downloaded_files"] == []
    assert summary["output"] == {"extracted_information": {}}

    assert "extract_failure_rate" not in scoped.workflow_run_outputs
    assert (
        block.format_block_parameter_template_from_workflow_run_context(
            "{{ extract_failure_rate.output.failure_rate }}", scoped
        )
        == "25.65%"
    )


@pytest.mark.asyncio
async def test_a_carried_label_that_executes_this_run_is_reported_again() -> None:
    scoped = await _scoped_context({"extract_failure_rate": CARRIED_EXTRACTION_OUTPUT})
    scoped.register_block_reference_variable_from_output_parameter(
        _make_output_parameter("extract_failure_rate_output"),
        {"status": "failed", "failure_reason": "this run failed", "errors": [{"reason": "this run"}]},
    )

    summary = scoped.build_workflow_run_summary()
    assert summary["status"] == "failed"
    assert summary["failure_reason"] == "this run failed"
    assert summary["errors"] == [{"reason": "this run"}]


@pytest.mark.asyncio
async def test_a_carried_success_is_replaced_by_this_runs_success_rather_than_merged() -> None:
    carried_success = {
        "status": "completed",
        "extracted_information": {"failure_rate": "25.65%"},
        "downloaded_files": [{"path": "/tmp/prior-run.csv"}],
        "errors": [{"reason": "prior run error"}],
    }
    scoped = await _scoped_context({"extract_failure_rate": carried_success})
    scoped.register_block_reference_variable_from_output_parameter(
        _make_output_parameter("extract_failure_rate_output"),
        {"status": "completed", "extracted_information": {"failure_rate": "31.02%"}},
    )

    summary = scoped.build_workflow_run_summary()
    assert summary["downloaded_files"] == []
    assert summary["errors"] == []
    assert summary["output"] == {"extracted_information": {"failure_rate": "31.02%"}}
