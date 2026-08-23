"""start_fresh_browser run flag + tasks browser_profile_id parity (Browser Memory ticket C)."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.routes.agent_protocol import (
    _workflow_run_request_from_workflow_request,
    _workflow_run_request_to_legacy_request,
)
from skyvern.schemas.credential_type import CredentialType
from skyvern.schemas.run_blocks import DownloadFilesRequest, LoginRequest
from skyvern.schemas.runs import (
    BlockRunRequest,
    TaskRunRequest,
    WorkflowRunRequest,
    resolve_start_fresh,
    should_suppress_memory_write,
)


@pytest.mark.parametrize(("flag", "expected"), [(True, True), (False, False), (None, False)])
def test_should_suppress_memory_write_truth_table(flag: bool | None, expected: bool) -> None:
    # Exported contract consumed by the credential write-gate (SKY-12645).
    assert should_suppress_memory_write(flag) is expected


def test_start_fresh_browser_defaults_false_on_all_request_models() -> None:
    assert WorkflowRunRequest(agent_id="wpid_1").start_fresh_browser is False
    assert BlockRunRequest(agent_id="wpid_1", block_labels=["b"]).start_fresh_browser is False
    assert TaskRunRequest(prompt="x").start_fresh_browser is False
    assert LoginRequest(credential_type=CredentialType.skyvern).start_fresh_browser is False
    assert DownloadFilesRequest(navigation_goal="g").start_fresh_browser is False


def test_task_run_request_gains_browser_profile_id() -> None:
    assert TaskRunRequest(prompt="x").browser_profile_id is None
    assert TaskRunRequest(prompt="x", browser_profile_id="bp_1").browser_profile_id == "bp_1"


def test_start_fresh_browser_threads_to_legacy_request() -> None:
    legacy = _workflow_run_request_to_legacy_request(WorkflowRunRequest(agent_id="wpid_1", start_fresh_browser=True))
    assert legacy.start_fresh_browser is True


def test_workflow_run_request_from_body_echoes_start_fresh() -> None:
    # Naive-echo class (validated-request source): a WorkflowRequestBody can't hold a conflicting
    # session/profile, so the run_request response echo forwards start_fresh_browser directly.
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    result = _workflow_run_request_from_workflow_request(
        workflow_id="w_1", title="t", workflow_request=WorkflowRequestBody(start_fresh_browser=True)
    )
    assert result.start_fresh_browser is True


def test_override_wins_over_start_fresh_but_writes_stay_suppressed() -> None:
    # Explicit per-run browser_profile_id override beats the fresh flag (suppression is below the
    # run-override level); the run still suppresses its own-memory writes.
    assert resolve_start_fresh(start_fresh_browser=True, override_browser_profile_id="bp_x") is False
    assert resolve_start_fresh(start_fresh_browser=True, override_browser_profile_id=None) is True
    assert resolve_start_fresh(start_fresh_browser=False, override_browser_profile_id=None) is False
    assert should_suppress_memory_write(True) is True
