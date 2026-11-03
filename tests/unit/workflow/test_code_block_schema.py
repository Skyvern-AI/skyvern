from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockStep, ErrorCode
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import CodeBlockYAML, WorkflowDefinitionYAML, _direct_code_block_error_code_raises
from skyvern.webeye.actions.action_types import ActionType


def _output_parameter() -> OutputParameter:
    return OutputParameter(
        output_parameter_id="op_1",
        key="code_output",
        workflow_id="w_1",
        created_at="2026-01-01T00:00:00",
        modified_at="2026-01-01T00:00:00",
    )


def test_code_block_yaml_accepts_code_first_fields() -> None:
    block_yaml = CodeBlockYAML(
        label="code_1",
        code="x = 1",
        prompt="Open {{ url }} and read the total",
        steps=[
            {"description": "Open the page", "action_type": "goto_url", "line_start": 1, "line_end": 2},
            {"description": "Read the total", "action_type": "extract"},
        ],
    )
    assert block_yaml.prompt == "Open {{ url }} and read the total"
    assert block_yaml.steps is not None and block_yaml.steps[0].action_type == "goto_url"


def test_code_block_yaml_defaults_keep_legacy_shape() -> None:
    block_yaml = CodeBlockYAML(label="code_1", code="x = 1")
    assert block_yaml.prompt is None
    assert block_yaml.steps is None
    assert block_yaml.error_code_mapping is None


@pytest.mark.parametrize(
    "code",
    [
        "x = 1",
        'raise ErrorCode("A", "why")',
        'raise ErrorCode(code="A", reasoning="why")',
        "    x = 1",
        "x = (",
    ],
)
def test_runtime_workflow_definition_loads_stored_code_without_strict_ast_validation(code: str) -> None:
    WorkflowDefinition.model_validate(
        {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "code",
                    "label": "code_1",
                    "code": code,
                    "output_parameter": _output_parameter().model_dump(),
                }
            ],
        }
    )


def test_code_block_yaml_accepts_indented_valid_code() -> None:
    CodeBlockYAML(
        label="code_1",
        code="    raise ErrorCode('A', 'why')",
        error_code_mapping={"A": "Declared"},
    )


def test_code_block_manifest_round_trip_and_converter() -> None:
    mapping = {"missing-output.v1": "Missing output"}
    block_yaml = CodeBlockYAML(
        label="code_1",
        code="raise ErrorCode('missing-output.v1', 'missing')",
        error_code_mapping=mapping,
    )
    block = block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    assert block_yaml.model_dump()["error_code_mapping"] == mapping
    assert isinstance(block, CodeBlock)
    assert block.error_code_mapping == mapping
    assert "error_code" not in block.model_dump()


def test_code_block_rejects_obsolete_singular_error_code() -> None:
    with pytest.raises(ValidationError, match="error_code_mapping"):
        CodeBlockYAML(label="code_1", code="x = 1", error_code="OLD")


@pytest.mark.parametrize(
    "code",
    [
        "raise ErrorCode(code, 'reason')",
        "raise ErrorCode('declared')",
        "raise ErrorCode(error_code='declared', reasoning='reason')",
        "error = ErrorCode('declared', 'reason')\nraise error",
        "Alias = ErrorCode\nraise Alias('declared', 'reason')",
        "raise ErrorCode",
    ],
)
def test_code_block_accepts_indirect_or_malformed_error_code_raises_for_runtime(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})


def test_code_block_accepts_multiple_error_code_raises_on_one_line_for_runtime() -> None:
    code = "if condition: raise ErrorCode('A', 'why'); raise ErrorCode('B', 'why')"

    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"A": "First", "B": "Second"})


@pytest.mark.parametrize(
    "code",
    [
        "raise ErrorCode('A', 'why')",
        "if condition: raise ErrorCode('A', 'why')",
    ],
)
def test_code_block_accepts_single_error_code_raise_on_line(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"A": "Declared"})


def test_code_block_accepts_jinja_control_statements_without_error_code() -> None:
    CodeBlockYAML(label="code_1", code="{% if enabled %}\nx = 1\n{% endif %}")


def test_code_block_counts_declared_raise_with_jinja_control_statements() -> None:
    code = "{% if enabled %}\nraise ErrorCode('declared', 'reason')\n{% endif %}"

    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})

    assert _direct_code_block_error_code_raises(code) == {(2, "declared")}


def test_code_block_preserves_raise_line_after_multiline_jinja_statement() -> None:
    code = "{% if\n enabled %}\nx = 1\nraise ErrorCode('declared', 'reason')"

    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})

    assert _direct_code_block_error_code_raises(code) == {(4, "declared")}


def test_runtime_classifier_rejects_ambiguous_raises_after_multiline_jinja_statement() -> None:
    code = "{% if\n enabled %}\nif condition: raise ErrorCode('A', 'why'); raise ErrorCode('B', 'why')"

    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"A": "First", "B": "Second"})
    with pytest.raises(ValueError, match="ErrorCode"):
        _direct_code_block_error_code_raises(code)


def test_workflow_accepts_undeclared_raise_with_jinja_control_statements() -> None:
    code = "{% if enabled %}\nraise ErrorCode('missing', 'reason')\n{% endif %}"

    workflow = WorkflowDefinitionYAML(parameters=[], blocks=[CodeBlockYAML(label="code_1", code=code)])

    assert workflow.blocks[0].code == code


def test_code_block_accepts_aliased_raise_with_jinja_control_statements_for_runtime() -> None:
    code = "{% if enabled %}\nAlias = ErrorCode\nraise Alias('declared', 'reason')\n{% endif %}"

    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})


def test_code_block_schema_preserves_broken_python_for_authoring_feedback() -> None:
    code = "{% if enabled %}\nx = (1\n{% endif %}"

    assert CodeBlockYAML(label="code_1", code=code).code == code


def test_runtime_classifier_reports_real_syntax_error_line_after_multiline_jinja_statement() -> None:
    code = "{% if\n enabled %}\nx = 1\ny = ("

    CodeBlockYAML(label="code_1", code=code)
    with pytest.raises(ValueError, match=r"CodeBlock code is invalid Python: .*line 4"):
        _direct_code_block_error_code_raises(code)


def test_code_block_jinja_expression_substitution_remains_supported() -> None:
    CodeBlockYAML(label="code_1", code="x = {{ value }}")


@pytest.mark.parametrize(
    "code",
    [
        "class ErrorCode(Exception):\n    pass\nraise ErrorCode('declared', 'reason')",
        "def ErrorCode(code, reasoning):\n    return Exception()\nraise ErrorCode('declared', 'reason')",
        "async def ErrorCode(code, reasoning):\n    return Exception()\nraise ErrorCode('declared', 'reason')",
        "ErrorCode = ValueError\nraise ErrorCode('declared', 'reason')",
    ],
)
def test_code_block_accepts_error_code_shadowing_for_runtime(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})


@pytest.mark.parametrize(
    "code",
    [
        "def f(ErrorCode):\n    raise ErrorCode('declared', 'reason')",
        "def f(*, ErrorCode):\n    raise ErrorCode('declared', 'reason')",
        "f = lambda ErrorCode: ErrorCode('declared', 'reason')",
    ],
)
def test_code_block_accepts_error_code_parameter_shadowing_for_runtime(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})


@pytest.mark.parametrize(
    "code",
    [
        "try:\n    pass\nexcept Exception as ErrorCode:\n    pass",
        "with context_manager() as ErrorCode:\n    pass",
        "match value:\n    case ErrorCode:\n        pass",
        "match value:\n    case [*ErrorCode]:\n        pass",
        'match value:\n    case {"key": item, **ErrorCode}:\n        pass',
        "match value:\n    case 1 as ErrorCode:\n        pass",
        "match value:\n    case some.ErrorCode as ErrorCode:\n        pass",
        "match value:\n    case [x] as ErrorCode:\n        pass",
    ],
)
def test_code_block_accepts_error_code_non_name_shadowing_for_runtime(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code)


@pytest.mark.parametrize(
    "handler",
    [
        "except ErrorCode:",
        "except (ValueError, ErrorCode):",
    ],
)
def test_code_block_allows_error_code_in_exception_type(handler: str) -> None:
    CodeBlockYAML(label="code_1", code=f"try:\n    pass\n{handler}\n    pass")


def test_code_block_allows_similarly_named_parameter_with_error_code_raise() -> None:
    CodeBlockYAML(
        label="code_1",
        code="def f(ErrorCodeFoo):\n    raise ErrorCode('declared', 'reason')",
        error_code_mapping={"declared": "Declared"},
    )


def test_code_block_allows_other_class_names_with_error_code_raise() -> None:
    CodeBlockYAML(
        label="code_1",
        code="class ErrorCodeFoo(Exception):\n    pass\nraise ErrorCode('declared', 'reason')",
        error_code_mapping={"declared": "Declared"},
    )


@pytest.mark.parametrize(
    "code",
    [
        "raise errors.ErrorCode('declared', 'reason')",
        "raise importlib.import_module('pkg').ErrorCode('declared', 'reason')",
        "raise __import__('pkg').ErrorCode('declared', 'reason')",
        "for item in items:\n    raise errors.ErrorCode('declared', 'reason')",
    ],
)
def test_code_block_accepts_attribute_error_code_raises_for_runtime(code: str) -> None:
    CodeBlockYAML(label="code_1", code=code, error_code_mapping={"declared": "Declared"})


def test_code_block_allows_other_attribute_exception_names() -> None:
    CodeBlockYAML(label="code_1", code="raise errors.ErrorCodeFoo('declared', 'reason')")


def test_workflow_effective_manifest_and_unused_declarations() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        error_code_mapping={"workflow_code": "Workflow declaration"},
        blocks=[
            CodeBlockYAML(label="one", code="raise ErrorCode('workflow_code', 'reason')"),
            CodeBlockYAML(label="two", code="x = 1", error_code_mapping={"unused": "Unused"}),
        ],
    )
    assert workflow.blocks[1].error_code_mapping == {"unused": "Unused"}


@pytest.mark.parametrize(
    "mapping",
    [
        {f"code_{index}": "description" for index in range(100)},
        {"legacy_code": ""},
        {f"code_{index}": "x" * 2000 for index in range(17)},
    ],
    ids=["entry-count", "empty-description", "aggregate-size"],
)
def test_workflow_accepts_legacy_error_code_mapping_shapes(mapping: dict[str, str]) -> None:
    workflow = WorkflowDefinitionYAML(parameters=[], blocks=[], error_code_mapping=mapping)

    assert workflow.error_code_mapping == mapping


def test_code_block_own_mapping_remains_strict() -> None:
    mapping = {f"code_{index}": "description" for index in range(65)}

    with pytest.raises(ValidationError, match="at most 64 entries"):
        CodeBlockYAML(label="code", code="x = 1", error_code_mapping=mapping)
    with pytest.raises(ValidationError, match="trimmed, non-empty"):
        CodeBlockYAML(label="code", code="x = 1", error_code_mapping={"legacy_code": ""})
    with pytest.raises(ValidationError, match="32768 UTF-8 bytes"):
        CodeBlockYAML(
            label="code",
            code="x = 1",
            error_code_mapping={f"code_{index}": "x" * 2000 for index in range(17)},
        )


@pytest.mark.asyncio
async def test_inherited_control_character_entry_saves_but_cannot_authorize_typed_error() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        error_code_mapping={"legacy_code": "unsafe\u0000description"},
        blocks=[CodeBlockYAML(label="code", code="raise ErrorCode('legacy_code', 'reason')")],
    )
    block = block_yaml_to_block(workflow.blocks[0], {"code_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    context = _make_workflow_run_context()
    context.workflow = SimpleNamespace(workflow_definition=workflow)

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block.error_code_mapping is None
    assert block._extract_declared_error(exc_info.value, context) is None


def test_workflow_accepts_undeclared_direct_raise() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        blocks=[CodeBlockYAML(label="one", code="raise ErrorCode('missing', 'reason')")],
    )

    assert workflow.blocks[0].code == "raise ErrorCode('missing', 'reason')"


@pytest.mark.asyncio
async def test_dynamic_error_code_raise_is_an_ordinary_runtime_failure() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        blocks=[
            CodeBlockYAML(
                label="one",
                code="code = 'dynamic'\nraise ErrorCode(code, 'runtime evidence')",
                error_code_mapping={"dynamic": "Dynamic failure"},
            )
        ],
    )
    block = block_yaml_to_block(workflow.blocks[0], {"one_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    context = _make_workflow_run_context()
    context.workflow = SimpleNamespace(workflow_definition=workflow)

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(SyntaxError):
        await function()


def test_workflow_manifest_validation_descends_through_nested_loops() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        error_code_mapping={"workflow_code": "Workflow declaration"},
        blocks=[
            {
                "block_type": "for_loop",
                "label": "outer",
                "loop_blocks": [
                    {
                        "block_type": "while_loop",
                        "label": "inner",
                        "condition": {"expression": "{{ true }}"},
                        "loop_blocks": [
                            {
                                "block_type": "code",
                                "label": "nested_code",
                                "code": "raise ErrorCode('workflow_code', 'reason')",
                            },
                            {
                                "block_type": "code",
                                "label": "override_code",
                                "code": "raise ErrorCode('override', 'reason')",
                                "error_code_mapping": {"override": "Block override"},
                            },
                        ],
                    }
                ],
            }
        ],
    )
    assert workflow.blocks[0].label == "outer"


def test_workflow_accepts_undeclared_raise_in_nested_loop() -> None:
    workflow = WorkflowDefinitionYAML(
        parameters=[],
        blocks=[
            {
                "block_type": "for_loop",
                "label": "outer",
                "loop_blocks": [
                    {
                        "block_type": "code",
                        "label": "nested_code",
                        "code": "raise ErrorCode('missing', 'reason')",
                    }
                ],
            }
        ],
    )

    assert workflow.blocks[0].label == "outer"


def test_error_code_manifest_entry_cap() -> None:
    mapping = {f"code_{index}": "description" for index in range(64)}
    CodeBlockYAML(label="code", code="x = 1", error_code_mapping=mapping)
    with pytest.raises(ValidationError, match="at most 64 entries"):
        CodeBlockYAML(label="code", code="x = 1", error_code_mapping={**mapping, "overflow": "description"})


def test_error_code_manifest_aggregate_size_cap() -> None:
    mapping = {f"code_{index}": "x" * 2000 for index in range(17)}
    with pytest.raises(ValidationError, match="32768 UTF-8 bytes"):
        CodeBlockYAML(label="code", code="x = 1", error_code_mapping=mapping)


@pytest.mark.parametrize(
    "mapping",
    [
        {"bad\nkey": "description"},
        {"valid": "bad\u202evalue"},
    ],
)
def test_error_code_manifest_rejects_unicode_category_c(mapping: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="Unicode category-C"):
        CodeBlockYAML(label="code", code="x = 1", error_code_mapping=mapping)


def test_error_code_manifest_accepts_normal_punctuation_and_backticks() -> None:
    CodeBlockYAML(
        label="code",
        code="x = 1",
        error_code_mapping={"human-readable.v1!": "Use `quoted` values; punctuation is okay."},
    )


def test_code_block_model_roundtrip() -> None:
    block = CodeBlock(
        label="code_1",
        output_parameter=_output_parameter(),
        code="x = 1",
        prompt="g",
        steps=[CodeBlockStep(description="d", action_type=ActionType.CLICK)],
    )
    dumped = block.model_dump()
    assert dumped["prompt"] == "g"
    assert dumped["steps"][0]["action_type"] == "click"
    assert dumped["error_code_mapping"] is None


def _make_workflow_run_context(values: dict | None = None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_run_id="wr_1",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


def test_format_potential_template_parameters_renders_prompt() -> None:
    # Regression: prompt must be jinja-rendered before it reaches the task v1
    # (mirrors the task block), not passed through raw.
    block = CodeBlock(
        label="code_1",
        output_parameter=_output_parameter(),
        code="x = {{ count }}",
        prompt="Open {{ url }}",
    )
    ctx = _make_workflow_run_context({"count": "1", "url": "https://example.com"})

    block.format_potential_template_parameters(ctx)

    assert block.code == "x = 1"
    assert block.prompt == "Open https://example.com"


def test_save_path_rebuilds_steps_from_code_and_discards_a_handed_in_list() -> None:
    code = "await page.goto('https://example.com/')\nprice = await page.locator('.price').inner_text()\n"
    block_yaml = CodeBlockYAML(
        label="code_1",
        code=code,
        steps=[
            {"description": "Click submit", "action_type": "click", "line_start": 1, "line_end": 2},
            {"description": "Stale step", "action_type": "extract", "line_start": 2, "line_end": 2},
        ],
    )
    block = block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    assert block.steps is not None
    assert [step.model_dump(mode="json") for step in block.steps] == derive_code_block_steps(code)
    assert [step.description for step in block.steps] == ["Open https://example.com/", "Extract price"]


@pytest.mark.parametrize("steps", [[{"line_start": "unknown"}], ["not a step"], "not a list"])
def test_save_path_accepts_code_carrying_malformed_steps(steps: object) -> None:
    code = "await page.goto('https://example.com/')\n"
    block_yaml = CodeBlockYAML.model_validate({"label": "code_1", "code": code, "steps": steps})
    block = block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    assert [step.description for step in block.steps or []] == ["Open https://example.com/"]
