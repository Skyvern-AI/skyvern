"""Tests for workflow parameter key and block label validation.

These tests ensure that parameter keys and block labels are valid Python/Jinja2 identifiers,
preventing runtime errors like "'State_' is undefined" when using keys like "State_/_Province".
"""

import time
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.schemas.workflows import (
    TaskBlockYAML,
    WorkflowParameterYAML,
    sanitize_block_label,
    sanitize_parameter_key,
    sanitize_workflow_yaml_with_references,
)
from skyvern.utils.templating import replace_jinja_reference


class TestIdentifierValidation:
    """Tests for parameter key and block label validation."""

    @pytest.mark.parametrize(
        ("model_name", "value"),
        [
            pytest.param("parameter", "my_parameter", id="parameter-key"),
            pytest.param("parameter", "param123", id="parameter-key-with-numbers"),
            pytest.param("parameter", "_private_param", id="parameter-key-underscore-prefix"),
            pytest.param("parameter", "x", id="parameter-key-single-letter"),
            pytest.param("block", "my_task", id="block-label"),
            pytest.param("block", "task123", id="block-label-with-numbers"),
            pytest.param("block", "_private_task", id="block-label-underscore-prefix"),
        ],
    )
    def test_valid_keys(self, model_name: str, value: str) -> None:
        if model_name == "parameter":
            param = WorkflowParameterYAML(
                key=value,
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
            assert param.key == value
        else:
            block = TaskBlockYAML(label=value, url="https://example.com")
            assert block.label == value

    @pytest.mark.parametrize(
        ("model_name", "value", "error_fragment"),
        [
            pytest.param("parameter", "State_/_Province", "not a valid parameter name", id="parameter-invalid-char"),
            pytest.param("parameter", "state-or-province", "not a valid parameter name", id="parameter-hyphen"),
            pytest.param("parameter", "some.property", "not a valid parameter name", id="parameter-dot"),
            pytest.param("parameter", "123param", "not a valid parameter name", id="parameter-digit-prefix"),
            pytest.param("parameter", "my parameter", "whitespace", id="parameter-whitespace"),
            pytest.param("parameter", "my\tparameter", "whitespace", id="parameter-tab"),
            pytest.param("parameter", "param*value", "not a valid parameter name", id="parameter-asterisk"),
            pytest.param("block", "task/block", "not a valid label", id="block-invalid-char"),
            pytest.param("block", "task-block", "not a valid label", id="block-hyphen"),
            pytest.param("block", "123task", "not a valid label", id="block-digit-prefix"),
            pytest.param("block", "", "empty", id="block-empty"),
            pytest.param("block", "   ", "empty", id="block-whitespace-only"),
            pytest.param("block", "my task", "not a valid label", id="block-space"),
        ],
    )
    def test_invalid_keys(self, model_name: str, value: str, error_fragment: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            if model_name == "parameter":
                WorkflowParameterYAML(
                    key=value,
                    workflow_parameter_type=WorkflowParameterType.STRING,
                )
            else:
                TaskBlockYAML(label=value, url="https://example.com")

        assert error_fragment in str(exc_info.value).lower()


class TestSanitizers:
    """Tests for identifier sanitizers."""

    @pytest.mark.parametrize(
        ("sanitizer", "raw_value", "expected"),
        [
            pytest.param(sanitize_block_label, "State/Province", "State_Province", id="block-label-slash"),
            pytest.param(sanitize_block_label, "my-block", "my_block", id="block-label-hyphen"),
            pytest.param(sanitize_block_label, "block.name", "block_name", id="block-label-dot"),
            pytest.param(sanitize_block_label, "State_/_Province", "State_Province", id="block-label-mixed-specials"),
            pytest.param(sanitize_block_label, "a__b___c", "a_b_c", id="block-label-collapse-underscores"),
            pytest.param(sanitize_block_label, "_my_block_", "my_block", id="block-label-trim-underscores"),
            pytest.param(sanitize_block_label, "123abc", "_123abc", id="block-label-digit-prefix"),
            pytest.param(sanitize_block_label, "_123abc", "_123abc", id="block-label-digit-prefix-after-strip"),
            pytest.param(sanitize_block_label, "///", "block", id="block-label-default"),
            pytest.param(sanitize_block_label, "", "block", id="block-label-empty-default"),
            pytest.param(sanitize_block_label, "my_valid_label", "my_valid_label", id="block-label-valid-unchanged"),
            pytest.param(sanitize_block_label, "my block name", "my_block_name", id="block-label-spaces"),
            pytest.param(sanitize_parameter_key, "State/Province", "State_Province", id="parameter-key-slash"),
            pytest.param(sanitize_parameter_key, "my-param", "my_param", id="parameter-key-hyphen"),
            pytest.param(sanitize_parameter_key, "param.name", "param_name", id="parameter-key-dot"),
            pytest.param(sanitize_parameter_key, "///", "parameter", id="parameter-key-default"),
            pytest.param(sanitize_parameter_key, "", "parameter", id="parameter-key-empty-default"),
            pytest.param(sanitize_parameter_key, "my_valid_key", "my_valid_key", id="parameter-key-valid-unchanged"),
        ],
    )
    def test_sanitizers(self, sanitizer: Callable[[str], str], raw_value: str, expected: str) -> None:
        assert sanitizer(raw_value) == expected


class TestReplaceJinjaReference:
    """Tests for the replace_jinja_reference function."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("Value is {{ old_key }}", "Value is {{ new_key }}", id="simple-reference"),
            pytest.param("Value is {{old_key}}", "Value is {{new_key}}", id="no-space-reference"),
            pytest.param("Value is {{ old_key.field }}", "Value is {{ new_key.field }}", id="attribute-access"),
            pytest.param(
                "Value is {{ old_key | default('') }}",
                "Value is {{ new_key | default('') }}",
                id="filter-expression",
            ),
            pytest.param("Value is {{ old_key[0] }}", "Value is {{ new_key[0] }}", id="index-access"),
            pytest.param(
                "{{ old_key }} and {{ old_key.field }}",
                "{{ new_key }} and {{ new_key.field }}",
                id="multiple-occurrences",
            ),
            pytest.param("{{ old_key_extended }}", "{{ old_key_extended }}", id="partial-match-unchanged"),
            pytest.param("{{ other_key }}", "{{ other_key }}", id="different-key-unchanged"),
            pytest.param(
                "{{ current_index < old_key }}",
                "{{ current_index < new_key }}",
                id="mid-expression-comparison",
            ),
            pytest.param(
                "{{ old_key + old_key }}",
                "{{ new_key + new_key }}",
                id="repeated-within-expression",
            ),
            pytest.param("{% if old_key %}yes{% endif %}", "{% if new_key %}yes{% endif %}", id="if-statement"),
            pytest.param(
                "{% for item in old_key %}{{ item }}{% endfor %}",
                "{% for item in new_key %}{{ item }}{% endfor %}",
                id="for-statement",
            ),
            pytest.param("{%- if old_key -%}", "{%- if new_key -%}", id="whitespace-control-statement"),
            pytest.param("{{\n  old_key\n  | trim }}", "{{\n  new_key\n  | trim }}", id="multiline-expression"),
            pytest.param("{% if old_key_extended %}", "{% if old_key_extended %}", id="statement-partial-unchanged"),
            pytest.param("{{ foo.old_key }}", "{{ foo.old_key }}", id="attribute-position-unchanged"),
            pytest.param("{{ func('old_key') }}", "{{ func('old_key') }}", id="string-literal-unchanged"),
            pytest.param(
                "{{ notify('ping old_key now') }}",
                "{{ notify('ping old_key now') }}",
                id="embedded-in-single-quoted-literal-unchanged",
            ),
            pytest.param(
                '{{ notify("ping old_key now") }}',
                '{{ notify("ping old_key now") }}',
                id="embedded-in-double-quoted-literal-unchanged",
            ),
            pytest.param(
                "{% if 'use old_key here' == mode %}",
                "{% if 'use old_key here' == mode %}",
                id="embedded-in-statement-literal-unchanged",
            ),
            pytest.param(
                r"{{ f('it\'s old_key here') }}",
                r"{{ f('it\'s old_key here') }}",
                id="escaped-quote-literal-unchanged",
            ),
            pytest.param(
                "{{ f('old_key') + old_key }}",
                "{{ f('old_key') + new_key }}",
                id="literal-preserved-bare-token-rewritten",
            ),
            pytest.param(
                "old_key outside delimiters",
                "old_key outside delimiters",
                id="outside-delimiters-unchanged",
            ),
            pytest.param("{{ old_key", "{{ new_key", id="unclosed-expression-leading-rewritten"),
            pytest.param(
                "{{ oops {% if old_key %}",
                "{{ oops {% if new_key %}",
                id="closed-statement-after-unclosed-expression-rewritten",
            ),
            pytest.param(
                "{% oops {{ x < old_key }}",
                "{% oops {{ x < new_key }}",
                id="closed-expression-after-unclosed-statement-rewritten",
            ),
            pytest.param(
                "{{ old_key {{ old_key",
                "{{ new_key {{ new_key",
                id="repeated-unclosed-leading-rewritten",
            ),
        ],
    )
    def test_replace_jinja_reference(self, text: str, expected: str) -> None:
        assert replace_jinja_reference(text, "old_key", "new_key") == expected

    @pytest.mark.parametrize(
        ("malformed",),
        [
            pytest.param("{{ " * 65536 + "old_key", id="unmatched-openers-with-spaces"),
            pytest.param("{{" * 65536 + "old_key", id="unmatched-openers-dense"),
            pytest.param("{% " * 65536 + "old_key", id="unmatched-statement-openers"),
        ],
    )
    def test_malformed_delimiters_scan_linearly(self, malformed: str) -> None:
        """Unmatched openers must not trigger quadratic rescans.

        The span search resumes where the previous span ended, so an input full of
        unmatched braces is scanned once. A lazy-regex implementation rescanned to the
        end of the input from every opener (~0.5s at 16k openers, quadrupling with input
        size); the generous absolute bound below fails that implementation at this size
        while leaving two orders of magnitude of headroom for a linear scan on slow CI.
        """
        start = time.perf_counter()
        replace_jinja_reference(malformed, "old_key", "new_key")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"malformed-delimiter scan took {elapsed:.2f}s — quadratic rescan regression"


class TestSanitizeWorkflowYamlWithReferences:
    """Tests for the sanitize_workflow_yaml_with_references function."""

    def test_sanitize_simple_block_label(self) -> None:
        """Test sanitizing a simple block label."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {"parameters": [], "blocks": [{"label": "State/Province", "block_type": "task"}]},
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "State_Province"

    def test_sanitize_updates_output_references(self) -> None:
        """Test that output references are updated when label is sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "my-block", "block_type": "task"},
                    {
                        "label": "second_block",
                        "block_type": "task",
                        "navigation_goal": "Use {{ my-block_output }} value",
                    },
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "my_block"
        assert "{{ my_block_output }}" in result["workflow_definition"]["blocks"][1]["navigation_goal"]

    def test_sanitize_updates_next_block_label(self) -> None:
        """Test that next_block_label is updated when label is sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "block-1", "block_type": "task", "next_block_label": "block-2"},
                    {"label": "block-2", "block_type": "task"},
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "block_1"
        assert result["workflow_definition"]["blocks"][0]["next_block_label"] == "block_2"
        assert result["workflow_definition"]["blocks"][1]["label"] == "block_2"

    def test_sanitize_updates_finally_block_label(self) -> None:
        """Test that finally_block_label is updated when referenced label is sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [{"label": "cleanup-block", "block_type": "task"}],
                "finally_block_label": "cleanup-block",
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "cleanup_block"
        assert result["workflow_definition"]["finally_block_label"] == "cleanup_block"

    def test_sanitize_nested_loop_blocks(self) -> None:
        """Test that nested blocks in for_loop are sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {
                        "label": "my_loop",
                        "block_type": "for_loop",
                        "loop_blocks": [{"label": "inner-block", "block_type": "task"}],
                    }
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["loop_blocks"][0]["label"] == "inner_block"

    def test_sanitize_no_changes_needed(self) -> None:
        """Test that valid labels are unchanged."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {"parameters": [], "blocks": [{"label": "valid_label", "block_type": "task"}]},
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "valid_label"

    def test_sanitize_empty_workflow_definition(self) -> None:
        """Test handling of missing workflow_definition."""
        workflow_yaml = {"title": "Test Workflow"}
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result == workflow_yaml

    def test_sanitize_updates_parameter_references(self) -> None:
        """Test that parameter references are updated."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {"key": "my_param", "parameter_type": "context", "source_parameter_key": "block-1_output"}
                ],
                "blocks": [{"label": "block-1", "block_type": "task"}],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "block_1"
        assert result["workflow_definition"]["parameters"][0]["source_parameter_key"] == "block_1_output"

    def test_sanitize_updates_references_in_statements_and_mid_expression(self) -> None:
        """Renamed keys are rewritten inside {% ... %} statements and mid-expression, not just after {{."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [{"key": "max attempts", "parameter_type": "workflow"}],
                "blocks": [
                    {
                        "label": "retry_loop",
                        "block_type": "while_loop",
                        "complete_criterion": "{{ current_index < max attempts }}",
                        "loop_blocks": [],
                    },
                    {
                        "label": "notify",
                        "block_type": "task",
                        "navigation_goal": "{% if max attempts %}Retry up to {{ max attempts }} times{% endif %}",
                    },
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        blocks = result["workflow_definition"]["blocks"]
        assert result["workflow_definition"]["parameters"][0]["key"] == "max_attempts"
        assert blocks[0]["complete_criterion"] == "{{ current_index < max_attempts }}"
        assert blocks[1]["navigation_goal"] == "{% if max_attempts %}Retry up to {{ max_attempts }} times{% endif %}"

    def test_sanitize_updates_output_references_in_statements(self) -> None:
        """Renamed block labels are rewritten in {label}_output references inside {% ... %} statements."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "my-block", "block_type": "task"},
                    {
                        "label": "guard",
                        "block_type": "task",
                        "navigation_goal": "{% if my-block_output.success %}Continue{% endif %}",
                    },
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        blocks = result["workflow_definition"]["blocks"]
        assert blocks[0]["label"] == "my_block"
        assert blocks[1]["navigation_goal"] == "{% if my_block_output.success %}Continue{% endif %}"

    def test_sanitize_parameter_key(self) -> None:
        """Test that parameter keys with invalid characters are sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {
                        "key": "State/Province",
                        "parameter_type": "workflow",
                    }
                ],
                "blocks": [],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["parameters"][0]["key"] == "State_Province"

    def test_sanitize_parameter_key_updates_jinja_references(self) -> None:
        """Test that Jinja references to sanitized parameter keys are updated."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {
                        "key": "user-input",
                        "parameter_type": "workflow",
                    }
                ],
                "blocks": [
                    {"label": "my_task", "block_type": "task", "navigation_goal": "Enter {{ user-input }} in the form"}
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["parameters"][0]["key"] == "user_input"
        assert "{{ user_input }}" in result["workflow_definition"]["blocks"][0]["navigation_goal"]

    def test_sanitize_parameter_key_updates_parameter_keys_array(self) -> None:
        """Test that parameter_keys arrays in blocks are updated."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {
                        "key": "my-param",
                        "parameter_type": "workflow",
                    }
                ],
                "blocks": [{"label": "my_task", "block_type": "task", "parameter_keys": ["my-param", "other_param"]}],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["parameters"][0]["key"] == "my_param"
        assert result["workflow_definition"]["blocks"][0]["parameter_keys"] == ["my_param", "other_param"]

    def test_sanitize_both_labels_and_parameter_keys(self) -> None:
        """Test that both block labels and parameter keys are sanitized together."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {
                        "key": "user/input",
                        "parameter_type": "workflow",
                    }
                ],
                "blocks": [
                    {
                        "label": "task-1",
                        "block_type": "task",
                        "navigation_goal": "Use {{ user/input }} and {{ task-1_output }}",
                    }
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["parameters"][0]["key"] == "user_input"
        assert result["workflow_definition"]["blocks"][0]["label"] == "task_1"
        nav_goal = result["workflow_definition"]["blocks"][0]["navigation_goal"]
        assert "{{ user_input }}" in nav_goal
        assert "{{ task_1_output }}" in nav_goal

    def test_sanitize_block_label_collision(self) -> None:
        """Test that block labels that sanitize to the same value get unique suffixes."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "state/province", "block_type": "task"},
                    {"label": "state-province", "block_type": "task"},
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        labels = [b["label"] for b in result["workflow_definition"]["blocks"]]
        assert labels[0] == "state_province"
        assert labels[1] == "state_province_2"
        # Ensure they are unique
        assert len(set(labels)) == len(labels)

    def test_sanitize_parameter_key_collision(self) -> None:
        """Test that parameter keys that sanitize to the same value get unique suffixes."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {"key": "user/input", "parameter_type": "workflow"},
                    {"key": "user-input", "parameter_type": "workflow"},
                ],
                "blocks": [
                    {
                        "label": "my_task",
                        "block_type": "task",
                        "navigation_goal": "Use {{ user/input }} and {{ user-input }}",
                    }
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        keys = [p["key"] for p in result["workflow_definition"]["parameters"]]
        assert keys[0] == "user_input"
        assert keys[1] == "user_input_2"
        # Ensure references are updated correctly
        nav_goal = result["workflow_definition"]["blocks"][0]["navigation_goal"]
        assert "{{ user_input }}" in nav_goal
        assert "{{ user_input_2 }}" in nav_goal

    def test_sanitize_collision_with_existing_valid_label(self) -> None:
        """Test that sanitized labels don't collide with already-valid labels."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "my_block", "block_type": "task"},
                    {"label": "my-block", "block_type": "task"},
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        labels = [b["label"] for b in result["workflow_definition"]["blocks"]]
        assert labels[0] == "my_block"
        assert labels[1] == "my_block_2"

    def test_sanitize_shorthand_block_label_references(self) -> None:
        """Test that shorthand block label references ({{ label }} without _output) are also updated."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {
                        "label": "extract/block",
                        "block_type": "extraction",
                    },
                    {
                        "label": "send_block",
                        "block_type": "send_email",
                        # Both shorthand {{ label }} and full {{ label_output }} patterns
                        "body": "Data: {{ extract/block.extracted_information }} and {{ extract/block_output.status }}",
                    },
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "extract_block"
        body = result["workflow_definition"]["blocks"][1]["body"]
        # Shorthand reference should be updated
        assert "{{ extract_block.extracted_information }}" in body
        # Full _output reference should also be updated
        assert "{{ extract_block_output.status }}" in body

    def test_sanitize_label_shorthand_does_not_corrupt_output_ref(self) -> None:
        """Ensure shorthand label replacement does not corrupt _output references.

        When a label like 'block-1' is sanitized to 'block_1', both the shorthand
        {{ block-1 }} and output {{ block-1_output }} patterns must be updated
        independently without the shorthand replacement corrupting the _output form.
        """
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {
                        "label": "block-1",
                        "block_type": "task",
                        "navigation_goal": "{{ block-1 }} and {{ block-1_output }}",
                    }
                ],
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        goal = result["workflow_definition"]["blocks"][0]["navigation_goal"]
        assert "{{ block_1 }}" in goal
        assert "{{ block_1_output }}" in goal

    def test_sanitize_updates_output_references_in_workflow_system_prompt(self) -> None:
        """Output references inside the workflow-level workflow_system_prompt must
        be rewritten when the referenced block label is sanitized. The global
        prompt is resolved through Jinja at execution time, so its references
        need the same renaming treatment as block-level fields."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [],
                "blocks": [{"label": "my-block", "block_type": "task"}],
                "workflow_system_prompt": "Honor {{ my-block_output }} for every downstream block.",
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["blocks"][0]["label"] == "my_block"
        assert "{{ my_block_output }}" in result["workflow_definition"]["workflow_system_prompt"]

    def test_sanitize_parameter_key_updates_jinja_references_in_workflow_system_prompt(self) -> None:
        """Parameter-key references inside the workflow-level workflow_system_prompt
        must be rewritten when the parameter key is sanitized."""
        workflow_yaml = {
            "title": "Test Workflow",
            "workflow_definition": {
                "parameters": [
                    {
                        "key": "user-input",
                        "parameter_type": "workflow",
                    }
                ],
                "blocks": [],
                "workflow_system_prompt": "Always respond in the style of {{ user-input }}.",
            },
        }
        result = sanitize_workflow_yaml_with_references(workflow_yaml)
        assert result["workflow_definition"]["parameters"][0]["key"] == "user_input"
        assert "{{ user_input }}" in result["workflow_definition"]["workflow_system_prompt"]
