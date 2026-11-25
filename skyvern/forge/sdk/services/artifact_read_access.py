import re
from collections.abc import Iterable

import structlog
from fastapi import HTTPException
from fastapi import status as http_status

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.services.browser_session_artifacts import (
    browser_session_artifact_access,
    browser_session_artifact_read,
    child_run_artifact_access,
)
from skyvern.forge.sdk.services.request_principal import RequestPrincipal
from skyvern.forge.sdk.services.route_authorization import EMBER_READ_ACTION

LOG = structlog.get_logger()

_EMBER_CHAT_LOG_PATH = re.compile(rf"/logs/{LogEntityType.EMBER_CHAT}/([^/]+)/")
DESCENDANT_CHECKED_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.DOWNLOAD,
        ArtifactType.PDF,
        ArtifactType.RECORDING,
        ArtifactType.SESSION_REPLAY,
        ArtifactType.HAR,
        ArtifactType.BROWSER_CONSOLE_LOG,
        ArtifactType.SKYVERN_LOG,
        ArtifactType.SKYVERN_LOG_RAW,
        ArtifactType.TRACE,
        ArtifactType.STEP_ARCHIVE,
        ArtifactType.TASK_ARCHIVE,
        ArtifactType.SCREENSHOT,
        ArtifactType.SCREENSHOT_LLM,
        ArtifactType.SCREENSHOT_ACTION,
        ArtifactType.SCREENSHOT_FINAL,
        ArtifactType.SCREENSHOT_PROXY,
        ArtifactType.SCREENSHOT_PRE_SUBMIT,
        ArtifactType.HTML,
        ArtifactType.HTML_SCRAPE,
        ArtifactType.HTML_ACTION,
        ArtifactType.HTML_PRE_SUBMIT,
        ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP,
        ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP,
        ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP,
        ArtifactType.VISIBLE_ELEMENTS_TREE,
        ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED,
        ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT,
        ArtifactType.HASHED_HREF_MAP,
        ArtifactType.LLM_PROMPT,
        ArtifactType.LLM_REQUEST,
        ArtifactType.LLM_RESPONSE,
        ArtifactType.LLM_RESPONSE_PARSED,
        ArtifactType.LLM_RESPONSE_RENDERED,
    }
)


def artifact_access_key(artifact: Artifact) -> tuple[str | None, ...]:
    """Artifacts with the same key get the same read decision."""
    key: tuple[str | None, ...] = (
        artifact.workflow_run_id,
        artifact.task_id,
        artifact.run_id,
        artifact.workflow_run_block_id,
        artifact.observer_cruise_id,
        artifact.browser_session_id,
        "descendants" if artifact.artifact_type in DESCENDANT_CHECKED_ARTIFACT_TYPES else None,
    )
    if not any(key[:-1]):
        key = (*key, artifact.artifact_id)
    return key


async def authorize_artifact_read(
    artifact: Artifact,
    principal: RequestPrincipal | None,
    checked_access: set[tuple[str, str, str]] | None = None,
    *,
    check_folder: bool = True,
) -> None:
    organization_id = artifact.organization_id
    if checked_access is None:
        checked_access = set()
    if await app.AGENT_FUNCTION.should_check_browser_session_artifacts(principal, organization_id):
        session_ids = {artifact.browser_session_id} if artifact.browser_session_id else set()
        run_ids = {artifact.workflow_run_id} if artifact.workflow_run_id else set()
        standalone_ids: set[str] = set()
        try:
            task_ids = {artifact.task_id} if artifact.task_id else set()
            task_v2_ids = {artifact.observer_cruise_id} if artifact.observer_cruise_id else set()
            if artifact.run_id and artifact.run_id not in run_ids:
                run = await app.DATABASE.workflow_runs.get_workflow_run(
                    artifact.run_id, organization_id=organization_id
                )
                if run:
                    run_ids.add(run.workflow_run_id)
                else:
                    task = await app.DATABASE.tasks.get_task(artifact.run_id, organization_id=organization_id)
                    if task:
                        task_ids.add(task.task_id)
                    else:
                        task_v2_ids.add(artifact.run_id)
            if artifact.workflow_run_block_id and not run_ids:
                block = await app.DATABASE.observer.get_workflow_run_block(
                    artifact.workflow_run_block_id, organization_id=organization_id
                )
                run_ids.add(block.workflow_run_id)
            for task_id in task_ids:
                task = await app.DATABASE.tasks.get_task(task_id, organization_id=organization_id)
                if not task:
                    raise PermissionError
                if task.browser_session_id:
                    session_ids.add(task.browser_session_id)
                if task.workflow_run_id:
                    run_ids.add(task.workflow_run_id)
                else:
                    standalone_ids.add(task_id)
            for task_v2_id in task_v2_ids:
                task_v2 = await app.DATABASE.observer.get_task_v2(task_v2_id, organization_id=organization_id)
                if not task_v2:
                    raise PermissionError
                if task_v2.browser_session_id:
                    session_ids.add(task_v2.browser_session_id)
                if task_v2.workflow_run_id:
                    run_ids.add(task_v2.workflow_run_id)
                else:
                    standalone_ids.add(task_v2_id)
            for session_id in session_ids:
                key = ("session", organization_id, session_id)
                if key not in checked_access:
                    if not await app.AGENT_FUNCTION.can_access_browser_session_artifacts(
                        principal, organization_id, session_id
                    ):
                        raise PermissionError
                    checked_access.add(key)
            for workflow_run_id in run_ids:
                key = ("run", organization_id, workflow_run_id)
                if key not in checked_access:
                    with browser_session_artifact_read(principal):
                        access = await browser_session_artifact_access(
                            organization_id,
                            workflow_run_id=workflow_run_id,
                            include_run_tasks=True,
                        )
                    if not access.allowed:
                        raise PermissionError
                    checked_access.add(key)
                if artifact.artifact_type in DESCENDANT_CHECKED_ARTIFACT_TYPES:
                    key = ("descendants", organization_id, workflow_run_id)
                    if key not in checked_access:
                        with browser_session_artifact_read(principal):
                            access = await child_run_artifact_access(organization_id, workflow_run_id)
                        if not access.allowed:
                            raise PermissionError
                        checked_access.add(key)
            for runnable_id in standalone_ids:
                key = ("runnable", organization_id, runnable_id)
                if key not in checked_access:
                    with browser_session_artifact_read(principal):
                        access = await browser_session_artifact_access(organization_id, runnable_ids=[runnable_id])
                    if not access.allowed:
                        raise PermissionError
                    checked_access.add(key)
        except PermissionError:
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Artifact access denied") from None
        except Exception:
            LOG.warning(
                "Artifact authorization lookup failed; denying",
                organization_id=organization_id,
                artifact_id=artifact.artifact_id,
            )
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Artifact access denied") from None

    if not check_folder:
        return
    # Any folder grant allows reads, so a caller who can see every folder needs no lookups.
    if await app.AGENT_FUNCTION.accessible_workflow_folder_ids(principal) is None:
        return
    for artifact_key, resource_key in (
        ("workflow_run_id", "workflow_run_id"),
        ("task_id", "task_id"),
        ("run_id", "run_id"),
        ("workflow_run_block_id", "workflow_run_block_id"),
        ("observer_cruise_id", "task_id"),
        ("browser_session_id", "browser_session_id"),
    ):
        if resource_id := getattr(artifact, artifact_key):
            await app.AGENT_FUNCTION.enforce_folder_access(principal, "workflow_run.read", {resource_key: resource_id})
            return
    if artifact.artifact_type == ArtifactType.SCRIPT_FILE:
        script_id = await app.DATABASE.scripts.get_script_id_for_artifact(
            artifact.organization_id, artifact.artifact_id
        )
        if script_id:
            await app.AGENT_FUNCTION.enforce_folder_access(principal, "script.read", {"script_id": script_id})
        return
    # Copilot screenshots and audio name their chat only in the log path create_log_artifact writes.
    if chat_match := _EMBER_CHAT_LOG_PATH.search(artifact.uri or ""):
        await app.AGENT_FUNCTION.enforce_folder_access(
            principal, EMBER_READ_ACTION, {"workflow_copilot_chat_id": chat_match.group(1)}
        )


async def readable_artifact_ids(artifacts: Iterable[Artifact], principal: RequestPrincipal | None) -> set[str]:
    decisions: dict[tuple[str | None, ...], bool] = {}
    checked_access: set[tuple[str, str, str]] = set()
    readable: set[str] = set()
    for artifact in artifacts:
        key = artifact_access_key(artifact)
        if key not in decisions:
            try:
                await authorize_artifact_read(artifact, principal, checked_access)
                decisions[key] = True
            except HTTPException:
                decisions[key] = False
        if decisions[key]:
            readable.add(artifact.artifact_id)
    return readable
