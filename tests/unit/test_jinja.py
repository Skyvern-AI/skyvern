from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from skyvern.forge.sdk.workflow.exceptions import (
    CodeBlockTemplateSyntaxError,
    FailedToFormatJinjaStyleParameter,
    MissingJinjaVariables,
)
from skyvern.forge.sdk.workflow.models._jinja import (
    mask_jinja_in_python_comments,
    restore_jinja_masked_comments,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlock, render_failure_reason, render_failure_record
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockResult, BlockStatus
from tests.unit.conftest import use_templating_strictness


def _round_trip(source: str) -> str:
    masked, comments = mask_jinja_in_python_comments(source)
    return restore_jinja_masked_comments(masked, comments)


def test_comment_with_jinja_delimiters_is_hidden_from_the_template() -> None:
    source = "# code blocks use {{ }} templating for the same reason.\nvalue = 1\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert "{{" not in masked
    assert len(comments) == 1
    assert restore_jinja_masked_comments(masked, comments) == source


def test_masking_preserves_line_count_and_code_columns() -> None:
    source = "a = 1  # {{ }}\nb = 2\n"

    masked, _ = mask_jinja_in_python_comments(source)

    assert masked.splitlines()[0].startswith("a = 1  #")
    assert len(masked.splitlines()) == len(source.splitlines())


def test_source_without_jinja_delimiters_is_untouched() -> None:
    source = "# a plain comment\nvalue = {'a': 1}\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_comment_without_delimiters_is_left_in_place() -> None:
    source = "# plain\nraw = '''{{ email }}'''  # keep me\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert "# plain" in masked
    assert "# keep me" in masked
    assert comments == {}
    assert "{{ email }}" in masked


def test_hash_inside_a_string_literal_is_not_masked() -> None:
    source = 'selector = "#id-{{ }}"\n'

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_untokenizable_source_is_returned_unchanged() -> None:
    source = "def broken(:\n  # {{ }}\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_multiple_masked_comments_round_trip() -> None:
    source = "# first {{ }}\nvalue = 1  # second {% bad %}\n# third {# unclosed\n"

    assert _round_trip(source) == source


def test_restore_handles_a_duplicated_sentinel() -> None:
    masked, comments = mask_jinja_in_python_comments("# {{ }}\n")
    sentinel = next(iter(comments))

    restored = restore_jinja_masked_comments(f"{sentinel}\n{sentinel}\n", comments)

    assert restored == "# {{ }}\n# {{ }}\n"


def _code_block(code: str) -> CodeBlock:
    now = datetime.now()
    return CodeBlock(
        label="prefill",
        code=code,
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="prefill_output",
            description=None,
            output_parameter_id="op-1",
            workflow_id="w-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        ),
    )


def _mock_context(values: dict[str, object]) -> MagicMock:
    context = MagicMock()
    context.values = values
    context.secrets = {}
    context.include_secrets_in_templates = False
    context.get_block_metadata = MagicMock(return_value={})
    return context


def test_code_block_renders_despite_bare_jinja_braces_in_a_comment() -> None:
    block = _code_block(
        "# code blocks use {{ }} templating for the same reason.\nraw_email = r'''{{ email }}'''\nvalue = 1\n"
    )

    block.format_potential_template_parameters(_mock_context({"email": "a@b.com"}))

    assert "raw_email = r'''a@b.com'''" in block.code
    assert "# code blocks use {{ }} templating for the same reason." in block.code


def test_code_block_does_not_substitute_values_into_comments() -> None:
    block = _code_block("# Plain {{ email }} renders a value.\nraw_email = r'''{{ email }}'''\n")

    block.format_potential_template_parameters(_mock_context({"email": "a@b.com"}))

    assert "# Plain {{ email }} renders a value." in block.code
    assert "raw_email = r'''a@b.com'''" in block.code


def test_code_block_still_raises_when_the_executable_source_is_unrenderable() -> None:
    block = _code_block("raw = r'''{{ }}'''\n")

    with pytest.raises(FailedToFormatJinjaStyleParameter):
        block.format_potential_template_parameters(_mock_context({}))


def test_code_block_template_validation_reports_the_executable_source_line() -> None:
    block = _code_block("# {{ }} is inert\nvalue = 1\nraw = r'''{{ }}'''\n")

    with pytest.raises(CodeBlockTemplateSyntaxError) as excinfo:
        block.validate_code_template()

    assert excinfo.value.block_label == "prefill"
    assert excinfo.value.line == 3


def test_code_block_template_validation_ignores_jinja_delimiters_in_comments() -> None:
    block = _code_block("# code blocks use {{ }} templating\nvalue = '{{ email }}'\n")

    block.validate_code_template()


@pytest.mark.asyncio
async def test_code_block_marks_runtime_template_failure_as_nonrecoverable() -> None:
    block = _code_block("value = {{ }}")
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())

    async def failure_result(*args: object, **kwargs: object) -> BlockResult:
        return BlockResult(
            success=False,
            output_parameter=block.output_parameter,
            status=BlockStatus.failed,
            can_continue_after_failure=bool(kwargs["can_continue_after_failure"]),
        )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block", new_callable=AsyncMock),
        patch.object(CodeBlock, "get_workflow_run_context", return_value=_mock_context({})),
        patch.object(CodeBlock, "get_or_create_browser_state", new_callable=AsyncMock, return_value=browser_state),
        patch.object(CodeBlock, "_ensure_run_recording_artifact", new_callable=AsyncMock),
        patch.object(CodeBlock, "_template_format_failure_result", side_effect=failure_result),
    ):
        result = await block._execute("wr_test", "wrb_test", organization_id="org_test")

    assert result.can_continue_after_failure is False


@pytest.mark.parametrize(
    ("strictness", "code", "values", "kind", "expression", "line"),
    [
        (
            "lax",
            "# reads {{ a }}\nrows = {{ a.values | tojson }}",
            {"a": {"values": [1]}},
            "Wrong value type",
            "a.values | tojson",
            2,
        ),
        (
            "strict",
            "rows = {{ a.values | tojson }}",
            {"a": {"values": [1]}},
            "Wrong value type",
            "a.values | tojson",
            1,
        ),
        (
            "lax",
            "x = {{ opt | default('') }}{{ a.output.x.y }}",
            {"a": {"output": {}}},
            "Missing key or attribute",
            "opt | default(...); a.output.x.y",
            1,
        ),
        (
            "strict",
            "x = {{ ok }}\ny = {{ a.missing | int }}",
            {"ok": 1, "a": {}},
            "Missing key or attribute",
            None,
            None,
        ),
        ("lax", "x = {{ a ! }}", {"a": 1}, "Syntax error", None, 1),
        (
            "lax",
            "x = {{ a.b + 'literal-text-77' ~ 31337 }}",
            {"a": {"b": 1}},
            "Wrong value type",
            "a.b + ... ~ ...",
            1,
        ),
        (
            "lax",
            "x = {% if a.b.c %}1{% endif %}{{ ok }}",
            {"a": {}, "ok": 1},
            "Missing key or attribute",
            "if a.b.c; ok",
            1,
        ),
        ("lax", "x = {{ a.__class__.__mro__ }}", {"a": {}}, "Unsafe template operation", "a.__class__.__mro__", 1),
    ],
)
def test_code_block_render_failure_names_kind_expression_and_line(
    monkeypatch: pytest.MonkeyPatch,
    strictness: str,
    code: str,
    values: dict[str, object],
    kind: str,
    expression: str | None,
    line: int | None,
) -> None:
    use_templating_strictness(monkeypatch, strictness)
    block = _code_block(code)

    with pytest.raises((FailedToFormatJinjaStyleParameter, MissingJinjaVariables)) as excinfo:
        block.format_potential_template_parameters(_mock_context(values))

    record = render_failure_record(excinfo.value, lambda value: value)
    assert record == {"kind": kind, "field": "code", "expression": expression, "line": line}
    reason = render_failure_reason("Failed to format CodeBlock parameters.", excinfo.value, lambda value: value)
    assert reason.startswith(f"Failed to format CodeBlock parameters. {kind}")
    if excinfo.value.__cause__ is not None:
        assert str(excinfo.value.__cause__) not in reason


def test_a_jinja_error_raised_before_rendering_names_its_kind_and_line() -> None:
    with pytest.raises(TemplateSyntaxError) as excinfo:
        SandboxedEnvironment().parse("Stopped because\n{{ result.count ")

    reason = render_failure_reason("Failed to format jinja template:", excinfo.value, lambda value: value)
    assert reason == "Failed to format jinja template: Syntax error (line 2)."
