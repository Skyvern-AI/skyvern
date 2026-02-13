import json
from datetime import datetime, timezone
from enum import StrEnum
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models.block import (
    _JSON_TYPE_MARKER,
    HttpRequestBlock,
    _json_type_filter,
    jinja_sandbox_env,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


class TestJsonTypeFilter:
    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            42,
            19.99,
            None,
            [1, 2, 3],
            {"a": 1, "b": "hello"},
            "hello",
            [],
            {},
        ],
    )
    def test_filter_wraps_with_marker(self, value: object) -> None:
        result = _json_type_filter(value)
        assert result.startswith(_JSON_TYPE_MARKER)
        assert result.endswith(_JSON_TYPE_MARKER)

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            42,
            19.99,
            None,
            [1, 2, 3],
            {"a": 1, "b": "hello"},
            "hello",
        ],
    )
    def test_filter_json_is_parseable(self, value: object) -> None:
        result = _json_type_filter(value)
        json_part = result[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == value

    def test_filter_handles_datetime(self) -> None:
        now = datetime(2024, 1, 15, 12, 30, 45)
        result = _json_type_filter(now)
        json_part = result[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == "2024-01-15 12:30:45"

    def test_filter_handles_enum(self) -> None:
        class Status(StrEnum):
            completed = "completed"
            failed = "failed"

        result = _json_type_filter(Status.completed)
        json_part = result[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == "completed"

    def test_filter_handles_nested_datetime_in_dict(self) -> None:
        data = {
            "status": "completed",
            "downloaded_files": [
                {"url": "https://example.com/file.pdf", "modified_at": datetime(2024, 1, 15, 12, 30, 45)}
            ],
        }
        result = _json_type_filter(data)
        json_part = result[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed["downloaded_files"][0]["modified_at"] == "2024-01-15 12:30:45"


class TestJinjaJsonFilterRegistration:
    def test_json_filter_is_registered(self) -> None:
        assert "json" in jinja_sandbox_env.filters
        assert jinja_sandbox_env.filters["json"] == _json_type_filter

    @pytest.mark.parametrize(
        "template,context,expected_json",
        [
            ("{{ flag | json }}", {"flag": True}, True),
            ("{{ flag | json }}", {"flag": False}, False),
            ("{{ count | json }}", {"count": 42}, 42),
            ("{{ price | json }}", {"price": 19.99}, 19.99),
            ("{{ null_val | json }}", {"null_val": None}, None),
            ("{{ items | json }}", {"items": [1, 2, 3]}, [1, 2, 3]),
            ("{{ data | json }}", {"data": {"a": 1}}, {"a": 1}),
            ("{{ str_val | json }}", {"str_val": "hello"}, "hello"),
        ],
    )
    def test_jinja_renders_json_filter(self, template: str, context: dict, expected_json: object) -> None:
        rendered = jinja_sandbox_env.from_string(template).render(context)
        # The output should have markers
        assert rendered.startswith(_JSON_TYPE_MARKER)
        assert rendered.endswith(_JSON_TYPE_MARKER)
        # Extract and parse JSON
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == expected_json

    def test_json_filter_with_nested_access(self) -> None:
        template = "{{ data.nested.value | json }}"
        context = {"data": {"nested": {"value": True}}}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) is True

    def test_json_filter_with_list_index(self) -> None:
        template = "{{ items[1] | json }}"
        context = {"items": [10, 20, 30]}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) == 20

    def test_json_filter_chains_with_default(self) -> None:
        template = "{{ missing_val | default(false) | json }}"
        context = {}  # missing_val not defined
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) is False

    def test_json_filter_chains_with_default_list(self) -> None:
        template = "{{ items | default([]) | json }}"
        context = {}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) == []


class TestMarkerDetection:
    def test_marker_detection_simple(self) -> None:
        wrapped = _json_type_filter(True)
        assert wrapped.startswith(_JSON_TYPE_MARKER)
        assert wrapped.endswith(_JSON_TYPE_MARKER)
        # Simulate the detection logic from _render_templates_in_json
        json_str = wrapped[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_str) is True

    def test_marker_detection_complex_object(self) -> None:
        complex_obj = {"users": [{"name": "Alice", "active": True}], "count": 1}
        wrapped = _json_type_filter(complex_obj)
        json_str = wrapped[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_str) == complex_obj

    def test_plain_string_not_detected_as_marker(self) -> None:
        plain_string = "hello world"
        assert not plain_string.startswith(_JSON_TYPE_MARKER)
        assert not plain_string.endswith(_JSON_TYPE_MARKER)

    def test_partial_marker_not_detected(self) -> None:
        start_only = f"{_JSON_TYPE_MARKER}true"
        end_only = f"true{_JSON_TYPE_MARKER}"
        assert not (start_only.startswith(_JSON_TYPE_MARKER) and start_only.endswith(_JSON_TYPE_MARKER))
        assert not (end_only.startswith(_JSON_TYPE_MARKER) and end_only.endswith(_JSON_TYPE_MARKER))


class TestWithoutJsonFilter:
    def test_standard_template_renders_string(self) -> None:
        template = "{{ flag }}"
        context = {"flag": True}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        assert rendered == "True"  # Python str(True)
        assert not rendered.startswith(_JSON_TYPE_MARKER)

    def test_standard_template_integer(self) -> None:
        template = "{{ count }}"
        context = {"count": 42}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        assert rendered == "42"
        assert not rendered.startswith(_JSON_TYPE_MARKER)


class TestEdgeCasesAndLimitations:
    def test_mixed_template_jinja_output_contains_marker(self) -> None:
        """Jinja renders mixed templates with markers embedded in output.

        This verifies what Jinja produces. The actual error handling happens
        in _render_templates_in_json (tested separately below).
        """
        template = "prefix_{{ flag | json }}_suffix"
        context = {"flag": True}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        # The output contains the marker because it's mixed with prefix/suffix
        assert _JSON_TYPE_MARKER in rendered
        assert rendered.startswith("prefix_")
        assert rendered.endswith("_suffix")
        # The marker detection logic (startswith AND endswith) will NOT match
        assert not (rendered.startswith(_JSON_TYPE_MARKER) and rendered.endswith(_JSON_TYPE_MARKER))

    def test_deeply_nested_structure(self) -> None:
        template = "{{ data | json }}"
        context = {
            "data": {
                "level1": {
                    "level2": {
                        "level3": {
                            "items": [1, 2, 3],
                            "active": True,
                        }
                    }
                }
            }
        }
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == context["data"]
        assert parsed["level1"]["level2"]["level3"]["active"] is True

    def test_special_characters_in_string_value(self) -> None:
        template = "{{ text | json }}"
        context = {"text": 'Hello "World"\nNew line\ttab'}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == context["text"]

    def test_unicode_characters(self) -> None:
        template = "{{ text | json }}"
        context = {"text": "Hello \u4e16\u754c \U0001f600"}  # Chinese + emoji
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)
        assert parsed == context["text"]

    def test_empty_values(self) -> None:
        # Empty string
        template = "{{ text | json }}"
        context: dict[str, object] = {"text": ""}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) == ""

        # Empty list
        context = {"text": []}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) == []

        # Empty dict
        context = {"text": {}}
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        assert json.loads(json_part) == {}


class TestEmbeddedMarkerErrorHandling:
    """Tests that embedded markers (| json mixed with other text) raise clear errors."""

    def test_embedded_marker_raises_error(self) -> None:
        """Using | json with prefix/suffix text should raise FailedToFormatJinjaStyleParameter."""
        now = datetime.now(timezone.utc)
        output_param = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="http_output",
            description=None,
            output_parameter_id="output-1",
            workflow_id="workflow-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        )

        block = HttpRequestBlock(
            label="test-block",
            url="https://example.com",
            method="POST",
            body={"id": "prefix-{{ val | json }}"},
            output_parameter=output_param,
        )

        mock_context = MagicMock()
        mock_context.values = {"val": 123}
        mock_context.secrets = {}
        mock_context.include_secrets_in_templates = False
        mock_context.get_block_metadata = MagicMock(return_value={})

        with pytest.raises(FailedToFormatJinjaStyleParameter) as exc_info:
            block.format_potential_template_parameters(mock_context)

        assert "can only be used for complete value replacement" in str(exc_info.value)

    def test_valid_json_filter_does_not_raise(self) -> None:
        """Using | json for complete value replacement should work without error."""
        now = datetime.now(timezone.utc)
        output_param = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="http_output",
            description=None,
            output_parameter_id="output-1",
            workflow_id="workflow-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        )

        block = HttpRequestBlock(
            label="test-block",
            url="https://example.com",
            method="POST",
            body={"enabled": "{{ flag | json }}", "count": "{{ num | json }}"},
            output_parameter=output_param,
        )

        mock_context = MagicMock()
        mock_context.values = {"flag": True, "num": 42}
        mock_context.secrets = {}
        mock_context.include_secrets_in_templates = False
        mock_context.get_block_metadata = MagicMock(return_value={})

        # Should not raise
        block.format_potential_template_parameters(mock_context)

        # Verify the values were correctly converted to native types
        assert block.body == {"enabled": True, "count": 42}


class TestWorkflowRunSummary:
    """Tests for the workflow_run_summary template variable."""

    def test_build_workflow_run_summary_empty_outputs(self) -> None:
        """Test summary with no block outputs."""
        context = MagicMock()
        context.workflow_run_id = "wr_123"
        context.workflow_run_outputs = {}

        # Create a real context to test the method
        summary = WorkflowRunContext.build_workflow_run_summary(context)

        assert summary["workflow_run_id"] == "wr_123"
        assert summary["status"] is None
        assert summary["output"] == {"extracted_information": {}}
        assert summary["downloaded_files"] == []
        assert summary["errors"] == []
        assert summary["failure_reason"] is None

    def test_build_workflow_run_summary_merges_extracted_information(self) -> None:
        """Test that output.extracted_information is merged from all blocks."""
        context = MagicMock()
        context.workflow_run_id = "wr_456"
        context.workflow_run_outputs = {
            "NavigationBlock": {
                "status": "completed",
                "extracted_information": {"title": "Example Page"},
                "errors": [],
                "downloaded_files": [],
            },
            "ExtractionBlock": {
                "status": "completed",
                "extracted_information": {"documents": [{"name": "doc1.pdf"}]},
                "errors": [],
                "downloaded_files": [],
            },
        }

        summary = WorkflowRunContext.build_workflow_run_summary(context)

        # extracted_information is merged from all blocks (flattened, not keyed by block label)
        assert summary["output"]["extracted_information"] == {
            "title": "Example Page",
            "documents": [{"name": "doc1.pdf"}],
        }

    def test_build_workflow_run_summary_aggregates_downloaded_files(self) -> None:
        """Test that downloaded_files are aggregated from all blocks."""
        context = MagicMock()
        context.workflow_run_id = "wr_789"
        context.workflow_run_outputs = {
            "Block1": {
                "status": "completed",
                "downloaded_files": [{"url": "file1.pdf"}],
                "errors": [],
            },
            "Block2": {
                "status": "completed",
                "downloaded_files": [{"url": "file2.pdf"}, {"url": "file3.pdf"}],
                "errors": [],
            },
        }

        summary = WorkflowRunContext.build_workflow_run_summary(context)

        assert len(summary["downloaded_files"]) == 3
        assert {"url": "file1.pdf"} in summary["downloaded_files"]
        assert {"url": "file2.pdf"} in summary["downloaded_files"]
        assert {"url": "file3.pdf"} in summary["downloaded_files"]

    def test_build_workflow_run_summary_aggregates_errors(self) -> None:
        """Test that errors are aggregated from all blocks."""
        context = MagicMock()
        context.workflow_run_id = "wr_errors"
        context.workflow_run_outputs = {
            "Block1": {
                "status": "failed",
                "errors": [{"message": "Error 1"}],
                "failure_reason": "Block 1 failed",
            },
            "Block2": {
                "status": "completed",
                "errors": [{"message": "Warning"}],
            },
        }

        summary = WorkflowRunContext.build_workflow_run_summary(context)

        assert len(summary["errors"]) == 2
        assert {"message": "Error 1"} in summary["errors"]
        assert {"message": "Warning"} in summary["errors"]
        assert summary["failure_reason"] == "Block 1 failed"

    def test_build_workflow_run_summary_uses_last_status(self) -> None:
        """Test that the last block's status is used."""
        context = MagicMock()
        context.workflow_run_id = "wr_status"
        context.workflow_run_outputs = {
            "Block1": {"status": "completed", "errors": []},
            "Block2": {"status": "failed", "errors": []},
            "Block3": {"status": "completed", "errors": []},
        }

        summary = WorkflowRunContext.build_workflow_run_summary(context)
        # Last block's status is used
        assert summary["status"] == "completed"

    def test_status_converted_to_string_in_summary(self) -> None:
        """Test that TaskStatus enum is converted to string in summary."""
        context = MagicMock()
        context.workflow_run_id = "wr_enum"
        context.workflow_run_outputs = {
            "Block1": {"status": TaskStatus.completed, "errors": []},
            "Block2": {"status": TaskStatus.timed_out, "errors": []},
        }

        summary = WorkflowRunContext.build_workflow_run_summary(context)

        assert summary["status"] == "timed_out"
        assert type(summary["status"]) is str  # Not TaskStatus enum

    def test_workflow_run_summary_with_json_filter(self) -> None:
        """Test that workflow_run_summary works with | json filter in templates."""
        template = "{{ workflow_run_summary | json }}"
        context = {
            "workflow_run_summary": {
                "workflow_run_id": "wr_template",
                "status": "completed",
                "output": {"extracted_information": {"documents": [{"name": "doc1.pdf"}]}},
                "downloaded_files": [{"url": "file.pdf"}],
                "errors": [],
                "failure_reason": None,
            }
        }
        rendered = jinja_sandbox_env.from_string(template).render(context)
        json_part = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
        parsed = json.loads(json_part)

        assert parsed["workflow_run_id"] == "wr_template"
        assert parsed["status"] == "completed"
        assert parsed["output"]["extracted_information"]["documents"] == [{"name": "doc1.pdf"}]
        assert parsed["downloaded_files"] == [{"url": "file.pdf"}]
        assert parsed["errors"] == []
        assert parsed["failure_reason"] is None
