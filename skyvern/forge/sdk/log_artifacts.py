import json
import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.skyvern_json_encoder import SkyvernJSONLogEncoder
from skyvern.forge.skyvern_log_encoder import SkyvernLogEncoder
from skyvern.forge.sdk.artifact.models import ArtifactType, LogEntityType

LOG = structlog.get_logger()

async def save_step_logs(step_id: str) -> None:
    log = skyvern_context.current().log
    organization_id = skyvern_context.current().organization_id

    current_step_log = [entry for entry in log if entry.get("step_id", "") == step_id]

    await _save_log_artifacts(
        log=current_step_log,
        log_entity_type=LogEntityType.STEP,
        log_entity_id=step_id,
        organization_id=organization_id,
    )


async def save_task_logs(task_id: str) -> None:
    log = skyvern_context.current().log
    organization_id = skyvern_context.current().organization_id

    current_task_log = [entry for entry in log if entry.get("task_id", "") == task_id]

    await _save_log_artifacts(
        log=current_task_log,
        log_entity_type=LogEntityType.TASK,
        log_entity_id=task_id,
        organization_id=organization_id,
    )


async def save_workflow_run_logs(workflow_run_id: str) -> None:
    log = skyvern_context.current().log
    organization_id = skyvern_context.current().organization_id

    current_workflow_run_log = [entry for entry in log if entry.get("workflow_run_id", "") == workflow_run_id]

    await _save_log_artifacts(
        log=current_workflow_run_log,
        log_entity_type=LogEntityType.WORKFLOW_RUN,
        log_entity_id=workflow_run_id,
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
    )


async def save_workflow_run_block_logs(workflow_run_block_id: str) -> None:
    log = skyvern_context.current().log
    organization_id = skyvern_context.current().organization_id
    current_workflow_run_block_log = [entry for entry in log if entry.get("workflow_run_block_id", "") == workflow_run_block_id]

    await _save_log_artifacts(
        log=current_workflow_run_block_log,
        log_entity_type=LogEntityType.WORKFLOW_RUN_BLOCK,
        log_entity_id=workflow_run_block_id,
        organization_id=organization_id,
        workflow_run_block_id=workflow_run_block_id,
    )


async def _save_log_artifacts(
    log: list[dict],
    log_entity_type: LogEntityType,
    log_entity_id: str,
    organization_id: str,
    step_id: str | None = None,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_run_block_id: str | None = None,
) -> None:
    try:
        log_json = json.dumps(log, cls=SkyvernJSONLogEncoder, indent=2)
        await app.ARTIFACT_MANAGER.create_log_artifact(
            organization_id=organization_id,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            log_entity_type=log_entity_type,
            log_entity_id=log_entity_id,
            artifact_type=ArtifactType.SKYVERN_LOG_RAW,
            data=log_json.encode(),
        )

        formatted_log = SkyvernLogEncoder.encode(log)
        await app.ARTIFACT_MANAGER.create_log_artifact(
            organization_id=organization_id,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            log_entity_type=log_entity_type,
            log_entity_id=log_entity_id,
            artifact_type=ArtifactType.SKYVERN_LOG,
            data=formatted_log.encode(),
        )
    except Exception as e:
        LOG.error(
            "Failed to save log artifacts",
            log_entity_type=log_entity_type,
            log_entity_id=log_entity_id,
            organization_id=organization_id,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            exc_info=True,
        )

