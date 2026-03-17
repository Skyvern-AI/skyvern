"""Tests for WorkflowService.should_run_script().

Verifies the priority chain:
  run-level run_with > workflow-level run_with > default (agent).
adaptive_caching alone does NOT force code mode.
"""

from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus


def _make_workflow(run_with: str | None = None, adaptive_caching: bool = False) -> Workflow:
    return Workflow(
        workflow_id="wf_test",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        title="test",
        version=1,
        is_saved_task=False,
        workflow_definition={"parameters": [], "blocks": []},
        run_with=run_with,
        adaptive_caching=adaptive_caching,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_run(run_with: str | None = None) -> WorkflowRun:
    return WorkflowRun(
        workflow_run_id="wr_test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        status=WorkflowRunStatus.running,
        run_with=run_with,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def service():
    from skyvern.forge.sdk.workflow.service import WorkflowService

    return WorkflowService()


class TestShouldRunScript:
    """Run-level run_with takes priority, then workflow-level, then defaults to agent."""

    def test_run_code_overrides_workflow_agent(self, service):
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with="code")
        assert service.should_run_script(wf, wr) is True

    def test_run_code_v2_overrides_workflow_agent(self, service):
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with="code_v2")
        assert service.should_run_script(wf, wr) is True

    def test_run_agent_overrides_workflow_code(self, service):
        wf = _make_workflow(run_with="code")
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_workflow_code_with_null_run(self, service):
        wf = _make_workflow(run_with="code")
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True

    def test_workflow_agent_with_null_run(self, service):
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_both_null_defaults_to_agent(self, service):
        """When neither workflow nor run specifies run_with, default to agent (no code)."""
        wf = _make_workflow(run_with=None)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_adaptive_caching_alone_does_not_force_code(self, service):
        """adaptive_caching=True with run_with=null should NOT force code mode (SKY-8390)."""
        wf = _make_workflow(run_with=None, adaptive_caching=True)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_adaptive_caching_with_workflow_agent_does_not_force_code(self, service):
        """adaptive_caching=True with explicit run_with=agent should NOT force code."""
        wf = _make_workflow(run_with="agent", adaptive_caching=True)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_adaptive_caching_with_workflow_code_runs_code(self, service):
        """adaptive_caching=True with run_with=code should run code (v1→v2 upgrade is separate)."""
        wf = _make_workflow(run_with="code", adaptive_caching=True)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True

    def test_workflow_code_v2_with_null_run(self, service):
        """workflow.run_with='code_v2' should be treated the same as 'code'."""
        wf = _make_workflow(run_with="code_v2")
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True
