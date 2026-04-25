"""Tests for workflow-level workflow_system_prompt inheritance into blocks at execution time."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    FileParserBlock,
    PDFParserBlock,
    TaskBlock,
    TaskV2Block,
    TextPromptBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.workflows import FileType


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


def _make_task_block(workflow_system_prompt: str | None = None) -> TaskBlock:
    return TaskBlock(
        label="task1",
        output_parameter=_make_output_parameter(),
        title="task title",
        workflow_system_prompt=workflow_system_prompt,
    )


def _make_task_v2_block(workflow_system_prompt: str | None = None) -> TaskV2Block:
    return TaskV2Block(
        label="task1",
        output_parameter=_make_output_parameter(),
        prompt="user goal",
        workflow_system_prompt=workflow_system_prompt,
    )


def _make_workflow(workflow_system_prompt: str | None) -> Workflow:
    workflow_definition = WorkflowDefinition(
        parameters=[],
        blocks=[],
        workflow_system_prompt=workflow_system_prompt,
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


def _make_workflow_run_context(
    workflow_system_prompt: str | None,
    inherited_workflow_system_prompt: str | None = None,
) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        workflow=_make_workflow(workflow_system_prompt),
        inherited_workflow_system_prompt=inherited_workflow_system_prompt,
    )
    return ctx


class TestTaskBlockSystemPromptInheritance:
    def test_block_inherits_workflow_prompt_when_none(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Never guess. If unsure, say UNKNOWN.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Never guess. If unsure, say UNKNOWN."

    def test_both_none_stays_none(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(workflow_system_prompt=None)

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_jinja_substitution_resolves_workflow_parameters(self) -> None:
        """Global system prompt should support Jinja substitution against workflow parameters."""
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Respond in the style of {{ style }}.")
        ctx.values["style"] = "a formal English butler"

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Respond in the style of a formal English butler."


class TestTaskV2BlockSystemPromptInheritance:
    def test_block_inherits_workflow_prompt_when_none(self) -> None:
        block = _make_task_v2_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Never guess.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Never guess."

    def test_both_none_stays_none(self) -> None:
        block = _make_task_v2_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(workflow_system_prompt=None)

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None


def _make_text_prompt_block(workflow_system_prompt: str | None = None) -> TextPromptBlock:
    return TextPromptBlock(
        label="prompt1",
        output_parameter=_make_output_parameter(),
        prompt="what is 2 + 2?",
        workflow_system_prompt=workflow_system_prompt,
    )


def _make_file_parser_block(workflow_system_prompt: str | None = None) -> FileParserBlock:
    return FileParserBlock(
        label="fileparser1",
        output_parameter=_make_output_parameter(),
        file_url="https://example.com/file.csv",
        file_type=FileType.CSV,
        workflow_system_prompt=workflow_system_prompt,
    )


def _make_pdf_parser_block(workflow_system_prompt: str | None = None) -> PDFParserBlock:
    return PDFParserBlock(
        label="pdfparser1",
        output_parameter=_make_output_parameter(),
        file_url="https://example.com/file.pdf",
        workflow_system_prompt=workflow_system_prompt,
    )


class TestTextPromptBlockSystemPromptInheritance:
    def test_block_inherits_workflow_prompt_when_none(self) -> None:
        block = _make_text_prompt_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Answer only in Spanish.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Answer only in Spanish."

    def test_both_none_stays_none(self) -> None:
        block = _make_text_prompt_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(workflow_system_prompt=None)

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_jinja_substitution_resolves_workflow_parameters(self) -> None:
        block = _make_text_prompt_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Respond in the style of {{ style }}.")
        ctx.values["style"] = "a pirate"

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Respond in the style of a pirate."


class TestFileParserBlockSystemPromptInheritance:
    def test_block_inherits_workflow_prompt_when_none(self) -> None:
        block = _make_file_parser_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Only respond with structured data.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Only respond with structured data."

    def test_both_none_stays_none(self) -> None:
        block = _make_file_parser_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(workflow_system_prompt=None)

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None


class TestPDFParserBlockSystemPromptInheritance:
    def test_block_inherits_workflow_prompt_when_none(self) -> None:
        block = _make_pdf_parser_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Summarize in English.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Summarize in English."

    def test_both_none_stays_none(self) -> None:
        block = _make_pdf_parser_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(workflow_system_prompt=None)

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None


class TestChildWorkflowInheritsParentWorkflowSystemPrompt:
    """SKY-9147: parent workflow_trigger workflow_system_prompt must flow into child blocks."""

    def test_child_inherits_parent_when_child_unset(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(
            workflow_system_prompt=None,
            inherited_workflow_system_prompt="Omit the word 'not'.",
        )

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Omit the word 'not'."

    def test_child_concatenates_parent_and_own_prompt(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context(
            workflow_system_prompt="Respond in French.",
            inherited_workflow_system_prompt="Omit the word 'not'.",
        )

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Omit the word 'not'.\n\nRespond in French."

    def test_ignore_flag_drops_both_inherited_and_own(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context(
            workflow_system_prompt="Respond in French.",
            inherited_workflow_system_prompt="Omit the word 'not'.",
        )

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_inherited_only_with_no_workflow_attached(self) -> None:
        """Child context may carry inherited rules even when workflow is not hydrated."""
        block = _make_text_prompt_block(workflow_system_prompt=None)
        ctx = WorkflowRunContext(
            workflow_title="child",
            workflow_id="w_child",
            workflow_permanent_id="wpid_child",
            workflow_run_id="wr_child",
            aws_client=MagicMock(),
            workflow=None,
            inherited_workflow_system_prompt="Be concise.",
        )

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Be concise."


class TestIgnoreWorkflowSystemPromptPerBlock:
    """SKY-9147: per-block opt-out short-circuits inheritance across every LLM-consuming block."""

    def test_task_block_opt_out_skips_workflow_prompt(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context("Be concise.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_task_v2_block_opt_out_skips_workflow_prompt(self) -> None:
        block = _make_task_v2_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context("Be concise.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_text_prompt_block_opt_out_skips_workflow_prompt(self) -> None:
        block = _make_text_prompt_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context("Answer only in Spanish.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_file_parser_block_opt_out_skips_workflow_prompt(self) -> None:
        block = _make_file_parser_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context("Only respond with structured data.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_pdf_parser_block_opt_out_skips_workflow_prompt(self) -> None:
        block = _make_pdf_parser_block(workflow_system_prompt=None)
        block.ignore_workflow_system_prompt = True
        ctx = _make_workflow_run_context("Summarize in English.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt is None

    def test_opt_out_default_false_preserves_inheritance(self) -> None:
        """Regression guard: omitting the field leaves default inheritance behavior intact."""
        block = _make_task_block(workflow_system_prompt=None)
        assert block.ignore_workflow_system_prompt is False
        ctx = _make_workflow_run_context("Be concise.")

        block.format_potential_template_parameters(ctx)

        assert block.workflow_system_prompt == "Be concise."


class TestWorkflowTriggerPersistsOptOutOnChildRun:
    """SKY-9147: the trigger-block flag is persisted on the spawned child's
    workflow_run row so both sync and async (Temporal-dispatched) child
    executions honor it uniformly when they read their own row.
    """

    def test_workflow_run_has_skip_inherited_field(self) -> None:
        """WorkflowRun Pydantic model carries the persisted flag."""
        now = datetime.now(timezone.utc)
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

        run = WorkflowRun(
            workflow_run_id="wr_child",
            workflow_id="w_child",
            workflow_permanent_id="wpid_child",
            organization_id="o_test",
            status=WorkflowRunStatus.created,
            created_at=now,
            modified_at=now,
            ignore_inherited_workflow_system_prompt=True,
        )

        assert run.ignore_inherited_workflow_system_prompt is True

    def test_workflow_run_defaults_false(self) -> None:
        """Existing code paths that omit the field continue to inherit."""
        now = datetime.now(timezone.utc)
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

        run = WorkflowRun(
            workflow_run_id="wr_child",
            workflow_id="w_child",
            workflow_permanent_id="wpid_child",
            organization_id="o_test",
            status=WorkflowRunStatus.created,
            created_at=now,
            modified_at=now,
        )

        assert run.ignore_inherited_workflow_system_prompt is False


class TestWorkflowDefinitionYAMLRoundTrip:
    def test_workflow_system_prompt_survives_yaml_roundtrip(self) -> None:
        """Regression guard: workflow_system_prompt must roundtrip through WorkflowCreateYAMLRequest."""
        from skyvern.schemas.workflows import WorkflowCreateYAMLRequest

        payload = {
            "title": "test",
            "workflow_definition": {
                "version": 1,
                "parameters": [],
                "blocks": [],
                "workflow_system_prompt": "Never guess.",
            },
        }
        request = WorkflowCreateYAMLRequest.model_validate(payload)
        assert request.workflow_definition.workflow_system_prompt == "Never guess."


class TestBlockWorkflowSystemPromptNotSerialized:
    """The runtime cache must not leak through ``model_dump`` or JSON
    serialization — it's a per-run transient, not part of the block's
    authored shape."""

    def test_unset_field_absent_from_model_dump(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        assert "workflow_system_prompt" not in block.model_dump()

    def test_resolved_runtime_value_absent_from_model_dump(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Never guess.")
        block.format_potential_template_parameters(ctx)

        # Invariant confirmed at runtime: inheritance populated the cache…
        assert block.workflow_system_prompt == "Never guess."
        # …but it still doesn't escape via serialization.
        dumped = block.model_dump()
        assert "workflow_system_prompt" not in dumped
        assert "Never guess." not in block.model_dump_json()


class TestBlockWorkflowSystemPromptRecordedOnContext:
    """``Block._apply_workflow_system_prompt`` records its decision on the
    ``WorkflowRunContext`` so both the agent path (which uses the block's
    own ``workflow_system_prompt`` field) and the script path (which reads
    from the context cache via ``ai_extract``) see the same value — single
    source of truth for the opt-out (SKY-9147)."""

    def test_non_opted_out_block_records_resolved_value(self) -> None:
        block = _make_task_block(workflow_system_prompt=None)
        ctx = _make_workflow_run_context("Answer only in Spanish.")

        block.format_potential_template_parameters(ctx)

        recorded, value = ctx.get_block_workflow_system_prompt(block.label)
        assert recorded is True
        assert value == "Answer only in Spanish."

    def test_opted_out_block_records_none(self) -> None:
        block = TaskBlock(
            label="task1",
            output_parameter=_make_output_parameter(),
            title="task title",
            ignore_workflow_system_prompt=True,
        )
        ctx = _make_workflow_run_context("WORKFLOW RULES.")

        block.format_potential_template_parameters(ctx)

        # Recorded explicitly so ``ai_extract`` reads ``None`` (opt-out) rather
        # than falling through to ``resolve_effective_workflow_system_prompt``.
        recorded, value = ctx.get_block_workflow_system_prompt(block.label)
        assert recorded is True
        assert value is None

    def test_unknown_label_returns_not_recorded(self) -> None:
        ctx = _make_workflow_run_context("whatever")
        recorded, value = ctx.get_block_workflow_system_prompt("never-seen")
        assert recorded is False
        assert value is None


class TestResolveEffectiveWorkflowSystemPromptRejectsNonString:
    """``resolve_effective_workflow_system_prompt`` must treat a non-string
    ``workflow_system_prompt`` as absent rather than passing it to Jinja.

    Regression: a malformed workflow definition (or a test fixture whose
    attribute access returns a ``MagicMock``) previously flowed a non-str
    into ``env.from_string`` and exploded with ``Can't compile non template
    nodes`` from deep inside the template compiler."""

    def test_magicmock_workflow_definition_yields_none(self) -> None:
        ctx = _make_workflow_run_context(workflow_system_prompt=None)
        mock_workflow = MagicMock()
        # Attribute access on a MagicMock returns another truthy MagicMock,
        # mirroring what ``get_workflow_by_permanent_id`` returns in tests
        # that don't set up a real Workflow.
        ctx.set_workflow(mock_workflow)

        assert ctx.resolve_effective_workflow_system_prompt() is None

    def test_non_string_inherited_prompt_yields_none(self) -> None:
        ctx = WorkflowRunContext(
            workflow_title="test",
            workflow_id="w_test",
            workflow_permanent_id="wpid_test",
            workflow_run_id="wr_test",
            aws_client=MagicMock(),
            inherited_workflow_system_prompt=MagicMock(),  # non-str
        )

        assert ctx.resolve_effective_workflow_system_prompt() is None
