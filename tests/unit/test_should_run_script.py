"""Tests for WorkflowService.should_run_script() and script reviewer gating.

Verifies the priority chain:
  run-level run_with > workflow-level run_with > code_version fallback > default (agent).

run_with is always "agent" or "code" (never null). When run_with="agent" and
code_version >= 1, the code_version fallback still applies (workflow intended code mode).
"""

from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    is_adaptive_caching,
)


def _make_workflow(
    run_with: str = "agent",
    adaptive_caching: bool = False,
    code_version: int | None = None,
) -> Workflow:
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
        code_version=code_version,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_run(run_with: str = "agent") -> WorkflowRun:
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
    """Run-level run_with takes priority, then workflow-level, then code_version fallback, then agent."""

    def test_run_code_overrides_workflow_agent(self, service):
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with="code")
        assert service.should_run_script(wf, wr) is True

    def test_run_agent_overrides_workflow_code(self, service):
        wf = _make_workflow(run_with="code")
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_run_agent_overrides_code_version(self, service):
        """Explicit run-level agent overrides code_version fallback."""
        wf = _make_workflow(run_with="agent", code_version=2)
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_workflow_code_with_agent_run(self, service):
        """Workflow says code, run says agent → run wins."""
        wf = _make_workflow(run_with="code")
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_workflow_agent_with_agent_run(self, service):
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_workflow_agent_overrides_code_version(self, service):
        """Explicit workflow-level agent takes priority over code_version."""
        wf = _make_workflow(run_with="agent", code_version=2)
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_both_agent_defaults_to_agent(self, service):
        """When both workflow and run are agent, default to agent."""
        wf = _make_workflow(run_with="agent")
        wr = _make_run(run_with="agent")
        assert service.should_run_script(wf, wr) is False

    def test_code_version_with_agent_run_with(self, service):
        """code_version >= 1 with run_with=agent should still check code_version fallback."""
        wf = _make_workflow(run_with="agent", code_version=2)
        wr = _make_run(run_with="agent")
        # run_with=agent on both sides → agent wins, code_version ignored
        assert service.should_run_script(wf, wr) is False

    def test_code_version_with_code_run_with(self, service):
        """code_version >= 1 with explicit run_with=code should run code."""
        wf = _make_workflow(run_with="code", code_version=2)
        wr = _make_run(run_with="agent")
        # run-level agent overrides workflow code
        assert service.should_run_script(wf, wr) is False

    def test_workflow_code_run_code(self, service):
        """Both workflow and run say code → code."""
        wf = _make_workflow(run_with="code", code_version=2)
        wr = _make_run(run_with="code")
        assert service.should_run_script(wf, wr) is True

    def test_workflow_code_no_code_version(self, service):
        """Workflow says code, no code_version → should still run code."""
        wf = _make_workflow(run_with="code")
        wr = _make_run(run_with="code")
        assert service.should_run_script(wf, wr) is True

    def test_legacy_adaptive_caching_does_not_override_agent(self, service):
        """Legacy adaptive_caching=True does NOT override explicit run_with=agent."""
        wf = _make_workflow(run_with="agent", adaptive_caching=True)
        wr = _make_run(run_with="agent")
        # Explicit agent wins — adaptive_caching is a legacy fallback
        # that only applied when run_with was null (no longer possible).
        assert service.should_run_script(wf, wr) is False


class TestScriptReviewerGate:
    """The script reviewer should only fire when the script was actually executed.

    The gate requires BOTH is_adaptive_caching()=True AND should_run_script()=True.
    This prevents wasting LLM tokens reviewing scripts based on agent-only runs.
    """

    @pytest.mark.parametrize(
        "wf_run_with,run_run_with,code_version,expect_reviewer",
        [
            ("code", "code", 2, True),  # code + code_version=2 → adaptive caching on
            ("code", "code", 1, False),  # code + code_version=1 → adaptive caching off
            ("agent", "agent", 2, False),  # agent overrides everything
            ("agent", "agent", None, False),  # agent, no code_version
            ("code", "agent", 2, False),  # run-level agent overrides
            ("agent", "code", None, False),  # code without code_version → not adaptive
            ("agent", "code", 2, True),  # run-level code + code_version=2 → adaptive
        ],
    )
    def test_reviewer_gate(self, service, wf_run_with, run_run_with, code_version, expect_reviewer):
        wf = _make_workflow(run_with=wf_run_with, code_version=code_version)
        wr = _make_run(run_with=run_run_with)
        should_review = is_adaptive_caching(wf, wr) and service.should_run_script(wf, wr)
        assert should_review is expect_reviewer

    def test_legacy_adaptive_caching_backward_compat(self, service):
        """Legacy adaptive_caching=True with code_version=None still enables reviewer."""
        wf = _make_workflow(run_with="agent", adaptive_caching=True, code_version=None)
        wr = _make_run(run_with="code")
        should_review = is_adaptive_caching(wf, wr) and service.should_run_script(wf, wr)
        assert should_review is True
