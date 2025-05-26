from enum import StrEnum


class EntityType(StrEnum):
    STEP = "step"
    TASK = "task"
    WORKFLOW_RUN = "workflow_run"
    WORKFLOW_RUN_BLOCK = "workflow_run_block"
    THOUGHT = "thought"


entity_type_to_param = {
    EntityType.STEP: "step_id",
    EntityType.TASK: "task_id",
    EntityType.WORKFLOW_RUN: "workflow_run_id",
    EntityType.WORKFLOW_RUN_BLOCK: "workflow_run_block_id",
    EntityType.THOUGHT: "thought_id",
}
