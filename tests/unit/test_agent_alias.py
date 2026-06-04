"""Backwards-compatible `agent_id` alias for the workflows API.

An agent *is* a workflow, so `agent_id` is accepted anywhere `workflow_id`/`workflow_permanent_id`
is, carrying the same `wpid_` value, and is echoed back in responses alongside the original fields.
"""

from datetime import UTC, datetime

from skyvern.forge.sdk.workflow.models.workflow import (
    RunWorkflowResponse,
    Workflow,
    WorkflowDefinition,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import RunType, WorkflowRunRequest, WorkflowRunResponse


def test_workflow_run_request_accepts_agent_id_and_workflow_id() -> None:
    by_alias = WorkflowRunRequest.model_validate({"agent_id": "wpid_abc"})
    by_name = WorkflowRunRequest.model_validate({"workflow_id": "wpid_abc"})
    assert by_alias.workflow_id == by_name.workflow_id == "wpid_abc"
    # constructing by python field name keeps working too
    assert WorkflowRunRequest(workflow_id="wpid_abc").workflow_id == "wpid_abc"


def test_workflow_run_request_output_keeps_workflow_id_only() -> None:
    # agent_id is an *input* alias; the serialized request body stays on the canonical workflow_id
    # field and must not start emitting an agent_id key (which would break existing consumers).
    dumped = WorkflowRunRequest(agent_id="wpid_abc").model_dump()
    assert dumped["workflow_id"] == "wpid_abc"
    assert "agent_id" not in dumped


def test_workflow_run_response_omits_unreliable_agent_id() -> None:
    # The v2 run response intentionally has no top-level agent_id: it has no reliable permanent-id
    # source, so a computed alias would echo null (no run_request) or a version id (login/download).
    # The reliable alias lives on WorkflowRunResponseBase / Workflow / RunWorkflowResponse instead.
    response = WorkflowRunResponse(
        run_id="wr_1",
        run_type=RunType.workflow_run,
        status="completed",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        run_request=WorkflowRunRequest(workflow_id="wpid_abc"),
    )
    assert "agent_id" not in response.model_dump()


def test_run_workflow_response_echoes_agent_aliases() -> None:
    dumped = RunWorkflowResponse(workflow_id="wpid_abc", workflow_run_id="wr_1").model_dump()
    assert dumped["agent_id"] == "wpid_abc"
    assert dumped["agent_run_id"] == "wr_1"
    # originals preserved for backwards compatibility
    assert dumped["workflow_id"] == "wpid_abc"
    assert dumped["workflow_run_id"] == "wr_1"


def test_workflow_run_response_base_echoes_agent_aliases() -> None:
    dumped = WorkflowRunResponseBase(
        workflow_id="wpid_abc",
        workflow_run_id="wr_1",
        status=WorkflowRunStatus.completed,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        parameters={},
    ).model_dump()
    assert dumped["agent_id"] == "wpid_abc"
    assert dumped["agent_run_id"] == "wr_1"


def test_workflow_object_echoes_agent_id() -> None:
    workflow = Workflow(
        workflow_id="w_1",
        organization_id="o_1",
        title="example",
        workflow_permanent_id="wpid_abc",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    assert workflow.agent_id == "wpid_abc"
    assert workflow.model_dump()["agent_id"] == "wpid_abc"
