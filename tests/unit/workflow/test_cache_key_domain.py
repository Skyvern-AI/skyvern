"""Tests for automatic domain-based cache key enrichment."""

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import BlockType, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.services.workflow_script_service import _extract_first_block_domain, _jinja_domain_filter


def _output_param(key: str = "out") -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="",
        output_parameter_id="op_test",
        workflow_id="w_test",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _task_block(label: str, url: str | None = None) -> TaskBlock:
    return TaskBlock(
        label=label,
        block_type=BlockType.TASK,
        output_parameter=_output_param(f"{label}_out"),
        url=url,
        title="Test",
        navigation_goal="Do something",
    )


def _workflow(blocks: list, cache_key: str = "default") -> Workflow:
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="Test Workflow",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(blocks=blocks, parameters=[]),
        cache_key=cache_key,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


class TestJinjaDomainFilter:
    def test_extracts_domain_from_url(self) -> None:
        assert _jinja_domain_filter("https://www.fanr.gov.ae/en/Documents") == "www.fanr.gov.ae"

    def test_extracts_domain_with_port(self) -> None:
        assert _jinja_domain_filter("https://example.com:8080/path") == "example.com:8080"

    def test_returns_input_for_non_url(self) -> None:
        assert _jinja_domain_filter("not-a-url") == "not-a-url"

    def test_returns_input_for_empty_string(self) -> None:
        assert _jinja_domain_filter("") == ""


class TestExtractFirstBlockDomain:
    def test_extracts_domain_from_first_block_with_url(self) -> None:
        blocks = [_task_block("step1", url="https://www.fanr.gov.ae/documents")]
        wf = _workflow(blocks)
        assert _extract_first_block_domain(wf, {}) == "www.fanr.gov.ae"

    def test_skips_blocks_without_url(self) -> None:
        blocks = [
            _task_block("step1", url=None),
            _task_block("step2", url="https://search.gov.hk/results"),
        ]
        wf = _workflow(blocks)
        assert _extract_first_block_domain(wf, {}) == "search.gov.hk"

    def test_renders_jinja_template_url(self) -> None:
        blocks = [_task_block("step1", url="{{ target_url }}")]
        wf = _workflow(blocks)
        params = {"target_url": "https://www.irs.gov/apply-ein"}
        assert _extract_first_block_domain(wf, params) == "www.irs.gov"

    def test_returns_empty_when_no_blocks_have_url(self) -> None:
        blocks = [_task_block("step1", url=None)]
        wf = _workflow(blocks)
        assert _extract_first_block_domain(wf, {}) == ""

    def test_returns_empty_for_empty_blocks(self) -> None:
        wf = _workflow([])
        assert _extract_first_block_domain(wf, {}) == ""

    def test_handles_unresolvable_template_gracefully(self) -> None:
        blocks = [_task_block("step1", url="{{ missing_param }}")]
        wf = _workflow(blocks)
        # Jinja renders undefined variables as empty string in SandboxedEnvironment
        assert _extract_first_block_domain(wf, {}) == ""
