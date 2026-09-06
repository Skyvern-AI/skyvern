"""Tests for block-level error_code_mapping: workflow inheritance, and redaction on the block's own
task-failure handler (SKY-15643).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import ERROR_CODE_REASONING_MAX_LENGTH
from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, Block, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.workflows import BlockStatus
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER


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


class TestBlockTaskFailureRedaction:
    """The block's own failure handler persists failure_reason and the detector's reasoning to the
    task, and both leave over the customer webhook. It is a third caller of the same detector, and
    it wraps execute_step for every engine (SKY-15643)."""

    @staticmethod
    def _patch_secrets(
        monkeypatch: pytest.MonkeyPatch, secrets: set[str], gate: bool = True, seen: list | None = None
    ) -> None:
        # `seen` captures the identifier the lookups are keyed on. Without it a regression that
        # passed task_id, or a typo'd attribute, would look up the wrong run's secrets, redact
        # nothing in production, and leave every assertion below green.
        def _record(value: object) -> None:
            if seen is not None:
                seen.append(value)

        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled",
            lambda run_id, *_a, **_k: (_record(run_id), gate)[1],
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
            lambda run_id, *_a, **_k: (_record(run_id), secrets)[1],
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
            lambda *_a, **_k: set(),
        )

    @pytest.mark.asyncio
    async def test_failure_reason_is_redacted_before_it_is_persisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block = _make_task_block(error_code_mapping={"payment_failed": "Payment was declined"})
        seen: list = []
        self._patch_secrets(monkeypatch, {"sk4829137765"}, seen=seen)
        update_task = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app.DATABASE.tasks.update_task", update_task)
        detector = AsyncMock(return_value=[])
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.detect_user_defined_errors_for_task", detector)
        task = MagicMock(task_id="t_1", workflow_run_id="wr_test")

        await block._handle_task_failure_with_error_detection(
            task=task,
            step=MagicMock(step_id="s_1"),
            browser_state=None,
            failure_reason="the portal rejected the key sk4829137765",
            organization_id="o_test",
        )

        persisted = update_task.await_args_list[0].kwargs["failure_reason"]
        assert "sk4829137765" not in persisted
        assert REDACTED_SECRET_PLACEHOLDER in persisted
        # Redacted at the top, so the detector is handed the scrubbed string too.
        assert "sk4829137765" not in detector.await_args.kwargs["failure_reason"]
        # ...and the secrets were looked up for the RUN, not some other identifier on the task.
        assert set(seen) == {"wr_test"}

    @pytest.mark.asyncio
    async def test_detector_written_reasoning_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The detector reads the page as well as the failure reason, so scrubbing its input is not
        # enough: a secret typed into the form can come back in its reasoning.
        block = _make_task_block(error_code_mapping={"payment_failed": "Payment was declined"})
        self._patch_secrets(monkeypatch, {"sk4829137765"})
        update_task = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app.DATABASE.tasks.update_task", update_task)
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.detect_user_defined_errors_for_task",
            AsyncMock(
                return_value=[
                    UserDefinedError(
                        error_code="payment_failed",
                        reasoning="the page showed sk4829137765 after submit",
                        confidence_float=1.0,
                    )
                ]
            ),
        )
        task = MagicMock(task_id="t_1", workflow_run_id="wr_test")

        await block._handle_task_failure_with_error_detection(
            task=task,
            step=MagicMock(step_id="s_1"),
            browser_state=None,
            failure_reason="could not continue",
            organization_id="o_test",
        )

        (persisted,) = update_task.await_args_list[-1].kwargs["errors"]
        assert "sk4829137765" not in persisted["reasoning"]
        assert REDACTED_SECRET_PLACEHOLDER in persisted["reasoning"]

    @pytest.mark.asyncio
    async def test_gate_off_leaves_a_workflow_secret_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A workflow-registered secret is available but the per-workflow opt-in is off, so it must
        # not reach the scrub. Engine-minted runtime values still would, via the fallback.
        block = _make_task_block(error_code_mapping=None)
        self._patch_secrets(monkeypatch, {"sk4829137765"}, gate=False)
        update_task = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app.DATABASE.tasks.update_task", update_task)
        task = MagicMock(task_id="t_1", workflow_run_id="wr_test")
        raw = "the portal rejected the key sk4829137765"

        await block._handle_task_failure_with_error_detection(
            task=task,
            step=MagicMock(step_id="s_1"),
            browser_state=None,
            failure_reason=raw,
            organization_id="o_test",
        )

        assert update_task.await_args_list[0].kwargs["failure_reason"] == raw

    @pytest.mark.asyncio
    async def test_redacting_reasoning_cannot_push_it_past_the_model_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redaction LENGTHENS: the placeholder is longer than a short code. model_copy skips the
        # field validator, so a reasoning already at the bound would land over it, and nothing
        # re-validates tasks.errors on the way out.
        block = _make_task_block(error_code_mapping={"payment_failed": "Payment was declined"})
        self._patch_secrets(monkeypatch, {"482913"})
        update_task = AsyncMock()
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app.DATABASE.tasks.update_task", update_task)
        # Short numeric secrets are boundary-anchored, so the code needs a non-alphanumeric neighbour.
        at_the_bound = "482913 " + "z" * (ERROR_CODE_REASONING_MAX_LENGTH - 7)
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.detect_user_defined_errors_for_task",
            AsyncMock(
                return_value=[
                    UserDefinedError(error_code="payment_failed", reasoning=at_the_bound, confidence_float=1.0)
                ]
            ),
        )

        await block._handle_task_failure_with_error_detection(
            task=MagicMock(task_id="t_1", workflow_run_id="wr_test"),
            step=MagicMock(step_id="s_1"),
            browser_state=None,
            failure_reason="could not continue",
            organization_id="o_test",
        )

        (persisted,) = update_task.await_args_list[-1].kwargs["errors"]
        assert "482913" not in persisted["reasoning"]
        assert len(persisted["reasoning"]) == ERROR_CODE_REASONING_MAX_LENGTH

    @pytest.mark.asyncio
    async def test_block_level_failure_reason_is_redacted_too(self) -> None:
        # The helper above scrubs the TASK row, but the block's own failure_reason is a separate
        # string that is persisted to workflow_run_blocks and lifted onto the run, neither redacted
        # downstream. The scrub lives in build_block_result rather than the except arm, because a
        # block that reports failure by RETURNING an unsuccessful result never raises -- so the
        # except arm is not the convergence point, and asserting on the returned BlockResult is what
        # covers both arms.
        block = _make_task_block(error_code_mapping=None)

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(
                BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=RuntimeError("token sk4829137765 bad")
            ),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(return_value=MagicMock())
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = None
            mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled = lambda *_a, **_k: True
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run = lambda *_a, **_k: {"sk4829137765"}

            result = await block.execute_safe(workflow_run_id="wr_test", current_index=None)

        assert "sk4829137765" not in (result.failure_reason or "")
        assert REDACTED_SECRET_PLACEHOLDER in (result.failure_reason or "")

    @pytest.mark.asyncio
    async def test_a_failed_secret_lookup_does_not_break_the_block_result(self) -> None:
        # build_block_result runs for EVERY block result, including on paths that never start a
        # ForgeApp. A redaction lookup must never be what turns a block result into a failure, so it
        # degrades to the unredacted reason -- which is exactly the pre-existing behaviour.
        block = _make_task_block(error_code_mapping=None)

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.side_effect = RuntimeError(
                "ForgeApp is not initialized"
            )

            result = await block.build_block_result(
                success=False,
                failure_reason="the vendor rejected the request",
                status=BlockStatus.failed,
                workflow_run_block_id="wrb_1",
                organization_id="o_test",
            )

        assert result.failure_reason == "the vendor rejected the request"
        assert result.success is False

    @pytest.mark.asyncio
    async def test_a_returned_failure_reason_is_redacted_not_only_a_raised_one(self) -> None:
        # The arm the except branch never sees: a block that reports failure by RETURNING an
        # unsuccessful result. Scrubbing only the raise path would leave this one raw.
        block = _make_task_block(error_code_mapping=None)

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled = lambda *_a, **_k: True
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run = lambda *_a, **_k: {"sk4829137765"}

            result = await block.build_block_result(
                success=False,
                failure_reason="the vendor rejected token sk4829137765",
                status=BlockStatus.failed,
                workflow_run_block_id="wrb_1",
                organization_id="o_test",
            )

        assert "sk4829137765" not in (result.failure_reason or "")
        assert REDACTED_SECRET_PLACEHOLDER in (result.failure_reason or "")
