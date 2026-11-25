import re
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.id import WORKFLOW_RUN_PREFIX
from skyvern.forge.sdk.workflow.reached_download_target import registered_download_proof_view

LOG = structlog.get_logger()

SCREENSHOT_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.SCREENSHOT,
        ArtifactType.SCREENSHOT_LLM,
        ArtifactType.SCREENSHOT_ACTION,
        ArtifactType.SCREENSHOT_FINAL,
        ArtifactType.SCREENSHOT_PROXY,
        ArtifactType.SCREENSHOT_PRE_SUBMIT,
    }
)
# Stored artifact objects are named {timestamp}_{artifact_id}_{artifact_type}.{ext}.
_STORED_ARTIFACT_ID = re.compile(r"_(a_\d+)_")
_ARTIFACT_PATH_VERSION = "v1"


def bound_to_run(artifact: Artifact, run_ids: Collection[str]) -> bool:
    return artifact.workflow_run_id in run_ids or artifact.run_id in run_ids


async def unreadable_run_artifact_ids(
    organization_id: str,
    workflow_run_id: str | None,
    artifacts: Sequence[Artifact],
    *,
    run_ids: Collection[str],
    task_id: str | None = None,
) -> set[str]:
    """Ids the executing run can neither claim as its own nor read; ``task_id`` names a task outside a workflow run."""
    foreign = [
        artifact
        for artifact in artifacts
        if not bound_to_run(artifact, run_ids) and not (task_id and artifact.task_id == task_id)
    ]
    if not foreign:
        return set()
    try:
        readable = await app.AGENT_FUNCTION.readable_run_artifact_ids(
            organization_id, workflow_run_id, foreign, task_id=task_id
        )
    except Exception:
        LOG.warning(
            "Artifact reference read check failed; treating the references as unreadable",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            exc_info=True,
        )
        readable = set()
    return {artifact.artifact_id for artifact in foreign if artifact.artifact_id not in readable}


async def unreadable_output_artifact_ids(
    organization_id: str,
    workflow_run_id: str,
    artifacts: Sequence[Artifact],
    *,
    screenshot_ids: Collection[str],
    download_ids: Collection[str],
    run_ids: Collection[str],
) -> set[str]:
    misnamed = {
        artifact.artifact_id
        for artifact in artifacts
        if (artifact.artifact_id in screenshot_ids and artifact.artifact_type not in SCREENSHOT_ARTIFACT_TYPES)
        or (artifact.artifact_id in download_ids and artifact.artifact_type != ArtifactType.DOWNLOAD)
    }
    named = [artifact for artifact in artifacts if artifact.artifact_id not in misnamed]
    return misnamed | await unreadable_run_artifact_ids(organization_id, workflow_run_id, named, run_ids=run_ids)


def drop_withheld_downloads(value: Any, withheld: frozenset[str]) -> None:
    """Remove withheld download ids, and the stored entries and URLs that name them, from an output tree in place."""
    if isinstance(value, list):
        for item in value:
            drop_withheld_downloads(item, withheld)
        return
    if not isinstance(value, dict):
        return
    if "downloaded_file_artifact_ids" in value:
        value.update(registered_download_proof_view(value, withheld))
    for child in value.values():
        if isinstance(child, (dict, list)):
            drop_withheld_downloads(child, withheld)


def withhold_unreadable_references(
    organization_id: str,
    workflow_run_id: str | None,
    unreadable_count: int,
    *,
    source: str,
    reason: Literal["unreadable", "unresolved"] = "unreadable",
    task_id: str | None = None,
) -> bool:
    """Log the read decision for a run's artifact references and return whether to withhold the unreadable ones."""
    if not unreadable_count:
        return False
    enforced = settings.ARTIFACT_REFERENCE_READ_CHECK
    LOG.info(
        "Artifact reference read check decision",
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        task_id=task_id,
        source=source,
        reason=reason,
        unreadable_count=unreadable_count,
        enforced=enforced,
    )
    return enforced


class RunScope(NamedTuple):
    workflow_run_id: str | None
    # Set only for a task that runs outside a workflow run.
    task_id: str | None
    own_ids: frozenset[str]


def current_run_scope() -> RunScope | None:
    """The workflow run, or the task outside one, that the worker's context names, and the ids its files are under."""
    context = skyvern_context.current()
    if context is None:
        return None
    task_id = None if context.workflow_run_id else context.task_id
    subject_id = context.workflow_run_id or task_id
    if not subject_id:
        return None
    return RunScope(context.workflow_run_id, task_id, frozenset(i for i in (subject_id, context.run_id) if i))


async def check_run_artifact_reference(organization_id: str, artifact: Artifact | None, *, source: str) -> None:
    """Raise PermissionError when the check is enforced and the executing run cannot read the artifact.

    None stands for a reference that resolves to no stored object. A worker whose context names neither a workflow
    run nor a task is not checked.
    """
    scope = current_run_scope()
    if scope is None:
        return
    reason: Literal["unreadable", "unresolved"] = "unresolved" if artifact is None else "unreadable"
    unreadable = artifact is None or bool(
        await unreadable_run_artifact_ids(
            organization_id, scope.workflow_run_id, [artifact], run_ids=scope.own_ids, task_id=scope.task_id
        )
    )
    if withhold_unreadable_references(
        organization_id, scope.workflow_run_id, int(unreadable), source=source, reason=reason, task_id=scope.task_id
    ):
        raise PermissionError("No permission to read the referenced artifact")


def _owner_reference(uri: str, organization_id: str, **owner: str) -> Artifact:
    # A run-written object with no artifact row is judged by the run or session that owns it.
    now = datetime.now(UTC)
    return Artifact(
        artifact_id=uri,
        artifact_type=ArtifactType.DOWNLOAD,
        uri=uri,
        organization_id=organization_id,
        created_at=now,
        modified_at=now,
        **owner,
    )


async def _managed_file_reference(
    uri: str, organization_id: str, run_ids: frozenset[str]
) -> tuple[bool, Artifact | None]:
    """Whether the URI names a stored object outside the executing run, and the artifact to judge it by."""
    key = urlparse(uri).path.lstrip("/")
    env_org = f"{settings.ENV}/{organization_id}/"
    if ".." in key.split("/"):
        return True, None
    if key.startswith(f"{DOWNLOAD_FILE_PREFIX}/{env_org}"):
        owner_run_id = key.removeprefix(f"{DOWNLOAD_FILE_PREFIX}/{env_org}").partition("/")[0]
        if owner_run_id in run_ids:
            return False, None
        row = await app.DATABASE.artifacts.find_download_artifact(
            organization_id=organization_id, run_id=owner_run_id, uri=uri
        )
        return True, row or _owner_reference(uri, organization_id, run_id=owner_run_id)
    if key.startswith(f"{_ARTIFACT_PATH_VERSION}/{env_org}"):
        segments = key.removeprefix(f"{_ARTIFACT_PATH_VERSION}/{env_org}").split("/")
        if segments[0] == "browser_sessions" and len(segments) > 2:
            return True, _owner_reference(uri, organization_id, browser_session_id=segments[1])
        match = _STORED_ARTIFACT_ID.search(segments[-1])
        row = (
            await app.DATABASE.artifacts.get_artifact_by_id(artifact_id=match.group(1), organization_id=organization_id)
            if match
            else None
        )
        return True, row if row is not None and row.uri == uri else None
    if key.startswith(env_org):
        # Run-written uploads are keyed {env}/{org}/{date}/{workflow_run_id}/{name}; other uploads belong to the org.
        segments = key.removeprefix(env_org).split("/")
        if len(segments) > 2 and segments[1].startswith(WORKFLOW_RUN_PREFIX + "_"):
            if segments[1] in run_ids:
                return False, None
            return True, _owner_reference(uri, organization_id, workflow_run_id=segments[1])
    return False, None


async def check_managed_file_reference(uri: str, organization_id: str) -> None:
    """Apply the executing run's artifact read check to a cloud storage URI before its bytes are read.

    A ``file://`` URI reaches managed storage only as an uploaded file, which belongs to the organization.
    """
    scope = current_run_scope()
    if scope is None or urlparse(uri).scheme not in ("s3", "gs", "azure"):
        return
    try:
        outside_run, artifact = await _managed_file_reference(uri, organization_id, scope.own_ids)
    except Exception:
        LOG.warning(
            "Managed file reference lookup failed",
            workflow_run_id=scope.workflow_run_id,
            task_id=scope.task_id,
            exc_info=True,
        )
        outside_run, artifact = True, None
    if outside_run:
        await check_run_artifact_reference(organization_id, artifact, source="managed_file_input")
