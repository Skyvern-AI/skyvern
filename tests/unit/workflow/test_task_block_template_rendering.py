from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from skyvern.core.script_generations import real_skyvern_page_ai
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.models._jinja import render_templates_in_json_value
from skyvern.forge.sdk.workflow.models.block import CodeBlock, FileDownloadBlock, HumanInteractionBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.services import script_service
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER, redact_secrets_from_text
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


_SECRET = "hunter2x9"


def _context_with_registered_secret(**values: Any) -> WorkflowRunContext:
    ctx = _make_workflow_run_context({"token": _SECRET, **values})
    ctx.secrets["placeholder_tok"] = _SECRET
    return ctx


@pytest.mark.parametrize(
    ("expression", "transformed"),
    [
        ("{{ token|upper }}", "HUNTER2X9"),
        ("{{ token|reverse }}", "9x2retnuh"),
        ("{{ token|replace('2', '-') }}", "hunter-x9"),
        ("{{ token[2:] }}", "nter2x9"),
        ("{{ login.password|title }}", "Hunter2x9"),
        ("AUTH{{ token[:4] }}CODE", "AUTHhuntCODE"),
        ("{{ token|list|join('-') }}", "h-u-n-t-e-r-2-x-9"),
    ],
)
def test_secret_transformed_by_a_template_is_redacted_wherever_the_run_redacts(
    expression: str, transformed: str
) -> None:
    ctx = _context_with_registered_secret(login={"username": "someone", "password": _SECRET})
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[ctx.workflow_run_id] = ctx
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    rendered = block.render_templatable_field("navigation_goal", f"Sign in with {expression} now", ctx)

    assert rendered == f"Sign in with {transformed} now"
    run_secrets = manager.get_secret_values_for_run(ctx.workflow_run_id, respect_artifact_redaction_flag=False)
    assert redact_secrets_from_text(rendered, run_secrets) == f"Sign in with {REDACTED_SECRET_PLACEHOLDER} now"
    assert ctx.mask_secrets_in_data(rendered) == "Sign in with ***** now"


def test_error_code_mapping_built_from_a_transformed_secret_never_carries_it() -> None:
    ctx = _context_with_registered_secret()
    block = TaskBlock(
        label="sign_in",
        output_parameter=_make_output_parameter(),
        title="sign in",
        error_code_mapping={
            "AUTH_{{ token|upper }}": "bad token",
            "AUTH_EXPIRED": "Token {{ token|reverse }} expired",
        },
    )

    block.format_potential_template_parameters(ctx)

    assert block.error_code_mapping == {"AUTH_EXPIRED": "Token [redacted] expired"}


@pytest.mark.parametrize(
    ("secret", "template", "expected"),
    [
        (_SECRET, "{{ token.split('2')[1].upper() }}-SUFFIX", "X9-SUFFIX"),
    ],
)
def test_secret_output_no_canary_can_reproduce_is_registered_whole(secret: str, template: str, expected: str) -> None:
    ctx = _make_workflow_run_context({"token": secret})
    ctx.secrets["placeholder_tok"] = secret
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    rendered = block.render_templatable_field("navigation_goal", template, ctx)

    assert rendered == expected
    assert ctx.mask_secrets_in_data(rendered) == "*****"


def test_part_of_a_structured_secret_split_out_in_code_is_masked_when_it_is_returned() -> None:
    secret = "clientid:s3cr3tvalue"
    ctx = _make_workflow_run_context({"token": secret})
    ctx.secrets["placeholder_tok"] = secret
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    rendered = block.render_templatable_field("navigation_goal", "secret = \"{{ token.split(':')[1].strip() }}\"", ctx)

    assert rendered == 'secret = "s3cr3tvalue"'
    assert ctx.mask_secrets_in_data({"secret": "s3cr3tvalue"}) == {"secret": "*****"}


_FILLER = "x" * 2000


@pytest.mark.parametrize(
    ("secret", "template", "expected"),
    [
        (_SECRET, "{{ token|replace('2', '\"')|json }}", [REDACTED_SECRET_PLACEHOLDER]),
        (_SECRET, "{{ [token|upper, token|upper]|json }}", [REDACTED_SECRET_PLACEHOLDER] * 2),
        (
            _SECRET,
            "{{ [token|upper, filler, token|upper]|json }}",
            [REDACTED_SECRET_PLACEHOLDER, _FILLER, REDACTED_SECRET_PLACEHOLDER],
        ),
        ("ab-cd", "{{ [token|upper, token|upper]|json }}", [REDACTED_SECRET_PLACEHOLDER] * 2),
        (
            _SECRET,
            "{{ [token|replace('hunter', 'H'), filler, token|replace('hunter', 'H'), 'H2x9']|json }}",
            [REDACTED_SECRET_PLACEHOLDER, _FILLER, REDACTED_SECRET_PLACEHOLDER, REDACTED_SECRET_PLACEHOLDER],
        ),
        ("hunter2x9abcdef", "{{ token.split('2')[1].strip()|json }}", [REDACTED_SECRET_PLACEHOLDER]),
    ],
)
def test_secret_transformed_inside_a_json_typed_value_is_redacted_after_decoding(
    secret: str, template: str, expected: list[str]
) -> None:
    ctx = _make_workflow_run_context({"token": secret, "filler": _FILLER})
    ctx.secrets["placeholder_tok"] = secret
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[ctx.workflow_run_id] = ctx
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    decoded = render_templates_in_json_value(
        template, lambda value: block.render_templatable_field("navigation_goal", value, ctx)
    )

    values = decoded if isinstance(decoded, list) else [decoded]
    run_secrets = manager.get_secret_values_for_run(ctx.workflow_run_id, respect_artifact_redaction_flag=False)
    assert [redact_secrets_from_text(value, run_secrets) for value in values] == expected


def test_cyclic_template_data_still_renders_and_fails_closed() -> None:
    data: dict[str, Any] = {"token": _SECRET}
    data["self"] = data
    ctx = _context_with_registered_secret(data=data)
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    rendered = block.render_templatable_field("navigation_goal", "{{ data.token|upper }}", ctx)

    assert rendered == "HUNTER2X9"
    assert ctx.mask_secrets_in_data(rendered) == "*****"


@pytest.mark.parametrize("template", ["{{ token|int|json }}", "{{ ((token|int) + 1)|json }}"])
def test_numeric_json_value_from_a_secret_is_masked_in_structured_output(template: str) -> None:
    ctx = _make_workflow_run_context({"token": "123456"})
    ctx.secrets["placeholder_pin"] = "123456"
    ctx.secrets["placeholder_year"] = "2026"
    ctx.secrets["placeholder_price"] = "19.99"
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    decoded = render_templates_in_json_value(
        template, lambda value: block.render_templatable_field("navigation_goal", value, ctx)
    )

    assert isinstance(decoded, int)
    # A number under the redactor's numeric floor (a year equal to a card expiry) stays.
    assert ctx.mask_secrets_in_data({"pin": decoded, "year": 2026, "price": 19.99}) == {
        "pin": "*****",
        "year": 2026,
        "price": 19.99,
    }


def test_a_registered_slice_never_masks_part_of_the_full_secret() -> None:
    secret = "hunter2x9abc"
    ctx = _make_workflow_run_context({"token": secret})
    ctx.secrets["placeholder_tok"] = secret
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")
    for template in ("{{ token[1:] }}", "{{ token[2:] }}", "{{ token[3:] }}", "{{ token[:-1] }}", "{{ token[:-2] }}"):
        block.render_templatable_field("navigation_goal", template, ctx)

    # Masking a slice first would leave the rest of the full secret (`h*****`) behind.
    assert ctx.mask_secrets_in_data(f"key={secret}") == "key=*****"


@pytest.mark.parametrize(
    ("template", "copies"),
    [
        ("{{ token|replace('e', 'ee') }}:{{ token|replace('e', 'ee') }}", ["hunteer2x9", "hunteer2x9"]),
        ("Use {{ token|upper }} or {{ token|replace('e', 'ee') }}", ["HUNTER2X9", "hunteer2x9"]),
    ],
)
def test_copies_separated_by_a_short_delimiter_are_each_masked(template: str, copies: list[str]) -> None:
    ctx = _context_with_registered_secret()
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    block.render_templatable_field("navigation_goal", template, ctx)

    assert ctx.mask_secrets_in_data(copies) == ["*****"] * len(copies)


_CREDENTIAL = "someone:hunter2x9"


def _context_with_a_combined_credential() -> WorkflowRunContext:
    ctx = _make_workflow_run_context({"cred": _CREDENTIAL})
    ctx.secrets["placeholder_cred"] = _CREDENTIAL
    return ctx


def test_a_split_credential_in_a_json_payload_registers_only_its_own_value() -> None:
    ctx = _context_with_a_combined_credential()
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")
    template = (
        '{{ {"grant_type": "password", "password": cred.split(":")[1].strip(), "username": "someone", '
        '"scope": "openid profile"}|json }}'
    )

    decoded = render_templates_in_json_value(
        template, lambda value: block.render_templatable_field("navigation_goal", value, ctx)
    )

    assert ctx.mask_secrets_in_data(decoded) == {
        "grant_type": "password",
        "password": "*****",
        "username": "someone",
        "scope": "openid profile",
    }


def test_a_split_credential_rendered_in_a_loop_registers_one_value() -> None:
    ctx = _context_with_a_combined_credential()
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    for step in range(20):
        ctx.values["current_value"] = f"item {step}"
        block.render_templatable_field(
            "navigation_goal", "Login with {{ cred.split(':')[1].strip() }} then process {{ current_value }}", ctx
        )

    assert [value for value in ctx.secrets.values() if value != _CREDENTIAL] == ["hunter2x9"]


def test_rendering_a_secret_in_a_loop_registers_a_bounded_set_of_values() -> None:
    ctx = _context_with_registered_secret()
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    for step in range(5):
        ctx.values["step"] = step
        block.render_templatable_field("navigation_goal", "{{ token|upper }} step {{ step }} {{ token|reverse }}", ctx)

    assert sorted(value for value in ctx.secrets.values() if value != _SECRET) == ["9x2retnuh", "HUNTER2X9"]


@pytest.mark.parametrize("module", [script_service, real_skyvern_page_ai])
def test_cached_script_render_registers_a_secret_it_transformed(module: ModuleType) -> None:
    ctx = _context_with_registered_secret()
    context = SimpleNamespace(workflow_run_id=ctx.workflow_run_id, script_run_parameters={}, loop_metadata=None)
    stub_app = SimpleNamespace(WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(get_workflow_run_context=lambda _run_id: ctx))

    with patch.object(module, "app", stub_app), patch.object(module.skyvern_context, "current", return_value=context):
        rendered = module.render_template("Use {{ token|upper }} now")

    assert rendered == "Use HUNTER2X9 now"
    assert ctx.mask_secrets_in_data(rendered) == "Use ***** now"


def _code_block_with_a_declared_login() -> tuple[CodeBlock, WorkflowRunContext]:
    ctx = _make_workflow_run_context(
        {"login": {"context": "login", "username": "placeholder_u", "password": "placeholder_p"}}
    )
    ctx.secrets.update({"placeholder_u": "someone", "placeholder_p": _SECRET})
    now = datetime.now(timezone.utc)
    login = WorkflowParameter(
        workflow_parameter_id="wp_login",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        workflow_id="w_task_template_test",
        key="login",
        created_at=now,
        modified_at=now,
    )
    block = CodeBlock(
        label="call_api", code="value = 'ok'", output_parameter=_make_output_parameter(), parameters=[login]
    )
    return block, ctx


def test_credential_a_code_block_transforms_is_redacted_wherever_the_run_redacts() -> None:
    block, ctx = _code_block_with_a_declared_login()
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[ctx.workflow_run_id] = ctx

    rendered = block.format_block_parameter_template_from_workflow_run_context(
        "auth={{ login_real_password|upper }}", ctx, force_include_secrets=True
    )

    assert rendered == "auth=HUNTER2X9"
    run_secrets = manager.get_secret_values_for_run(ctx.workflow_run_id, respect_artifact_redaction_flag=False)
    assert redact_secrets_from_text(rendered, run_secrets) == f"auth={REDACTED_SECRET_PLACEHOLDER}"


@pytest.mark.parametrize(
    "between", [f"\n# {'x' * 1200}\n", f'; pad = "{"x" * 1200}"; '], ids=["separate lines", "same line"]
)
def test_credential_transformed_far_apart_in_code_is_masked_in_each_returned_value(between: str) -> None:
    block, ctx = _code_block_with_a_declared_login()
    use = "{{ login_real_password|replace('e', 'ee') }}"
    code = f'first = "{use}"{between}second = "{use}"\n'

    rendered = block.format_block_parameter_template_from_workflow_run_context(code, ctx, force_include_secrets=True)

    assert rendered.count("hunteer2x9") == 2
    # A Code block returns each local separately, so each copy must match on its own.
    assert ctx.mask_secrets_in_data({"first": "hunteer2x9", "second": "hunteer2x9"}) == {
        "first": "*****",
        "second": "*****",
    }


@pytest.mark.parametrize(
    "template",
    ["{{ response.status|upper }}", "{{ token|length }}", "{{ token[:3] }}", "abc{{ token }}def", "Account locked"],
)
def test_template_output_not_derived_from_a_secret_registers_nothing(template: str) -> None:
    ctx = _context_with_registered_secret(response={"status": "locked", "access_token": _SECRET})
    registered = dict(ctx.secrets)
    block = TaskBlock(label="sign_in", output_parameter=_make_output_parameter(), title="sign in")

    block.render_templatable_field("navigation_goal", template, ctx)

    assert ctx.secrets == registered
