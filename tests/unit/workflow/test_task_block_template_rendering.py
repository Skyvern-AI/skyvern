from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock, HumanInteractionBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.utils.templating import get_available_keys


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


def _make_workflow_run_context(values: dict | None = None) -> WorkflowRunContext:
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

    assert {"status", "url"}.issubset(set(exc_info.value.available_keys))
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

    assert {"sku", "qty"}.issubset(set(exc_info.value.available_keys))


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


def test_jinja_available_keys_are_names_only_and_bounded() -> None:
    secret = "parameter-secret-value-15419"
    long_key = "k" * 500
    keys = get_available_keys("{{ a.missing }}", {"a": {"token": secret, long_key: 1}, "top": secret})

    assert "token" in keys and "top" in keys
    assert secret not in " ".join(keys)
    assert max(len(key) for key in keys) == 128


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
