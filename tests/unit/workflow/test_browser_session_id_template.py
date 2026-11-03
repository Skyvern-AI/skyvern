"""Unit tests for {{ browser_session_id }} template variable injection from WorkflowRunContext.

Tests both the low-level template rendering AND the full customer flow:
  1. WorkflowRunContext is created (like initialize_workflow_run_context)
  2. browser_session_id is set on the context (like execute_workflow does)
  3. Multiple block types render templates that reference {{ browser_session_id }}
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import HttpRequestBlock, NavigationBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeWorkflowRunContext:
    """Lightweight stand-in for tests that don't need the real WorkflowRunContext."""

    def __init__(
        self,
        *,
        values: dict | None = None,
        browser_session_id: str | None = None,
    ) -> None:
        self.values = dict(values or {})
        self.secrets = {}
        self.include_secrets_in_templates = False
        self.workflow_title = "wf-title"
        self.workflow_id = "wf-id"
        self.workflow_permanent_id = "wf-perm-id"
        self.workflow_run_id = "wr_test123"
        self.workflow_run_outputs = {}
        self.browser_session_id = browser_session_id

    def get_block_metadata(self, label: str) -> dict:
        return {}

    def build_workflow_run_summary(self) -> str:
        return ""


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        key="__output__",
        output_parameter_id="op_test",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_http_block() -> HttpRequestBlock:
    return HttpRequestBlock(
        label="test_http",
        url="https://api.example.com/v1/run/workflows",
        method="POST",
        output_parameter=_make_output_parameter(),
    )


def _make_navigation_block() -> NavigationBlock:
    return NavigationBlock(
        label="test_nav",
        url="https://example.com",
        navigation_goal="Navigate to the page",
        output_parameter=_make_output_parameter(),
    )


def _make_real_workflow_run_context(*, browser_session_id: str | None = None) -> WorkflowRunContext:
    """Create a real WorkflowRunContext (as execute_workflow would) with a mock AWS client."""
    ctx = WorkflowRunContext(
        workflow_title="E2E Test Workflow",
        workflow_id="wf_e2e_test",
        workflow_permanent_id="wpid_e2e_test",
        workflow_run_id="wr_e2e_test",
        aws_client=MagicMock(),
    )
    ctx.browser_session_id = browser_session_id
    return ctx


# ---------------------------------------------------------------------------
# 1. Basic template injection (FakeWorkflowRunContext)
# ---------------------------------------------------------------------------


class TestBrowserSessionIdTemplateInjection:
    """Verify {{ browser_session_id }} resolves from WorkflowRunContext."""

    def test_workflow_parameter_takes_precedence(self) -> None:
        """If a workflow parameter named browser_session_id exists, it should win over the context attribute."""
        block = _make_http_block()
        ctx = FakeWorkflowRunContext(values={"browser_session_id": "pbs_from_param"}, browser_session_id="pbs_from_ctx")
        result = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result == "pbs_from_param"


# ---------------------------------------------------------------------------
# 2. Full customer flow e2e (real WorkflowRunContext, multiple block types)
# ---------------------------------------------------------------------------


class TestCustomerFlowE2E:
    """Simulate the real customer flow end-to-end:
    1. Create browser profile (bp_...)
    2. Create browser session with that profile (pbs_...)
    3. Run workflow → execute_workflow sets browser_session_id on WorkflowRunContext
    4. Workflow blocks render {{ browser_session_id }} in their templates
    """

    def test_http_block_json_body_with_real_context(self) -> None:
        """Customer pattern: HTTP request block POSTs to /v1/run/workflows with
        browser_session_id in the JSON body."""
        ctx = _make_real_workflow_run_context(browser_session_id="pbs_customer_abc")
        block = _make_http_block()

        body_template = (
            '{"workflow_id": "wpid_abc", '
            '"browser_session_id": "{{ browser_session_id }}", '
            '"parameters": {"key": "value"}}'
        )
        result = block.format_block_parameter_template_from_workflow_run_context(body_template, ctx)

        assert '"browser_session_id": "pbs_customer_abc"' in result
        assert "{{ browser_session_id }}" not in result

    def test_navigation_block_url_with_real_context(self) -> None:
        """Navigation block that includes browser_session_id in the URL template."""
        ctx = _make_real_workflow_run_context(browser_session_id="pbs_nav_test")
        block = _make_navigation_block()

        url_template = "https://api.example.com/sessions/{{ browser_session_id }}/status"
        result = block.format_block_parameter_template_from_workflow_run_context(url_template, ctx)

        assert result == "https://api.example.com/sessions/pbs_nav_test/status"

    def test_multiple_template_variables_together(self) -> None:
        """Templates can use browser_session_id alongside other workflow-level variables."""
        ctx = _make_real_workflow_run_context(browser_session_id="pbs_multi_var")
        block = _make_http_block()

        template = (
            '{"workflow_run_id": "{{ workflow_run_id }}", '
            '"browser_session_id": "{{ browser_session_id }}", '
            '"workflow_id": "{{ workflow_id }}"}'
        )
        result = block.format_block_parameter_template_from_workflow_run_context(template, ctx)

        assert '"workflow_run_id": "wr_e2e_test"' in result
        assert '"browser_session_id": "pbs_multi_var"' in result
        assert '"workflow_id": "wf_e2e_test"' in result

    def test_no_session_renders_empty_string_in_json(self) -> None:
        """When no browser session is used, {{ browser_session_id }} renders as empty string."""
        ctx = _make_real_workflow_run_context(browser_session_id=None)
        block = _make_http_block()

        template = '{"browser_session_id": "{{ browser_session_id }}"}'
        result = block.format_block_parameter_template_from_workflow_run_context(template, ctx)

        assert result == '{"browser_session_id": ""}'
