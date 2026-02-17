"""Tests for workflow parameter key and block label validation.

These tests ensure that parameter keys and block labels are valid Python/Jinja2 identifiers,
preventing runtime errors like "'State_' is undefined" when using keys like "State_/_Province".
"""

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


class TestParameterKeyValidation:
    """Tests for parameter key validation."""

    def test_valid_parameter_key_simple(self) -> None:
        """Test that simple valid keys are accepted."""
        param = WorkflowParameterYAML(
            key="my_parameter",
            workflow_parameter_type=WorkflowParameterType.STRING,
        )
        assert param.key == "my_parameter"

    def test_valid_parameter_key_with_numbers(self) -> None:
        """Test that keys with numbers (not at start) are accepted."""
        param = WorkflowParameterYAML(
            key="param123",
            workflow_parameter_type=WorkflowParameterType.STRING,
        )
        assert param.key == "param123"

    def test_valid_parameter_key_underscore_prefix(self) -> None:
        """Test that keys starting with underscore are accepted."""
        param = WorkflowParameterYAML(
            key="_private_param",
            workflow_parameter_type=WorkflowParameterType.STRING,
        )
        assert param.key == "_private_param"

    def test_valid_parameter_key_single_letter(self) -> None:
        """Test that single letter keys are accepted."""
        param = WorkflowParameterYAML(
            key="x",
            workflow_parameter_type=WorkflowParameterType.STRING,
        )
        assert param.key == "x"

    def test_invalid_parameter_key_with_slash(self) -> None:
        """Test that keys with '/' are rejected (the main bug case from SKY-7356)."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="State_/_Province",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "not a valid parameter name" in error_msg

    def test_invalid_parameter_key_with_hyphen(self) -> None:
        """Test that keys with '-' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="state-or-province",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "not a valid parameter name" in error_msg

    def test_invalid_parameter_key_with_dot(self) -> None:
        """Test that keys with '.' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="some.property",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "not a valid parameter name" in error_msg

    def test_invalid_parameter_key_starts_with_digit(self) -> None:
        """Test that keys starting with a digit are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="123param",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "not a valid parameter name" in error_msg

    def test_invalid_parameter_key_with_space(self) -> None:
        """Test that keys with spaces are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="my parameter",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "whitespace" in error_msg

    def test_invalid_parameter_key_with_tab(self) -> None:
        """Test that keys with tabs are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="my\tparameter",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "whitespace" in error_msg

    def test_invalid_parameter_key_with_asterisk(self) -> None:
        """Test that keys with '*' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowParameterYAML(
                key="param*value",
                workflow_parameter_type=WorkflowParameterType.STRING,
            )
        error_msg = str(exc_info.value)
        assert "not a valid parameter name" in error_msg


class TestBlockLabelValidation:
    """Tests for block label validation."""

    def test_valid_block_label_simple(self) -> None:
        """Test that simple valid labels are accepted."""
        block = TaskBlockYAML(label="my_task", url="https://example.com")
        assert block.label == "my_task"

    def test_valid_block_label_with_numbers(self) -> None:
        """Test that labels with numbers (not at start) are accepted."""
        block = TaskBlockYAML(label="task123", url="https://example.com")
        assert block.label == "task123"

    def test_valid_block_label_underscore_prefix(self) -> None:
        """Test that labels starting with underscore are accepted."""
        block = TaskBlockYAML(label="_private_task", url="https://example.com")
        assert block.label == "_private_task"

    def test_invalid_block_label_with_slash(self) -> None:
        """Test that labels with '/' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="task/block", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "not a valid label" in error_msg

    def test_invalid_block_label_with_hyphen(self) -> None:
        """Test that labels with '-' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="task-block", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "not a valid label" in error_msg

    def test_invalid_block_label_starts_with_digit(self) -> None:
        """Test that labels starting with a digit are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="123task", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "not a valid label" in error_msg

    def test_invalid_block_label_empty(self) -> None:
        """Test that empty labels are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "empty" in error_msg.lower()

    def test_invalid_block_label_whitespace_only(self) -> None:
        """Test that whitespace-only labels are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="   ", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "empty" in error_msg.lower()

    def test_invalid_block_label_with_space(self) -> None:
        """Test that labels with spaces are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            TaskBlockYAML(label="my task", url="https://example.com")
        error_msg = str(exc_info.value)
        assert "not a valid label" in error_msg


class TestSanitizeBlockLabel:
    """Tests for the sanitize_block_label function."""

    def test_sanitize_slash(self) -> None:
        """Test that slashes are replaced with underscores."""
        assert sanitize_block_label("State/Province") == "State_Province"

    def test_sanitize_hyphen(self) -> None:
        """Test that hyphens are replaced with underscores."""
        assert sanitize_block_label("my-block") == "my_block"

    def test_sanitize_dot(self) -> None:
        """Test that dots are replaced with underscores."""
        assert sanitize_block_label("block.name") == "block_name"

    def test_sanitize_multiple_special_chars(self) -> None:
        """Test that multiple special characters are handled."""
        assert sanitize_block_label("State_/_Province") == "State_Province"

    def test_sanitize_consecutive_underscores(self) -> None:
        """Test that consecutive underscores are collapsed."""
        assert sanitize_block_label("a__b___c") == "a_b_c"

    def test_sanitize_leading_trailing_underscores(self) -> None:
        """Test that leading/trailing underscores are removed."""
        assert sanitize_block_label("_my_block_") == "my_block"

    def test_sanitize_digit_prefix(self) -> None:
        """Test that labels starting with digits get underscore prefix."""
        assert sanitize_block_label("123abc") == "_123abc"

    def test_sanitize_digit_prefix_after_strip(self) -> None:
        """Test that digit prefix is added after stripping underscores."""
        assert sanitize_block_label("_123abc") == "_123abc"

    def test_sanitize_all_invalid_chars(self) -> None:
        """Test that if all chars are invalid, default is returned."""
        assert sanitize_block_label("///") == "block"

    def test_sanitize_empty_string(self) -> None:
        """Test that empty string returns default."""
        assert sanitize_block_label("") == "block"

    def test_sanitize_valid_label_unchanged(self) -> None:
        """Test that valid labels are unchanged."""
        assert sanitize_block_label("my_valid_label") == "my_valid_label"

    def test_sanitize_spaces(self) -> None:
        """Test that spaces are replaced with underscores."""
        assert sanitize_block_label("my block name") == "my_block_name"


class TestSanitizeParameterKey:
    """Tests for the sanitize_parameter_key function."""

    def test_sanitize_slash(self) -> None:
        """Test that slashes are replaced with underscores."""
        assert sanitize_parameter_key("State/Province") == "State_Province"

    def test_sanitize_hyphen(self) -> None:
        """Test that hyphens are replaced with underscores."""
        assert sanitize_parameter_key("my-param") == "my_param"

    def test_sanitize_dot(self) -> None:
        """Test that dots are replaced with underscores."""
        assert sanitize_parameter_key("param.name") == "param_name"

    def test_sanitize_all_invalid_chars(self) -> None:
        """Test that if all chars are invalid, default is returned."""
        assert sanitize_parameter_key("///") == "parameter"

    def test_sanitize_empty_string(self) -> None:
        """Test that empty string returns default."""
        assert sanitize_parameter_key("") == "parameter"

    def test_sanitize_valid_key_unchanged(self) -> None:
        """Test that valid keys are unchanged."""
        assert sanitize_parameter_key("my_valid_key") == "my_valid_key"


class TestReplaceJinjaReference:
    """Tests for the replace_jinja_reference function."""

    def test_replace_simple_reference(self) -> None:
        """Test replacing a simple Jinja reference."""
        text = "Value is {{ old_key }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "Value is {{ new_key }}"

    def test_replace_reference_no_spaces(self) -> None:
        """Test replacing a reference without spaces."""
        text = "Value is {{old_key}}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "Value is {{new_key}}"

    def test_replace_reference_with_attribute(self) -> None:
        """Test replacing a reference with attribute access."""
        text = "Value is {{ old_key.field }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "Value is {{ new_key.field }}"

    def test_replace_reference_with_filter(self) -> None:
        """Test replacing a reference with filter."""
        text = "Value is {{ old_key | default('') }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "Value is {{ new_key | default('') }}"

    def test_replace_reference_with_index(self) -> None:
        """Test replacing a reference with index access."""
        text = "Value is {{ old_key[0] }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "Value is {{ new_key[0] }}"

    def test_replace_multiple_references(self) -> None:
        """Test replacing multiple occurrences."""
        text = "{{ old_key }} and {{ old_key.field }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "{{ new_key }} and {{ new_key.field }}"

    def test_no_replace_partial_match(self) -> None:
        """Test that partial matches are not replaced."""
        text = "{{ old_key_extended }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "{{ old_key_extended }}"

    def test_no_replace_different_key(self) -> None:
        """Test that different keys are not affected."""
        text = "{{ other_key }}"
        result = replace_jinja_reference(text, "old_key", "new_key")
        assert result == "{{ other_key }}"


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
