import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.services.request_principal import RequestPrincipal, get_request_principal

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun

LOG = structlog.get_logger()

_artifact_read_scope: ContextVar[tuple[RequestPrincipal | None] | Literal["system"] | None] = ContextVar(
    "browser_session_artifact_read_scope", default=None
)


@contextmanager
def browser_session_artifact_read(principal: RequestPrincipal | None) -> Iterator[None]:
    token = _artifact_read_scope.set((principal,))
    try:
        yield
    finally:
        _artifact_read_scope.reset(token)


@contextmanager
def browser_session_artifact_system_read() -> Iterator[None]:
    token = _artifact_read_scope.set("system")
    try:
        yield
    finally:
        _artifact_read_scope.reset(token)


def browser_session_artifact_read_scope() -> tuple[RequestPrincipal | None] | None:
    scope = _artifact_read_scope.get()
    if scope == "system":
        return None
    return scope if scope is not None else (get_request_principal(),)


_ARTIFACT_FIELDS = frozenset(
    {
        "downloaded_files",
        "downloaded_file_url",
        "downloaded_file_urls",
        "downloaded_file_artifact_id",
        "downloaded_file_artifact_ids",
        "download_url",
        "download_urls",
        "file_url",
        "file_urls",
        "task_screenshots",
        "workflow_screenshots",
        "task_screenshot_artifact_ids",
        "workflow_screenshot_artifact_ids",
        "screenshot_artifact_id",
        "screenshot_artifact_ids",
        "screenshot_url",
        "screenshot_urls",
        "action_screenshot_urls",
        "recording_url",
        "recording_urls",
        "recording_artifact_id",
        "recording_artifact_ids",
        "browser_console_log_url",
        "artifact_id",
        "artifact_ids",
        "artifact_url",
        "artifact_urls",
        "artifact_uri",
        "artifact_uris",
        "signed_url",
        "signed_urls",
    }
)
_FILE_INFO_REQUIRED_FIELDS = ("checksum", "filename", "file_size")
_MAX_REDACTION_SCAN_CHARS = 65_536
_SIGNED_PARAMETER = re.compile(r"^(?:x-amz-signature|x-goog-signature|signature|sig)=")
_FILE_INFO_URL = re.compile(r"(?:['\"]url['\"]\s*:|\burl=)")
_FILE_INFO_SERIALIZED_FIELDS = {
    field: re.compile(rf"(?:['\"]{field}['\"]\s*:|\b{field}=)")
    for field in (*_FILE_INFO_REQUIRED_FIELDS, "artifact_id")
}


def redact_downloaded_files(value: Any) -> Any:
    if isinstance(value, FileInfo):
        return value.model_copy(update={"url": "[redacted]", "artifact_id": None})
    if isinstance(value, BaseModel):
        fields = dict(value.__dict__)
        fields.update(value.model_extra or {})
        return value.model_copy(update=redact_downloaded_files(fields))
    if isinstance(value, dict):
        file_info = "url" in value and (
            bool(value.get("artifact_id")) or all(field in value for field in _FILE_INFO_REQUIRED_FIELDS)
        )
        return {
            key: None if key in _ARTIFACT_FIELDS or (file_info and key == "url") else redact_downloaded_files(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_downloaded_files(item) for item in value]
    if isinstance(value, str):
        # Withhold oversized strings instead of scanning or returning an unclassified URL.
        if len(value) > _MAX_REDACTION_SCAN_CHARS:
            return "[redacted]"
        lowered = value.lower()
        if len(lowered) > _MAX_REDACTION_SCAN_CHARS:
            return "[redacted]"
        if "sig" in lowered and ("https://" in lowered or "http://" in lowered):
            for token in lowered.split():
                starts = [index for scheme in ("http://", "https://") if (index := token.find(scheme)) >= 0]
                if starts:
                    parts = re.split(r"[?&]", token[min(starts) :])
                    if any(_SIGNED_PARAMETER.match(part.removeprefix("amp;")) for part in parts[1:]):
                        return "[redacted]"
        if "url" in lowered and (
            "artifact_id" in lowered or all(field in lowered for field in _FILE_INFO_REQUIRED_FIELDS)
        ):
            if _FILE_INFO_URL.search(value) and (
                _FILE_INFO_SERIALIZED_FIELDS["artifact_id"].search(value)
                or all(_FILE_INFO_SERIALIZED_FIELDS[field].search(value) for field in _FILE_INFO_REQUIRED_FIELDS)
            ):
                return "[redacted]"
    return value


@dataclass(frozen=True)
class BrowserSessionArtifactAccess:
    allowed: bool

    def redact_output(self, value: Any) -> Any:
        return value if self.allowed else redact_downloaded_files(value)


async def browser_session_artifact_access(
    organization_id: str,
    *,
    session_ids: Iterable[str | None] = (),
    workflow_run_id: str | None = None,
    workflow_run: "WorkflowRun | None" = None,
    runnable_ids: Iterable[str | None] = (),
    include_run_tasks: bool = False,
) -> BrowserSessionArtifactAccess:
    scope = browser_session_artifact_read_scope()
    if scope is None or not await app.AGENT_FUNCTION.should_check_browser_session_artifacts(scope[0], organization_id):
        return BrowserSessionArtifactAccess(True)

    ids = {session_id for session_id in session_ids if session_id}
    runnable_ids = list(runnable_ids)
    if include_run_tasks and workflow_run_id:
        tasks = await app.DATABASE.tasks.get_tasks_for_workflow_run_status(workflow_run_id)
        task_v2 = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(
            workflow_run_id, organization_id=organization_id
        )
        ids.update(
            task.browser_session_id
            for task in tasks
            if task.organization_id == organization_id and task.browser_session_id
        )
        runnable_ids.extend(task.task_id for task in tasks if task.organization_id == organization_id)
        if task_v2:
            ids.update([task_v2.browser_session_id] if task_v2.browser_session_id else [])
            runnable_ids.append(task_v2.observer_cruise_id)
    if workflow_run is None and workflow_run_id:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id, organization_id=organization_id
        )
    if workflow_run and workflow_run.browser_session_id:
        ids.add(workflow_run.browser_session_id)
    runnable_lookup_ids = {runnable_id for runnable_id in (workflow_run_id, *runnable_ids) if runnable_id}
    if runnable_lookup_ids:
        ids.update(
            await app.DATABASE.browser_sessions.get_persistent_browser_session_ids_by_runnable_ids(
                runnable_lookup_ids, organization_id
            )
        )
    for session_id in ids:
        if not await app.AGENT_FUNCTION.can_access_browser_session_artifacts(scope[0], organization_id, session_id):
            return BrowserSessionArtifactAccess(False)
    if not ids and not await app.AGENT_FUNCTION.can_access_sessionless_artifacts(
        scope[0], organization_id, workflow_run_id, [runnable_id for runnable_id in runnable_ids if runnable_id]
    ):
        return BrowserSessionArtifactAccess(False)
    return BrowserSessionArtifactAccess(True)


async def child_run_artifact_access(organization_id: str, workflow_run_id: str) -> BrowserSessionArtifactAccess:
    scope = browser_session_artifact_read_scope()
    if scope is None or not await app.AGENT_FUNCTION.should_check_browser_session_artifacts(scope[0], organization_id):
        return BrowserSessionArtifactAccess(True)
    pending = [workflow_run_id]
    checked = {workflow_run_id}
    try:
        while pending:
            parent_id = pending.pop()
            children = await app.DATABASE.workflow_runs.get_workflow_runs_by_parent_workflow_run_id(
                parent_id, organization_id=organization_id
            )
            child_runs = {child.workflow_run_id: child for child in children}
            child_ids = set(child_runs) | await app.DATABASE.observer.get_child_workflow_run_ids(
                parent_id, organization_id
            )
            for child_id in child_ids - checked:
                checked.add(child_id)
                access = await browser_session_artifact_access(
                    organization_id,
                    workflow_run_id=child_id,
                    workflow_run=child_runs.get(child_id),
                    include_run_tasks=True,
                )
                if not access.allowed:
                    return access
                pending.append(child_id)
        return BrowserSessionArtifactAccess(True)
    except Exception as exc:
        LOG.warning(
            "Child run artifact authorization failed; redacting",
            organization_id=organization_id,
            exception_type=type(exc).__name__,
        )
        return BrowserSessionArtifactAccess(False)
