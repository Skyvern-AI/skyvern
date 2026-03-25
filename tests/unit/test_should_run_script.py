"""Tests for WorkflowService.should_run_script() and script reviewer gating.

Verifies the priority chain:
  run-level run_with > workflow-level run_with > code_version fallback > default (agent).
When code_version >= 1 and no explicit run_with is set, the run defaults to code mode.

Also verifies that the script reviewer only fires when the script was actually
executed (should_run_script=True), not merely when adaptive caching is enabled.
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
    run_with: str | None = None,
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
        wf = _make_workflow(run_with=None, code_version=2)
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

    def test_workflow_agent_overrides_code_version(self, service):
        """Explicit workflow-level agent takes priority over code_version."""
        wf = _make_workflow(run_with="agent", code_version=2)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_both_null_defaults_to_agent(self, service):
        """When neither workflow nor run specifies run_with and no code_version, default to agent."""
        wf = _make_workflow(run_with=None)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is False

    def test_code_version_defaults_to_code(self, service):
        """code_version >= 1 with run_with=null should default to code mode."""
        wf = _make_workflow(run_with=None, code_version=2)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True

    def test_code_version_1_defaults_to_code(self, service):
        """code_version=1 with run_with=null should also run code."""
        wf = _make_workflow(run_with=None, code_version=1)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True

    def test_code_version_with_workflow_code_runs_code(self, service):
        """code_version=2 with run_with=code should run code."""
        wf = _make_workflow(run_with="code", code_version=2)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True

    def test_legacy_adaptive_caching_fallback(self, service):
        """Legacy adaptive_caching=True with no code_version should still default to code."""
        wf = _make_workflow(run_with=None, adaptive_caching=True)
        wr = _make_run(run_with=None)
        assert service.should_run_script(wf, wr) is True


class TestScriptReviewerGate:
    """The script reviewer should only fire when the script was actually executed.

    The gate requires BOTH is_adaptive_caching()=True AND should_run_script()=True.
    This prevents wasting LLM tokens reviewing scripts based on agent-only runs.
    """

    @pytest.mark.parametrize(
        "run_with,code_version,expect_reviewer",
        [
            ("code", 2, True),  # code + code_version=2 → adaptive caching on
            ("code", 1, False),  # code + code_version=1 → adaptive caching off
            (None, 2, True),  # code_version=2 defaults to code mode, adaptive
            (None, None, False),  # no code_version, no run_with → agent
            ("agent", 2, False),  # agent overrides everything
            ("agent", None, False),
            ("code", None, False),  # code without code_version → not adaptive
        ],
    )
    def test_reviewer_gate(self, service, run_with, code_version, expect_reviewer):
        wf = _make_workflow(run_with=None, code_version=code_version)
        wr = _make_run(run_with=run_with)
        should_review = is_adaptive_caching(wf, wr) and service.should_run_script(wf, wr)
        assert should_review is expect_reviewer

    def test_legacy_adaptive_caching_backward_compat(self, service):
        """Legacy adaptive_caching=True with code_version=None still enables reviewer."""
        wf = _make_workflow(run_with=None, adaptive_caching=True, code_version=None)
        wr = _make_run(run_with="code")
        should_review = is_adaptive_caching(wf, wr) and service.should_run_script(wf, wr)
        assert should_review is True
