import json

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType, LogEntityType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.skyvern_json_encoder import SkyvernJSONLogEncoder
from skyvern.forge.skyvern_log_encoder import SkyvernLogEncoder

LOG = structlog.get_logger()


def primary_key_from_log_entity_type(log_entity_type: LogEntityType) -> str:
    if log_entity_type == LogEntityType.STEP:
        return "step_id"
    elif log_entity_type == LogEntityType.TASK:
        return "task_id"
    elif log_entity_type == LogEntityType.WORKFLOW_RUN:
        return "workflow_run_id"
    elif log_entity_type == LogEntityType.WORKFLOW_RUN_BLOCK:
        return "workflow_run_block_id"
    elif log_entity_type == LogEntityType.TASK_V2:
        return "task_v2_id"
    else:
        raise ValueError(f"Invalid log entity type: {log_entity_type}")


async def save_step_logs(step_id: str) -> None:
    if not settings.ENABLE_LOG_ARTIFACTS:
        return

    context = skyvern_context.ensure_context()
    log = context.log
    organization_id = context.organization_id

    current_step_log = [entry for entry in log if entry.get("step_id", "") == step_id]

    await _save_log_artifacts(
        log=current_step_log,
        log_entity_type=LogEntityType.STEP,
        log_entity_id=step_id,
        organization_id=organization_id,
        step_id=step_id,
    )


async def save_task_logs(task_id: str) -> None:
    if not settings.ENABLE_LOG_ARTIFACTS:
        return

    context = skyvern_context.ensure_context()
    log = context.log
    organization_id = context.organization_id

    current_task_log = [entry for entry in log if entry.get("task_id", "") == task_id]

    await _save_log_artifacts(
        log=current_task_log,
        log_entity_type=LogEntityType.TASK,
        log_entity_id=task_id,
        organization_id=organization_id,
        task_id=task_id,
    )


async def save_workflow_run_logs(workflow_run_id: str) -> None:
    if not settings.ENABLE_LOG_ARTIFACTS:
        return

    context = skyvern_context.ensure_context()
    log = context.log
    organization_id = context.organization_id

    current_workflow_run_log = [entry for entry in log if entry.get("workflow_run_id", "") == workflow_run_id]

    await _save_log_artifacts(
        log=current_workflow_run_log,
        log_entity_type=LogEntityType.WORKFLOW_RUN,
        log_entity_id=workflow_run_id,
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
    )


async def save_workflow_run_block_logs(workflow_run_block_id: str) -> None:
    if not settings.ENABLE_LOG_ARTIFACTS:
        return

    context = skyvern_context.ensure_context()
    log = context.log
    organization_id = context.organization_id
    current_workflow_run_block_log = [
        entry for entry in log if entry.get("workflow_run_block_id", "") == workflow_run_block_id
    ]

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
    organization_id: str | None,
    step_id: str | None = None,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_run_block_id: str | None = None,
) -> None:
    try:
        if not settings.ENABLE_LOG_ARTIFACTS:
            return

        log_json = json.dumps(log, cls=SkyvernJSONLogEncoder, indent=2)

        log_artifact = await app.DATABASE.get_artifact_by_entity_id(
            artifact_type=ArtifactType.SKYVERN_LOG_RAW,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

        if log_artifact:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=log_artifact.artifact_id,
                organization_id=organization_id,
                data=log_json.encode(),
                primary_key=primary_key_from_log_entity_type(log_entity_type),
            )
        else:
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

        formatted_log_artifact = await app.DATABASE.get_artifact_by_entity_id(
            artifact_type=ArtifactType.SKYVERN_LOG,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

        if formatted_log_artifact:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=formatted_log_artifact.artifact_id,
                organization_id=organization_id,
                data=formatted_log.encode(),
                primary_key=primary_key_from_log_entity_type(log_entity_type),
            )
        else:
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
    except Exception:
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
