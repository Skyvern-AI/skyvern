"""Tests for workflow-level error_code_mapping inheritance into blocks at execution time."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="task1_output",
        description="test output",
        output_parameter_id="op_task1",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_block(error_code_mapping: dict[str, str] | None = None) -> TaskBlock:
    return TaskBlock(
        label="task1",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping=error_code_mapping,
    )


def _make_workflow(error_code_mapping: dict[str, str] | None) -> Workflow:
    workflow_definition = WorkflowDefinition(
        parameters=[],
        blocks=[],
        error_code_mapping=error_code_mapping,
    )
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=workflow_definition,
        created_at=now,
        modified_at=now,
    )


def _make_workflow_run_context(workflow_error_code_mapping: dict[str, str] | None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        workflow=_make_workflow(workflow_error_code_mapping),
    )
    return ctx


class TestWorkflowLevelErrorCodeMappingInheritance:
    def test_block_inherits_workflow_mapping_when_none(self) -> None:
        block = _make_task_block(error_code_mapping=None)
        ctx = _make_workflow_run_context({"ACCOUNT_NOT_FOUND": "If no records found, terminate"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"ACCOUNT_NOT_FOUND": "If no records found, terminate"}

    def test_block_merges_with_workflow_mapping(self) -> None:
        block = _make_task_block(error_code_mapping={"BLOCK_ERROR": "block-level error"})
        ctx = _make_workflow_run_context({"WORKFLOW_ERROR": "workflow-level error"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {
            "WORKFLOW_ERROR": "workflow-level error",
            "BLOCK_ERROR": "block-level error",
        }

    def test_block_level_overrides_workflow_on_conflict(self) -> None:
        block = _make_task_block(error_code_mapping={"SHARED_KEY": "block wins"})
        ctx = _make_workflow_run_context({"SHARED_KEY": "workflow loses"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"SHARED_KEY": "block wins"}

    def test_no_workflow_mapping_preserves_block(self) -> None:
        block = _make_task_block(error_code_mapping={"BLOCK_ERROR": "only block"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"BLOCK_ERROR": "only block"}

    def test_both_none_stays_none(self) -> None:
        block = _make_task_block(error_code_mapping=None)
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is None

    def test_entry_whose_key_renders_empty_is_dropped_not_persisted(self) -> None:
        # error_code_mapping is templatable, so the author-time schema validates a string that is not
        # yet the string the model will see. "{{ company }}" is a legal key at save time and renders
        # to "" here -- the empty-key case the schema cannot reach. Production has one workflow with
        # a templated error-code key, so this path is real, not hypothetical.
        block = _make_task_block(error_code_mapping={"{{ company }}": "no rating found", "OK_CODE": "kept"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.values["company"] = ""

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"OK_CODE": "kept"}

    def test_entry_whose_key_renders_over_length_is_dropped(self) -> None:
        # The other direction: a key well inside the 128-character limit at save time renders past it.
        block = _make_task_block(error_code_mapping={"CODE_{{ suffix }}": "d", "OK_CODE": "kept"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.values["suffix"] = "x" * 200

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"OK_CODE": "kept"}

    def test_a_mapping_that_renders_entirely_invalid_becomes_none_not_empty(self) -> None:
        # Downstream treats a falsy mapping as "no codes offered"; an empty dict would read as a
        # mapping that exists and offers nothing, which is a different thing to the error detector.
        block = _make_task_block(error_code_mapping={"{{ company }}": "no rating found"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.values["company"] = ""

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is None

    def test_a_templated_key_that_renders_valid_is_kept(self) -> None:
        # The guard must not break templating itself -- it is a deliberate feature of this field.
        block = _make_task_block(error_code_mapping={"{{ company }}_MISSING": "not found"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.values["company"] = "ACME"

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"ACME_MISSING": "not found"}

    def test_entries_past_the_aggregate_entry_cap_are_dropped(self) -> None:
        # Per-entry rules bound one string; only a running total bounds what a mapping can become.
        # CodeBlock already enforces this at render; BaseTaskBlock did not.
        from skyvern.schemas.workflows import ERROR_CODE_MAPPING_MAX_ENTRIES

        mapping = {f"CODE_{index}": "description" for index in range(ERROR_CODE_MAPPING_MAX_ENTRIES + 10)}
        block = _make_task_block(error_code_mapping=mapping)
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is not None
        assert len(block.error_code_mapping) == ERROR_CODE_MAPPING_MAX_ENTRIES

    def test_entries_past_the_aggregate_byte_cap_are_dropped(self) -> None:
        # Each entry is inside the per-entry character limit; together they are far past the byte cap,
        # and this mapping is JSON-dumped into the prompt.
        from skyvern.schemas.workflows import ERROR_CODE_MAPPING_MAX_UTF8_BYTES

        mapping = {f"CODE_{index}": "d" * 2000 for index in range(40)}
        block = _make_task_block(error_code_mapping=mapping)
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is not None
        total = sum(len(k.encode()) + len(v.encode()) for k, v in block.error_code_mapping.items())
        assert total <= ERROR_CODE_MAPPING_MAX_UTF8_BYTES
        assert len(block.error_code_mapping) < len(mapping)

    def test_a_key_that_renders_to_contain_a_registered_secret_is_dropped(self) -> None:
        # A description can be redacted; a key cannot, because it is the identifier the model names
        # and the customer matches on. task.errors carries it out over the customer's webhook.
        block = _make_task_block(error_code_mapping={"FAILED_{{ token }}": "d", "OK_CODE": "kept"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.values["token"] = "sk4829137765"
        ctx.secrets["sk_param"] = "sk4829137765"

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"OK_CODE": "kept"}

    def test_an_untrimmed_description_is_normalized_not_dropped(self) -> None:
        # The regression this PR round exists to prevent. Measured on production: 3,691 tasks a week
        # carry an untrimmed description and 90 would lose EVERY entry -- and a falsy mapping makes
        # error_detection_service skip detection entirely, so a customer silently stops receiving a
        # code they get today. A description is prose; the author's intent is recoverable.
        block = _make_task_block(error_code_mapping={"NO_RATING": "No rating found.\nTerminate.\n"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"NO_RATING": "No rating found. Terminate."}

    def test_a_short_registered_secret_does_not_delete_a_legitimate_code(self) -> None:
        # A bare substring test with no length floor lets a card-expiry "05" make HTTP_405_DECLINED
        # look secret-bearing. secret_redaction already carries the floors for exactly this reason.
        block = _make_task_block(error_code_mapping={"HTTP_405_DECLINED": "the gateway declined it"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)
        ctx.secrets["expiry"] = "05"

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"HTTP_405_DECLINED": "the gateway declined it"}

    def test_the_aggregate_cap_evicts_workflow_entries_before_the_blocks_own(self) -> None:
        # Merge order made block entries land last, so the cap evicted exactly the entries the
        # call site documents as taking precedence: 64 workflow entries plus 2 block entries kept
        # none of the block's own.
        from skyvern.schemas.workflows import ERROR_CODE_MAPPING_MAX_ENTRIES

        workflow_mapping = {f"WF_{index}": "workflow entry" for index in range(ERROR_CODE_MAPPING_MAX_ENTRIES)}
        block = _make_task_block(error_code_mapping={"BLOCK_A": "block entry", "BLOCK_B": "block entry"})
        ctx = _make_workflow_run_context(workflow_mapping)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is not None
        assert "BLOCK_A" in block.error_code_mapping
        assert "BLOCK_B" in block.error_code_mapping
        assert len(block.error_code_mapping) == ERROR_CODE_MAPPING_MAX_ENTRIES

    def test_sanitizer_rewrites_references_in_workflow_error_code_mapping(self) -> None:
        """Auto-sanitized labels/param keys must be rewritten inside workflow-level error_code_mapping."""
        from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references

        workflow_yaml = {
            "workflow_definition": {
                "parameters": [{"key": "bad-key", "parameter_type": "workflow", "workflow_parameter_type": "string"}],
                "blocks": [{"label": "block-1", "block_type": "task", "url": "https://example.com"}],
                "error_code_mapping": {
                    "ERR": "reason {{ bad-key }} from {{ block-1_output }}",
                    "ERR_{{ bad-key }}": "key-side ref to {{ block-1_output }}",
                },
            }
        }
        sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
        mapping = sanitized["workflow_definition"]["error_code_mapping"]
        assert mapping == {
            "ERR": "reason {{ bad_key }} from {{ block_1_output }}",
            "ERR_{{ bad_key }}": "key-side ref to {{ block_1_output }}",
        }

    def test_sanitizer_does_not_chain_rewrites_in_error_code_mapping(self) -> None:
        """Chained substitutions must not occur when one sanitized label collides with another's final name."""
        from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references

        # Both labels need sanitization; the first normalizes to "foo_bar", colliding
        # with the second whose normalization is "foo_bar", so it becomes "foo_bar_2".
        workflow_yaml = {
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "foo/bar", "block_type": "task", "url": "https://example.com"},
                    {"label": "foo-bar", "block_type": "task", "url": "https://example.com"},
                ],
                "error_code_mapping": {
                    "ERR": "first {{ foo/bar_output }}, second {{ foo-bar_output }}",
                },
            }
        }
        sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
        # foo/bar -> foo_bar should stay as foo_bar (not chain-rewrite to foo_bar_2).
        mapping = sanitized["workflow_definition"]["error_code_mapping"]
        assert mapping == {"ERR": "first {{ foo_bar_output }}, second {{ foo_bar_2_output }}"}

    def test_round_trip_does_not_bake_workflow_defaults(self) -> None:
        """Regression: converted blocks must not persist workflow-level keys.

        Without this guarantee, removing a workflow-level code would leave stale copies in each block
        after a read-modify-write round-trip.
        """
        from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
        from skyvern.schemas.workflows import TaskBlockYAML

        block_yaml = TaskBlockYAML(
            label="task1",
            url="https://example.com",
            navigation_goal="Do something",
            error_code_mapping={"BLOCK_ERROR": "only block"},
        )
        output_param = _make_output_parameter()
        parameters = {output_param.key: output_param}

        block = block_yaml_to_block(block_yaml, parameters)
        assert isinstance(block, TaskBlock)
        assert block.error_code_mapping == {"BLOCK_ERROR": "only block"}
