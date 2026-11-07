import json
import time
from datetime import UTC, datetime

import pytest

import skyvern.forge.sdk.workflow.validation as workflow_validation
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock, ForLoopBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.validation import (
    MAX_WORKFLOW_VALIDATION_LOOP_DEPTH,
    WorkflowValidationTooComplex,
    validate_stored_workflow,
    validate_workflow_definition,
)


def _definition(blocks: list[dict], parameters: list[dict] | None = None) -> dict:
    return {
        "title": "Validation fixture",
        "workflow_definition": {"parameters": parameters or [], "blocks": blocks},
    }


def test_reports_unknown_and_forward_references_in_nested_values() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {"block_type": "navigation", "label": "first", "navigation_goal": "{{ later_output }}"},
                {"block_type": "navigation", "label": "later", "navigation_goal": "{{ missing_value }}"},
            ]
        )
    )

    assert {problem.code for problem in result.problems} == {"forward_reference", "unknown_reference"}
    assert {problem.block_label for problem in result.problems} == {"first", "later"}


def test_reports_credential_references_in_notes() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "notes": "Use {{ login }}",
                }
            ],
            [{"parameter_type": "credential", "key": "login", "credential_id": "cred_test"}],
        )
    )

    assert [(problem.code, problem.field, problem.severity) for problem in result.problems] == [
        ("credential_in_note", "notes", "warning")
    ]
    assert result.valid


def test_reports_a_credential_used_only_in_a_statement_in_a_note() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "notes": "{% if login %}sign in{% endif %}",
                }
            ],
            [{"parameter_type": "credential", "key": "login", "credential_id": "cred_test"}],
        )
    )

    assert [problem.code for problem in result.problems] == ["credential_in_note"]


def test_branch_conditions_that_are_not_a_list_are_a_problem_not_an_error() -> None:
    result = validate_workflow_definition(
        _definition([{"block_type": "conditional", "label": "check", "branch_conditions": None}], [])
    )

    assert not result.valid


def test_reports_bad_jump_and_empty_branch_condition() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "conditional",
                    "label": "choose",
                    "branch_conditions": [
                        {"criteria": {"expression": "   "}, "next_block_label": "missing"},
                    ],
                },
            ]
        )
    )

    assert {problem.code for problem in result.problems} >= {"empty_condition", "bad_jump_target"}


def test_reports_an_unknown_output_reference() -> None:
    result = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "first", "navigation_goal": "Use {{ missing_output }}"}])
    )

    assert [(problem.code, problem.field) for problem in result.problems] == [("unknown_reference", "navigation_goal")]


def test_ignores_jinja_like_text_in_non_templated_action_selector() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "action",
                    "label": "click",
                    "selector": '[data-template="{{item}}"]',
                }
            ]
        )
    )

    assert result.valid, result.problems


def test_ignores_jinja_in_python_code_comments() -> None:
    result = validate_workflow_definition(
        _definition([{"block_type": "code", "label": "code", "code": "# {{ }}\n# fill {{ later }}"}])
    )

    assert result.valid, result.problems


def test_validates_workflow_system_prompt_references() -> None:
    result = validate_workflow_definition(
        {
            "title": "Workflow prompt",
            "workflow_definition": {
                "workflow_system_prompt": "Use {{ missing_value }}",
                "parameters": [],
                "blocks": [],
            },
        }
    )

    assert [(problem.code, problem.path) for problem in result.problems] == [
        ("unknown_reference", "workflow_definition.workflow_system_prompt")
    ]


@pytest.mark.parametrize(
    ("block", "field"),
    [
        (
            {"block_type": "navigation", "label": "step", "navigation_goal": "Go", "parameter_keys": ["missing"]},
            "parameter_keys",
        ),
        (
            {"block_type": "for_loop", "label": "each", "loop_over_parameter_key": "missing", "loop_blocks": []},
            "loop_over_parameter_key",
        ),
    ],
)
def test_reports_undefined_structural_parameter_references(block: dict, field: str) -> None:
    result = validate_workflow_definition(_definition([block]))

    assert [(problem.code, problem.field) for problem in result.problems] == [("unknown_reference", field)]


def test_structural_output_reference_must_be_declared_as_a_parameter() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "source",
                    "navigation_goal": "Go",
                    "parameter_keys": ["source_output"],
                }
            ]
        )
    )

    assert [(problem.code, problem.field) for problem in result.problems] == [("unknown_reference", "parameter_keys")]


def test_structural_references_can_use_outputs_from_earlier_blocks() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {"block_type": "navigation", "label": "source", "navigation_goal": "Go"},
                {
                    "block_type": "navigation",
                    "label": "consumer",
                    "navigation_goal": "Use source output",
                    "parameter_keys": ["source_output"],
                },
                {
                    "block_type": "for_loop",
                    "label": "each_source_result",
                    "loop_over_parameter_key": "source_output",
                    "loop_blocks": [],
                },
            ]
        )
    )

    assert result.valid, result.problems


@pytest.mark.parametrize("name", ["env", "environment", "llm", "params"])
def test_branch_criteria_aliases_are_not_available_in_ordinary_fields(name: str) -> None:
    ordinary_field = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "step", "navigation_goal": f"Use {{{{ {name}.foo }}}}"}])
    )
    conditional_criteria = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "conditional",
                    "label": "choose",
                    "branch_conditions": [
                        {"criteria": {"expression": f"{{{{ {name}.foo }}}}"}, "next_block_label": "step"}
                    ],
                },
                {"block_type": "navigation", "label": "step", "navigation_goal": "Go"},
            ]
        )
    )

    assert [(problem.code, problem.message) for problem in ordinary_field.problems] == [
        ("unknown_reference", f"Unknown reference: {name}.")
    ]
    assert conditional_criteria.valid, conditional_criteria.problems


def test_macro_names_are_checked_in_the_scope_where_the_macro_is_called() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "step",
                    "navigation_goal": "{% macro show() %}{{ value }}{% endmacro %}{% set value = 'ok' %}{{ show() }}",
                }
            ]
        )
    )

    assert result.valid, result.problems


def test_called_macro_still_reports_an_unknown_name() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "step",
                    "navigation_goal": "{% macro show() %}{{ missing }}{% endmacro %}{{ show() }}",
                }
            ]
        )
    )

    assert [(problem.code, problem.message) for problem in result.problems] == [
        ("unknown_reference", "Unknown reference: missing.")
    ]


def test_macro_expansion_has_a_validation_work_budget() -> None:
    macros = ["{% macro m0() %}value{% endmacro %}"]
    for index in range(1, 25):
        previous = index - 1
        macros.append(f"{{% macro m{index}() %}}{{{{ m{previous}() }}}}{{{{ m{previous}() }}}}{{% endmacro %}}")
    goal = "".join(macros) + "{{ m24() }}"

    result = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "step", "navigation_goal": goal}])
    )

    assert not result.valid
    assert [problem.code for problem in result.problems] == ["validation_limit"]


def test_macro_formal_argument_scans_use_the_validation_work_budget() -> None:
    arguments = ",".join(f"arg{index}" for index in range(1_000))
    macros = [f"{{% macro m0({arguments}) %}}value{{% endmacro %}}"]
    for index in range(1, 5):
        previous = index - 1
        macros.append(f"{{% macro m{index}() %}}{{{{ m{previous}() }}}}{{{{ m{previous}() }}}}{{% endmacro %}}")
    goal = "".join(macros) + "{{ m4() }}"

    result = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "step", "navigation_goal": goal}])
    )

    assert not result.valid
    assert [problem.code for problem in result.problems] == ["validation_limit"]


def test_empty_optional_task_goals_are_treated_as_absent() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "task",
                    "label": "step",
                    "navigation_goal": "",
                    "data_extraction_goal": "",
                }
            ]
        )
    )

    assert result.valid, result.problems


def _loop_over(source: str, child_goal: str) -> dict:
    return _definition(
        [
            {
                "block_type": "for_loop",
                "label": "each_item",
                "loop_variable_reference": source,
                "loop_blocks": [{"block_type": "navigation", "label": "inside", "navigation_goal": child_goal}],
            }
        ],
        [{"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "items"}],
    )


@pytest.mark.parametrize("name", ["current_item", "current_value", "current_index"])
def test_loop_context_variables_are_unknown_outside_a_loop(name: str) -> None:
    result = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "step", "navigation_goal": f"Use {{{{ {name} }}}}"}])
    )

    assert [(problem.code, problem.block_label) for problem in result.problems] == [("unknown_reference", "step")]


@pytest.mark.parametrize("source", ["items", "{{ items }}"])
def test_a_loop_over_a_declared_input_is_clean(source: str) -> None:
    result = validate_workflow_definition(
        _loop_over(source, "Use {{ current_item }}, {{ current_value }} at {{ current_index }}")
    )

    assert result.valid
    assert result.problems == []


def test_while_loop_context_variables_are_scoped_to_its_condition_and_body() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "while_loop",
                    "label": "repeat",
                    "condition": {"expression": "{{ current_index < 2 }}"},
                    "loop_blocks": [
                        {
                            "block_type": "navigation",
                            "label": "inside",
                            "navigation_goal": "Use {{ current_item }}, {{ current_value }}, {{ current_index }}",
                        }
                    ],
                }
            ]
        )
    )

    assert result.valid, result.problems


def test_a_loop_source_is_not_a_variable_its_children_can_read() -> None:
    result = validate_workflow_definition(_loop_over("ghost", "Use {{ ghost }}"))

    assert not result.valid
    assert {(problem.code, problem.block_label, problem.severity) for problem in result.problems} == {
        ("unknown_loop_source", "each_item", "warning"),
        ("unknown_reference", "inside", "error"),
    }


def test_a_templated_loop_source_must_resolve() -> None:
    result = validate_workflow_definition(_loop_over("{{ ghost }}", "Use {{ current_item }}"))

    assert [(problem.code, problem.block_label) for problem in result.problems] == [("unknown_reference", "each_item")]


def test_allows_earlier_nested_loop_output_in_a_later_sibling() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "for_loop",
                    "label": "each_item",
                    "loop_over_parameter_key": "items",
                    "loop_blocks": [
                        {"block_type": "navigation", "label": "a", "navigation_goal": "Open the page"},
                        {
                            "block_type": "navigation",
                            "label": "b",
                            "navigation_goal": "Use {{ a_output }}",
                        },
                    ],
                }
            ],
            [{"parameter_type": "workflow", "key": "items", "default_value": []}],
        )
    )

    assert not any(problem.code == "forward_reference" for problem in result.problems)


def test_reports_nested_empty_goal_and_bad_jump() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "for_loop",
                    "label": "each_item",
                    "loop_over_parameter_key": "items",
                    "loop_blocks": [
                        {
                            "block_type": "navigation",
                            "label": "inside",
                            "navigation_goal": "",
                            "next_block_label": "missing",
                        }
                    ],
                }
            ],
            [{"parameter_type": "workflow", "key": "items", "default_value": []}],
        )
    )

    assert {problem.code for problem in result.problems} >= {"empty_goal", "bad_jump_target"}


def test_ignores_jinja_syntax_in_a_description() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "description": "For example, {{ x }} is a placeholder.",
                }
            ]
        )
    )

    assert not any(problem.code == "unknown_reference" for problem in result.problems)


def test_ignores_malformed_jinja_in_notes() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "notes": "Keep {{ unfinished",
                }
            ]
        )
    )

    assert not any(problem.code == "template_syntax" for problem in result.problems)


def test_reports_workflow_credential_id_references_in_notes() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "notes": "Use {{ login }}",
                }
            ],
            [
                {
                    "parameter_type": "workflow",
                    "workflow_parameter_type": "credential_id",
                    "key": "login",
                }
            ],
        )
    )

    assert [(problem.code, problem.severity) for problem in result.problems] == [("credential_in_note", "warning")]


def _stored_workflow(first_goal: str, second_goal: str) -> Workflow:
    now = datetime.now(UTC)
    invoice_date = WorkflowParameter(
        workflow_parameter_id="wp_invoice_date",
        workflow_id="w_saved",
        key="invoice_date",
        workflow_parameter_type=WorkflowParameterType.STRING,
        created_at=now,
        modified_at=now,
    )
    outputs = [
        OutputParameter(
            output_parameter_id=f"op_{label}",
            workflow_id="w_saved",
            key=f"{label}_output",
            created_at=now,
            modified_at=now,
        )
        for label in ("first", "second")
    ]
    blocks = [
        FileDownloadBlock(label=label, output_parameter=output, navigation_goal=goal, parameters=[invoice_date])
        for label, output, goal in zip(("first", "second"), outputs, (first_goal, second_goal))
    ]
    return Workflow(
        workflow_id="w_saved",
        organization_id="o_test",
        title="Saved workflow",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[invoice_date, *outputs], blocks=blocks),
        created_at=now,
        modified_at=now,
    )


def test_validates_a_stored_workflow_with_input_and_output_parameters() -> None:
    clean = validate_stored_workflow(
        _stored_workflow("Download the invoice for {{ invoice_date }}", "Check {{ first_output }}")
    )
    forward = validate_stored_workflow(_stored_workflow("Check {{ second_output }}", "Download {{ invoice_date }}"))

    assert clean.valid, clean.problems
    assert [(problem.code, problem.block_label) for problem in forward.problems] == [("forward_reference", "first")]


_LOOP_OF_LOOPS = {
    "block_type": "for_loop",
    "label": "each_customer",
    "loop_variable_reference": "customers",
    "loop_blocks": [
        {"block_type": "navigation", "label": "open_customer", "navigation_goal": "Open {{ current_item }}"},
        {
            "block_type": "for_loop",
            "label": "each_order",
            "loop_variable_reference": "{{ open_customer_output.extracted_information }}",
            "loop_blocks": [
                {
                    "block_type": "navigation",
                    "label": "open_order",
                    "navigation_goal": "Open {{ current_value }} ({{ current_index }}) for {{ orders }}",
                }
            ],
        },
    ],
}


@pytest.mark.parametrize(
    ("blocks", "parameters"),
    [
        pytest.param(
            [_LOOP_OF_LOOPS],
            [
                {"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "customers"},
                {"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "orders"},
            ],
            id="nested-loops",
        ),
        pytest.param(
            [{"block_type": "navigation", "label": "lookup", "navigation_goal": "Use {{ customer_output }}"}],
            [{"parameter_type": "workflow", "workflow_parameter_type": "string", "key": "customer_output"}],
            id="declared-input-ending-in-output",
        ),
        pytest.param(
            [
                {
                    "block_type": "navigation",
                    "label": "stamp",
                    "navigation_goal": (
                        "Record {{ workflow_run_id }} of {{ workflow_id }} on {{ current_date }}"
                        "{% for i in range(2) %} {{ i }}{% endfor %}"
                    ),
                }
            ],
            [],
            id="runtime-variables",
        ),
        pytest.param(
            [
                {
                    "block_type": "navigation",
                    "label": "first",
                    "navigation_goal": "Open the page",
                    "notes": "Mention {{ anything }} and {{ later_output }} as written",
                    "trailing_notes": "Unfinished {{ template",
                }
            ],
            [],
            id="notes",
        ),
        pytest.param(
            [
                {
                    "block_type": "for_loop",
                    "label": "each_item",
                    "loop_over_parameter_key": "items",
                    "loop_blocks": [
                        {"block_type": "navigation", "label": "inner", "navigation_goal": "Use {{ current_item }}"}
                    ],
                },
                {"block_type": "navigation", "label": "after", "navigation_goal": "Use {{ inner_output }}"},
            ],
            [{"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "items"}],
            id="loop-child-output-after-loop",
        ),
        pytest.param(
            [
                {"block_type": "extraction", "label": "lookup", "data_extraction_goal": "Read the total"},
                {
                    "block_type": "for_loop",
                    "label": "outer",
                    "loop_over_parameter_key": "items",
                    "loop_blocks": [
                        {
                            "block_type": "navigation",
                            "label": "inner",
                            "navigation_goal": "Use {{ outer.current_value }} and {{ lookup.extracted_information }}",
                        }
                    ],
                },
                {
                    "block_type": "navigation",
                    "label": "after",
                    "navigation_goal": "Use {{ inner.extracted_information }}",
                },
            ],
            [{"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "items"}],
            id="bare-label-aliases",
        ),
    ],
)
def test_valid_workflows_report_no_problems(blocks: list[dict], parameters: list[dict]) -> None:
    result = validate_workflow_definition(_definition(blocks, parameters))

    assert result.valid
    assert result.problems == []


def test_a_long_expression_is_checked_quickly_and_still_reported() -> None:
    goal = "{{ " + " + ".join(f"v{index}" for index in range(300)) + " }}"
    blocks = [
        {"block_type": "navigation", "label": f"b{index}", "url": "https://x", "navigation_goal": goal}
        for index in range(4)
    ]

    started = time.monotonic()
    result = validate_workflow_definition({"title": "t", "workflow_definition": {"parameters": [], "blocks": blocks}})

    assert time.monotonic() - started < 2
    assert len(result.problems) == 100
    assert result.problems[-1].code == "too_many_problems"
    assert not result.valid


@pytest.mark.parametrize("field", ["browser_profile_key", "sequential_key"])
@pytest.mark.parametrize("input_name", ["account_id", "acount_id"])
def test_browser_identity_templates_use_the_authored_parameter_scope(field: str, input_name: str) -> None:
    draft = _definition(
        [{"block_type": "code", "label": "first", "code": "result = 1"}],
        [{"parameter_type": "workflow", "workflow_parameter_type": "string", "key": "account_id"}],
    )
    draft[field] = "acct-{{ " + input_name + " }}"

    result = validate_workflow_definition(draft)

    assert result.valid is (input_name == "account_id")
    if input_name != "account_id":
        assert [(problem.code, problem.field, problem.path) for problem in result.problems] == [
            ("unknown_reference", field, field)
        ]


def test_six_level_malformed_draft_uses_bounded_discriminated_parsing() -> None:
    blocks = [
        {
            "block_type": "code",
            "label": f"leaf_{index}",
            "continue_on_failure": [],
            "model": [],
            "ignore_workflow_system_prompt": [],
            "notes": [],
            "trailing_notes": [],
            "description": [],
            "code": [],
        }
        for index in range(20)
    ]
    for index in range(6):
        blocks = [{"block_type": "for_loop", "label": f"loop_{index}", "loop_blocks": blocks}]
    draft = _definition(blocks)
    assert 4_000 <= len(json.dumps(draft).encode()) <= 6_000

    started = time.monotonic()
    result = validate_workflow_definition(draft)

    assert time.monotonic() - started < 2
    assert not result.valid
    assert len(result.problems) == 100
    assert any(problem.code == "schema_error" for problem in result.problems)


def test_validation_limits_any_nested_block_list_before_schema_validation() -> None:
    blocks: list[dict] = [{"block_type": "navigation", "label": "leaf"}]
    for index in range(MAX_WORKFLOW_VALIDATION_LOOP_DEPTH + 1):
        blocks = [{"block_type": "navigation", "label": f"parent_{index}", "loop_blocks": blocks}]

    with pytest.raises(WorkflowValidationTooComplex, match="nested block lists"):
        validate_workflow_definition(_definition(blocks))


def test_stored_workflow_limits_run_before_yaml_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    blocks = [FileDownloadBlock.model_construct(label="leaf")]
    for index in range(MAX_WORKFLOW_VALIDATION_LOOP_DEPTH + 1):
        blocks = [ForLoopBlock.model_construct(label=f"loop_{index}", loop_blocks=blocks)]
    workflow = Workflow.model_construct(workflow_definition=WorkflowDefinition.model_construct(blocks=blocks))
    monkeypatch.setattr(
        workflow_validation,
        "workflow_to_copilot_yaml",
        lambda _: pytest.fail("over-nested stored workflow reached YAML conversion"),
    )

    with pytest.raises(WorkflowValidationTooComplex, match="nested block lists"):
        validate_stored_workflow(workflow)


def test_names_a_template_assigns_are_not_unknown() -> None:
    goal = "{% set total = 1 %}{% for row in rows %}{{ row }} {{ loop.index }} {{ total }}{% endfor %}"
    blocks = [{"block_type": "navigation", "label": "b", "url": "https://x", "navigation_goal": goal}]

    result = validate_workflow_definition({"title": "t", "workflow_definition": {"parameters": [], "blocks": blocks}})

    assert [problem.message for problem in result.problems] == ["Unknown reference: rows."]


def test_conditional_template_assignment_is_not_visible_after_implicit_no_match() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "step",
                    "navigation_goal": "{% if flag %}{% set token = 1 %}{% endif %}{{ token }}",
                }
            ],
            [{"parameter_type": "workflow", "workflow_parameter_type": "boolean", "key": "flag"}],
        )
    )

    assert [(problem.code, problem.message) for problem in result.problems] == [
        ("unknown_reference", "Unknown reference: token.")
    ]


def test_conditional_template_assignment_is_not_visible_in_an_alternative_branch() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "navigation",
                    "label": "step",
                    "navigation_goal": "{% if flag %}{% set token = 1 %}{% else %}{{ token }}{% endif %}",
                }
            ],
            [{"parameter_type": "workflow", "workflow_parameter_type": "boolean", "key": "flag"}],
        )
    )

    assert [(problem.code, problem.message) for problem in result.problems] == [
        ("unknown_reference", "Unknown reference: token.")
    ]


def test_an_expression_too_deep_to_parse_is_a_problem_not_an_error() -> None:
    goal = "{{ " + "(" * 5000 + "x" + ")" * 5000 + " }}"
    blocks = [{"block_type": "navigation", "label": "b", "url": "https://x", "navigation_goal": goal}]

    result = validate_workflow_definition({"title": "t", "workflow_definition": {"parameters": [], "blocks": blocks}})

    assert [problem.code for problem in result.problems] == ["template_too_complex"]


def test_a_name_used_before_its_loop_target_is_unknown_and_a_loop_cannot_read_its_own_output() -> None:
    result = validate_workflow_definition(
        _definition(
            [
                {
                    "block_type": "for_loop",
                    "label": "each",
                    "loop_variable_reference": "{{ rows }}",
                    "loop_blocks": [
                        {"block_type": "navigation", "label": "inner", "navigation_goal": "{{ each_output }}"},
                    ],
                },
                {
                    "block_type": "navigation",
                    "label": "after",
                    "navigation_goal": "{{ row }}{% for row in each_output %}{{ row }}{% endfor %}",
                },
            ],
            [{"parameter_type": "workflow", "key": "rows", "workflow_parameter_type": "json"}],
        )
    )

    assert {(problem.block_label, problem.code) for problem in result.problems} == {
        ("inner", "forward_reference"),
        ("after", "unknown_reference"),
    }


def test_a_schema_error_inside_a_block_names_the_block() -> None:
    result = validate_workflow_definition(
        _definition([{"block_type": "navigation", "label": "first", "navigation_goal": "Go", "max_retries": "x"}])
    )

    assert [problem.block_label for problem in result.problems if problem.code == "schema_error"] == ["first"]


@pytest.mark.parametrize(
    "field_value",
    [
        {"run_with": []},
        {"browser_profile_key": 7},
        {"browser_profile_key": "{{ user"},
    ],
)
def test_unexpected_schema_validator_errors_become_schema_problems(field_value: dict) -> None:
    raw = _definition([])
    raw.update(field_value)

    result = validate_workflow_definition(raw)

    assert not result.valid
    assert any(problem.code == "schema_error" for problem in result.problems)


@pytest.mark.parametrize("block_type", [[], {}, ["code"]])
def test_an_unhashable_block_type_is_a_schema_problem_and_notes_are_still_checked(block_type: object) -> None:
    result = validate_workflow_definition(
        _definition(
            [{"block_type": block_type, "label": "first", "notes": "Sign in with {{ login }}"}],
            [{"parameter_type": "credential", "key": "login", "credential_id": "cred_test"}],
        )
    )

    codes = {problem.code for problem in result.problems}
    assert "schema_error" in codes
    assert "credential_in_note" in codes


def test_a_schema_error_inside_a_loop_names_the_innermost_step() -> None:
    blocks = [{"block_type": "navigation", "label": f"step_{i}", "navigation_goal": "Go"} for i in range(11)]
    blocks[10] = {
        "block_type": "for_loop",
        "label": "each",
        "loop_over_parameter_key": "items",
        "loop_blocks": [{"block_type": "navigation", "label": "inner", "navigation_goal": "Go", "max_retries": "x"}],
    }
    result = validate_workflow_definition(
        _definition(blocks, [{"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "items"}])
    )

    assert {problem.block_label for problem in result.problems if problem.code == "schema_error"} == {"inner"}


def _branching(after_goal: str) -> dict:
    return _definition(
        [
            {
                "block_type": "conditional",
                "label": "check",
                "next_block_label": "after",
                "branch_conditions": [
                    {"criteria": {"expression": "{{ flag }}"}, "next_block_label": "left", "is_default": False},
                    {"next_block_label": "flag", "is_default": True},
                ],
            },
            {"block_type": "navigation", "label": "left", "navigation_goal": "Go", "next_block_label": "shared"},
            {"block_type": "navigation", "label": "flag", "navigation_goal": "Go", "next_block_label": "shared"},
            {"block_type": "navigation", "label": "shared", "navigation_goal": "Use {{ left_output }}"},
            {"block_type": "navigation", "label": "after", "navigation_goal": after_goal},
        ],
        [{"parameter_type": "workflow", "workflow_parameter_type": "boolean", "key": "flag"}],
    )


def test_a_loop_child_after_the_branches_is_warned_like_the_loop_itself() -> None:
    raw = _branching("Go")
    raw["workflow_definition"]["blocks"][3] = {
        "block_type": "for_loop",
        "label": "shared",
        "loop_over_parameter_key": "flag",
        "loop_blocks": [{"block_type": "navigation", "label": "inner", "navigation_goal": "Use {{ left_output }}"}],
    }

    result = validate_workflow_definition(raw)

    assert ("inner", "branch_only_reference") in {(problem.block_label, problem.code) for problem in result.problems}


def test_a_step_after_the_branches_is_warned_about_an_output_only_one_branch_produces() -> None:
    result = validate_workflow_definition(_branching("Use {{ left_output }}, {{ shared_output }} and {{ flag }}"))

    assert result.valid
    # An input named like a branch step is replaced by that step's output when the branch runs, so it varies too.
    assert sorted(
        (problem.block_label, problem.message.split()[0])
        for problem in result.problems
        if problem.severity == "warning"
    ) == [("after", "flag"), ("after", "left_output"), ("shared", "left_output")]
    assert {problem.code for problem in result.problems} == {"branch_only_reference"}


def test_a_loop_child_output_from_one_branch_is_warned_about_after_the_merge() -> None:
    definition = _branching("Use {{ row_output }}")
    blocks = definition["workflow_definition"]["blocks"]
    blocks[1] = {
        "block_type": "for_loop",
        "label": "left",
        "loop_variable_reference": "{{ rows }}",
        "next_block_label": "shared",
        "loop_blocks": [{"block_type": "navigation", "label": "row", "navigation_goal": "Go"}],
    }
    blocks[3]["navigation_goal"] = "Go"
    definition["workflow_definition"]["parameters"].append(
        {"parameter_type": "workflow", "workflow_parameter_type": "json", "key": "rows"}
    )

    result = validate_workflow_definition(definition)

    assert [(problem.block_label, problem.code) for problem in result.problems] == [("after", "branch_only_reference")]


def test_outputs_is_only_a_name_inside_branch_criteria() -> None:
    raw = _definition(
        [
            {
                "block_type": "conditional",
                "label": "check",
                "branch_conditions": [
                    {"criteria": {"expression": "{{ outputs.first }}"}, "next_block_label": None, "is_default": False},
                    {"next_block_label": None, "is_default": True},
                ],
            },
            {"block_type": "navigation", "label": "go", "navigation_goal": "Use {{ outputs.first }}"},
        ]
    )

    result = validate_workflow_definition(raw)

    assert [(problem.block_label, problem.code) for problem in result.problems] == [("go", "unknown_reference")]


def test_a_while_loop_condition_is_checked_like_a_branch_criterion() -> None:
    raw = _definition(
        [
            {
                "block_type": "while_loop",
                "label": "repeat",
                "condition": {"expression": "{{ misspelled }} and {{ params }} and {{ current_index }}"},
                "loop_blocks": [{"block_type": "navigation", "label": "inner", "navigation_goal": "Go"}],
            }
        ]
    )

    result = validate_workflow_definition(raw)

    assert [(problem.field, problem.message) for problem in result.problems] == [
        ("expression", "Unknown reference: misspelled.")
    ]


def test_a_repeated_input_key_is_an_error_like_on_save() -> None:
    parameter = {"parameter_type": "workflow", "workflow_parameter_type": "string", "key": "name"}

    result = validate_workflow_definition(_definition([], [parameter, dict(parameter)]))

    assert not result.valid
    assert [(problem.path, problem.code) for problem in result.problems] == [
        ("workflow_definition.parameters[1].key", "duplicate_parameter_key")
    ]


def test_a_while_condition_may_read_its_own_body_from_the_previous_iteration() -> None:
    raw = _definition(
        [
            {
                "block_type": "while_loop",
                "label": "pages",
                "condition": {"expression": "{{ current_index == 0 or extract_page_output.has_next_page }}"},
                "loop_blocks": [{"block_type": "extraction", "label": "extract_page", "data_extraction_goal": "Read"}],
            },
            {
                "block_type": "http_request",
                "label": "send",
                "url": "https://example.com",
                "body": {"condition": {"expression": "{{ params.x }}"}},
            },
        ]
    )

    result = validate_workflow_definition(raw)

    assert [(problem.block_label, problem.message) for problem in result.problems] == [
        ("send", "Unknown reference: params.")
    ]
