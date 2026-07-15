from datetime import UTC, datetime

from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotCompletionCriteriaSet


def test_completion_criteria_set_keeps_list_storage_shape() -> None:
    now = datetime.now(UTC)
    criteria = [{"id": "c0", "outcome": "done", "pinability": "pinned"}]

    stored = WorkflowCopilotCompletionCriteriaSet.model_validate(
        {
            "completion_criteria_set_id": "wccs_1",
            "organization_id": "o_1",
            "workflow_copilot_chat_id": "wcc_1",
            "goal_epoch": 1,
            "status": "active",
            "criteria": criteria,
            "created_at": now,
            "modified_at": now,
        }
    )

    assert stored.criteria == criteria
