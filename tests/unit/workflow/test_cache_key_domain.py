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

    def test_resolves_parameter_key_reference(self) -> None:
        """url field holding a bare parameter key resolves via the parameters dict.

        Mirrors BaseTaskBlock.execute() which replaces self.url with the parameter
        value when self.url matches a parameter key. Without this resolution the
        cache key at run start renders to the literal parameter name (e.g.
        ``default:website_url:v2``) and never matches any persisted script, while
        mid-run gen — after block execution mutates self.url — renders the real
        domain. Scripts get persisted under the correct key but are never loaded
        at run start.
        """
        blocks = [_task_block("step1", url="website_url")]
        wf = _workflow(blocks)
        params = {"website_url": "https://secure.example.com/login"}
        assert _extract_first_block_domain(wf, params) == "secure.example.com"

    def test_parameter_key_reference_takes_precedence_over_jinja(self) -> None:
        """When url is a bare key that matches a parameter, use the parameter value,
        not Jinja rendering of the literal string."""
        blocks = [_task_block("step1", url="target_site")]
        wf = _workflow(blocks)
        params = {"target_site": "https://portal.example.org/dashboard"}
        # Without the fix this would return "target_site" (literal jinja render).
        assert _extract_first_block_domain(wf, params) == "portal.example.org"

    def test_bare_key_without_matching_parameter_falls_back_to_jinja(self) -> None:
        """If url looks like a bare key but no parameter matches, Jinja renders
        the literal (``unknown_key``), prepend_scheme_and_validate_url yields
        ``https://unknown_key``, and the domain filter returns ``unknown_key``.
        The broken workflow produces a meaningless cache segment, but that's
        consistent with what runtime would produce via
        format_block_parameter_template_from_workflow_run_context."""
        blocks = [_task_block("step1", url="unknown_key")]
        wf = _workflow(blocks)
        assert _extract_first_block_domain(wf, {}) == "unknown_key"

    def test_parameter_key_with_falsy_value_falls_back_to_jinja(self) -> None:
        """Runtime's truthy-value guard (block.py:931 ``if task_url_parameter_value:``)
        leaves self.url unchanged when the parameter value is None/empty. Mirror
        that here — the falsy value causes a fall-through to the literal key
        (``website_url``). Without the guard, ``str(None)`` would yield the
        string ``None`` and cache-key segment ``default:None:v2``, which
        diverges from runtime's behavior of keeping the literal."""
        blocks = [_task_block("step1", url="website_url")]
        wf = _workflow(blocks)
        assert _extract_first_block_domain(wf, {"website_url": None}) == "website_url"
        assert _extract_first_block_domain(wf, {"website_url": ""}) == "website_url"

    def test_parameter_value_containing_jinja_is_rendered_too(self) -> None:
        """Runtime renders Jinja on the substituted value (format_potential_template_parameters
        at block.py:753). The fix mirrors this with a post-substitution Jinja render so
        parameter values that themselves contain template expressions are fully expanded."""
        blocks = [_task_block("step1", url="website_url")]
        wf = _workflow(blocks)
        params = {
            "website_url": "{{ base_url }}/login",
            "base_url": "https://nested.example.com",
        }
        assert _extract_first_block_domain(wf, params) == "nested.example.com"

    def test_scheme_less_parameter_value_is_normalized(self) -> None:
        """Runtime applies prepend_scheme_and_validate_url at block.py:754 so
        scheme-less URLs get an ``https://`` prepended before navigation. Lock
        the normalization branch against regression."""
        blocks = [_task_block("step1", url="website_url")]
        wf = _workflow(blocks)
        params = {"website_url": "example.com/login"}
        assert _extract_first_block_domain(wf, params) == "example.com"
