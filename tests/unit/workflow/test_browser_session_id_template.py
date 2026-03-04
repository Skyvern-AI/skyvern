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

    def test_resolves_from_workflow_run_context(self) -> None:
        block = _make_http_block()
        ctx = FakeWorkflowRunContext(browser_session_id="pbs_abc123")
        result = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result == "pbs_abc123"

    def test_renders_empty_when_no_session(self) -> None:
        block = _make_http_block()
        ctx = FakeWorkflowRunContext(browser_session_id=None)
        result = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result == ""

    def test_workflow_parameter_takes_precedence(self) -> None:
        """If a workflow parameter named browser_session_id exists, it should win over the context attribute."""
        block = _make_http_block()
        ctx = FakeWorkflowRunContext(values={"browser_session_id": "pbs_from_param"}, browser_session_id="pbs_from_ctx")
        result = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result == "pbs_from_param"

    def test_embedded_in_json_body(self) -> None:
        """Test the customer's actual pattern: browser_session_id embedded in a JSON body template."""
        block = _make_http_block()
        ctx = FakeWorkflowRunContext(browser_session_id="pbs_xyz789")
        template = '{"workflow_id": "wpid_test", "browser_session_id": "{{ browser_session_id }}"}'
        result = block.format_block_parameter_template_from_workflow_run_context(template, ctx)
        assert result == '{"workflow_id": "wpid_test", "browser_session_id": "pbs_xyz789"}'


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

    def test_context_attribute_matches_execute_workflow_assignment(self) -> None:
        """Verify that the WorkflowRunContext.browser_session_id attribute works
        exactly as execute_workflow assigns it (line 921-923 of service.py)."""
        ctx = WorkflowRunContext(
            workflow_title="Real Workflow",
            workflow_id="wf_real",
            workflow_permanent_id="wpid_real",
            workflow_run_id="wr_real",
            aws_client=MagicMock(),
        )
        # This mirrors: workflow_run_context.browser_session_id = browser_session_id
        ctx.browser_session_id = "pbs_from_execute_workflow"

        block = _make_http_block()
        result = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result == "pbs_from_execute_workflow"

    def test_auto_created_session_id_propagates(self) -> None:
        """Simulate auto-creation flow: browser_session_id starts None, then gets set
        after auto_create_browser_session_if_needed returns a session."""
        ctx = _make_real_workflow_run_context(browser_session_id=None)

        # Before auto-creation: should render empty
        block = _make_http_block()
        result_before = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result_before == ""

        # Simulate auto-creation setting the session ID (like service.py does)
        ctx.browser_session_id = "pbs_auto_created"

        result_after = block.format_block_parameter_template_from_workflow_run_context("{{ browser_session_id }}", ctx)
        assert result_after == "pbs_auto_created"


# ---------------------------------------------------------------------------
# 3. WorkflowContextManager → context injection → template rendering chain
# ---------------------------------------------------------------------------


class TestAutoCreateSessionContextInjection:
    """Simulate the full execute_workflow path when auto_create_browser_session_if_needed
    creates a session: WorkflowContextManager registers the context, execute_workflow
    mutates browser_session_id on it, then blocks render templates from the same context."""

    def test_context_manager_register_then_mutate_then_render(self) -> None:
        """Exercise the real WorkflowContextManager.get_workflow_run_context path
        that execute_workflow uses to inject browser_session_id."""
        from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager

        mgr = WorkflowContextManager()
        workflow_run_id = "wr_auto_test"

        # Step 1: Register context (like initialize_workflow_run_context does)
        ctx = WorkflowRunContext(
            workflow_title="Auto-Create Test",
            workflow_id="wf_auto",
            workflow_permanent_id="wpid_auto",
            workflow_run_id=workflow_run_id,
            aws_client=MagicMock(),
        )
        mgr.workflow_run_contexts[workflow_run_id] = ctx

        # Step 2: Simulate auto_create_browser_session_if_needed returning a session,
        # then execute_workflow setting browser_session_id via get_workflow_run_context
        auto_created_session_id = "pbs_auto_created_999"
        retrieved_ctx = mgr.get_workflow_run_context(workflow_run_id)
        retrieved_ctx.browser_session_id = auto_created_session_id

        # Step 3: Verify template rendering (as _execute_workflow_blocks would)
        block = _make_http_block()
        body_template = '{"browser_session_id": "{{ browser_session_id }}", "workflow_run_id": "{{ workflow_run_id }}"}'
        result = block.format_block_parameter_template_from_workflow_run_context(body_template, retrieved_ctx)

        assert f'"browser_session_id": "{auto_created_session_id}"' in result
        assert f'"workflow_run_id": "{workflow_run_id}"' in result

    def test_context_manager_without_auto_create(self) -> None:
        """When no auto-creation happens, browser_session_id stays None and renders empty."""
        from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager

        mgr = WorkflowContextManager()
        workflow_run_id = "wr_no_auto"

        ctx = WorkflowRunContext(
            workflow_title="No Auto-Create",
            workflow_id="wf_no_auto",
            workflow_permanent_id="wpid_no_auto",
            workflow_run_id=workflow_run_id,
            aws_client=MagicMock(),
        )
        mgr.workflow_run_contexts[workflow_run_id] = ctx

        # execute_workflow sets browser_session_id = None (no auto-creation)
        retrieved_ctx = mgr.get_workflow_run_context(workflow_run_id)
        retrieved_ctx.browser_session_id = None

        block = _make_http_block()
        result = block.format_block_parameter_template_from_workflow_run_context(
            "{{ browser_session_id }}", retrieved_ctx
        )
        assert result == ""
