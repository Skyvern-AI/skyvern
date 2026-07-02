from datetime import datetime

from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockType

# Constants
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"


def create_fake_for_ai_suggestion(ai_suggestion_id: str) -> AISuggestion:
    return AISuggestion(
        ai_suggestion_id=ai_suggestion_id,
        organization_id=TEST_ORGANIZATION_ID,
        ai_suggestion_type="test_suggestion_type",
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
    )


def create_fake_thought(cruise_id: str, thought_id: str) -> Thought:
    return Thought(
        observer_cruise_id=cruise_id,
        observer_thought_id=thought_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_step(step_id: str) -> Step:
    return Step(
        task_id=TEST_TASK_ID,
        order=1,
        retry_index=0,
        step_id=step_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        status=StepStatus.created,
        is_last=False,
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_task_v2(observer_cruise_id: str) -> TaskV2:
    return TaskV2(
        observer_cruise_id=observer_cruise_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        status=StepStatus.created,
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_workflow_run_block(workflow_run_id: str, workflow_run_block_id: str) -> WorkflowRunBlock:
    return WorkflowRunBlock(
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        organization_id=TEST_ORGANIZATION_ID,
        block_type=BlockType.TASK,
    )
