from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

import structlog
from fastapi import HTTPException

from skyvern.core.script_generations.script_block_extractor import (
    RunSignatureValidationError,
    ScriptBlockExtractionError,
    extract_script_blocks,
)
from skyvern.forge import app
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.repositories.scripts import WorkflowScriptUpsertStatus, WorkflowScriptWriterIntent
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.scripts import (
    DeployCachedScriptBlockPlan,
    DeployCachedScriptRequest,
    DeployCachedScriptResponse,
    FileEncoding,
    ScriptFileCreate,
    ScriptStatus,
    WorkflowScript,
)
from skyvern.services.workflow_script_service import CacheKeyResolutionError, resolve_cache_key_value

_CODE_VERSION_STATIC = 1
_CODE_VERSION_ADAPTIVE = 2
_MAX_SCRIPT_FILE_BYTES = 10 * 1024 * 1024

LOG = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _CachedScriptDeployPlan:
    workflow: Workflow
    proposed_workflow: Workflow
    cache_key_value: str
    block_plans: list[DeployCachedScriptBlockPlan]
    cacheable_block_count: int
    skipped_block_labels: list[str]
    warnings: list[str]
    validated_files: list[_ValidatedScriptFile]


@dataclass(frozen=True)
class _ValidatedScriptFile:
    file: ScriptFileCreate
    content_bytes: bytes


@dataclass
class _CachedScriptDeployUndoState:
    script_revision_id: str | None = None
    workflow_script_created_snapshot: WorkflowScript | None = None
    workflow_script_written_snapshot: WorkflowScript | None = None
    workflow_script_restore_snapshot: WorkflowScript | None = None
    workflow_dispatch_updated: bool = False
    workflow_dispatch_restore_run_with: str | None = None
    workflow_dispatch_restore_cache_key: str | None = None
    workflow_dispatch_restore_code_version: int | None = None
    workflow_dispatch_written_run_with: str | None = None
    workflow_dispatch_written_cache_key: str | None = None
    workflow_dispatch_written_code_version: int | None = None

    @property
    def has_writes(self) -> bool:
        return (
            self.script_revision_id is not None
            or self.workflow_script_created_snapshot is not None
            or self.workflow_script_written_snapshot is not None
            or self.workflow_script_restore_snapshot is not None
            or self.workflow_dispatch_updated
        )


def _decode_script_file_bytes(file: ScriptFileCreate) -> bytes:
    if file.encoding == FileEncoding.BASE64:
        try:
            return base64.b64decode(file.content, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"File {file.path!r} is not valid base64") from exc
    return file.content.encode("utf-8")


def _validate_script_file_path(file_path: str) -> None:
    parts = file_path.split("/")
    if file_path.startswith("/") or "\\" in file_path or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(
            status_code=400,
            detail=f"File path {file_path!r} must be a relative POSIX path without empty, '.', or '..' segments",
        )


def _validate_script_files(files: list[ScriptFileCreate]) -> list[_ValidatedScriptFile]:
    seen_paths: set[str] = set()
    validated_files: list[_ValidatedScriptFile] = []
    for file in files:
        _validate_script_file_path(file.path)
        if file.path in seen_paths:
            raise HTTPException(status_code=400, detail=f"Duplicate script file path {file.path!r}")
        seen_paths.add(file.path)
        content_bytes = _decode_script_file_bytes(file)
        if len(content_bytes) > _MAX_SCRIPT_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"File {file.path!r} exceeds maximum size of {_MAX_SCRIPT_FILE_BYTES} bytes",
            )
        validated_files.append(_ValidatedScriptFile(file=file, content_bytes=content_bytes))
    return validated_files


def _main_py(files: list[_ValidatedScriptFile]) -> str:
    for validated_file in files:
        if validated_file.file.path == "main.py":
            try:
                return validated_file.content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="File 'main.py' is not valid UTF-8") from exc
    raise HTTPException(status_code=400, detail="Cached script deploy requires a main.py file")


def _workflow_with_cache_key(workflow: Workflow, cache_key: str | None) -> Workflow:
    if cache_key is None or cache_key == workflow.cache_key:
        return workflow
    return workflow.model_copy(update={"cache_key": cache_key})


def _code_version_for_cache_context(request: DeployCachedScriptRequest) -> int:
    return _CODE_VERSION_ADAPTIVE if request.cache_context.adaptive_caching else _CODE_VERSION_STATIC


def _require_cache_key(plan: _CachedScriptDeployPlan) -> str:
    if plan.proposed_workflow.cache_key is None:
        raise HTTPException(status_code=400, detail="Cached script deploy requires a non-null workflow cache_key")
    return plan.proposed_workflow.cache_key


async def _persist_script_files(
    *,
    files: list[_ValidatedScriptFile],
    organization_id: str,
    script_id: str,
    script_version: int,
    script_revision_id: str,
) -> None:
    for validated_file in files:
        file = validated_file.file
        content_bytes = validated_file.content_bytes
        content_hash = f"sha256:{hashlib.sha256(content_bytes).hexdigest()}"
        artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
            organization_id=organization_id,
            script_id=script_id,
            script_version=script_version,
            file_path=file.path,
            data=content_bytes,
        )
        await app.DATABASE.scripts.create_script_file(
            script_revision_id=script_revision_id,
            script_id=script_id,
            organization_id=organization_id,
            file_path=file.path,
            file_name=file.path.split("/")[-1],
            file_type="file",
            content_hash=content_hash,
            file_size=len(content_bytes),
            mime_type=file.mime_type,
            encoding=file.encoding.value,
            artifact_id=artifact_id,
        )


async def _build_cached_script_deploy_plan(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    request: DeployCachedScriptRequest,
) -> _CachedScriptDeployPlan:
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.workflow_id != request.workflow_id or workflow.version != request.workflow_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "Workflow version is stale: expected "
                f"{request.workflow_id} v{request.workflow_version}, found {workflow.workflow_id} v{workflow.version}"
            ),
        )

    proposed_workflow = _workflow_with_cache_key(workflow, request.cache_key)
    validated_files = _validate_script_files(request.files)
    main_py = _main_py(validated_files)

    try:
        extraction = extract_script_blocks(main_py, proposed_workflow.workflow_definition.model_dump(mode="json"))
    except (ScriptBlockExtractionError, RunSignatureValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cacheable_blocks = extraction.cacheable_blocks
    if not cacheable_blocks:
        raise HTTPException(status_code=400, detail="Cached script deploy found zero cacheable script blocks")

    missing_globals = {block.label: list(block.missing_globals) for block in cacheable_blocks if block.missing_globals}
    if missing_globals:
        raise HTTPException(status_code=400, detail={"missing_globals": missing_globals})

    try:
        cache_key_value = resolve_cache_key_value(
            proposed_workflow,
            request.cache_context.parameters,
            adaptive_caching=request.cache_context.adaptive_caching,
            strict=True,
            domain_override=request.cache_context.domain_override,
        )
    except CacheKeyResolutionError as exc:
        raise HTTPException(status_code=400, detail=f"Could not resolve cache key value: {exc}") from exc

    if request.resolved_cache_key_value and request.resolved_cache_key_value != cache_key_value:
        raise HTTPException(
            status_code=400,
            detail=(
                "Resolved cache key value mismatch: "
                f"expected {request.resolved_cache_key_value!r}, resolved {cache_key_value!r}"
            ),
        )

    block_plans = [
        DeployCachedScriptBlockPlan(
            label=block.label,
            primitive=block.primitive,
            run_signature=block.run_signature,
            block_type=block.block_type,
            is_cacheable=block.is_cacheable,
            is_compound=block.is_compound,
            missing_globals=list(block.missing_globals),
            requires_agent=request.requires_agent_overrides.get(block.label, not block.is_cacheable),
        )
        for block in extraction.blocks
    ]

    return _CachedScriptDeployPlan(
        workflow=workflow,
        proposed_workflow=proposed_workflow,
        cache_key_value=cache_key_value,
        block_plans=block_plans,
        cacheable_block_count=len(cacheable_blocks),
        skipped_block_labels=[block.label for block in extraction.blocks if not block.is_cacheable],
        warnings=list(extraction.warnings),
        validated_files=validated_files,
    )


def _response_from_plan(
    *,
    plan: _CachedScriptDeployPlan,
    dry_run: bool,
    script_id: str | None = None,
    script_revision_id: str | None = None,
    script_version: int | None = None,
    workflow_script_id: str | None = None,
    workflow_script_upsert_status: str | None = None,
    script_was_created: bool = False,
) -> DeployCachedScriptResponse:
    return DeployCachedScriptResponse(
        workflow_id=plan.workflow.workflow_id,
        workflow_version=plan.workflow.version,
        cache_key=plan.proposed_workflow.cache_key,
        cache_key_value=plan.cache_key_value,
        dry_run=dry_run,
        would_create_script=True,
        script_was_created=script_was_created,
        script_id=script_id,
        script_revision_id=script_revision_id,
        script_version=script_version,
        workflow_script_id=workflow_script_id,
        workflow_script_upsert_status=workflow_script_upsert_status,
        cacheable_block_count=plan.cacheable_block_count,
        skipped_block_labels=plan.skipped_block_labels,
        blocks=plan.block_plans,
        warnings=plan.warnings,
    )


async def _cleanup_failed_cached_script_deploy(
    *,
    organization_id: str,
    plan: _CachedScriptDeployPlan,
    undo_state: _CachedScriptDeployUndoState,
) -> None:
    cleanup_errors: list[str] = []
    if undo_state.workflow_dispatch_updated:
        try:
            restored_workflow = await app.DATABASE.workflows.restore_workflow_script_dispatch_if_matches(
                workflow_id=plan.workflow.workflow_id,
                organization_id=organization_id,
                run_with=undo_state.workflow_dispatch_restore_run_with,
                cache_key=undo_state.workflow_dispatch_restore_cache_key,
                code_version=undo_state.workflow_dispatch_restore_code_version,
                current_run_with=undo_state.workflow_dispatch_written_run_with,
                current_cache_key=undo_state.workflow_dispatch_written_cache_key,
                current_code_version=undo_state.workflow_dispatch_written_code_version,
            )
            if restored_workflow is None:
                LOG.warning(
                    "Skipped restoring workflow dispatch after cached script deploy failure because it changed",
                    workflow_id=plan.workflow.workflow_id,
                    organization_id=organization_id,
                )
        except Exception:
            cleanup_errors.append("workflow_dispatch")
            LOG.warning(
                "Failed to restore workflow dispatch state after cached script deploy failure",
                workflow_id=plan.workflow.workflow_id,
                organization_id=organization_id,
                exc_info=True,
            )

    if (
        undo_state.workflow_script_restore_snapshot is not None
        and undo_state.workflow_script_written_snapshot is not None
    ):
        try:
            restored_workflow_script = await app.DATABASE.scripts.restore_workflow_script_if_matches(
                current_workflow_script=undo_state.workflow_script_written_snapshot,
                restore_workflow_script=undo_state.workflow_script_restore_snapshot,
            )
            if not restored_workflow_script:
                LOG.warning(
                    "Skipped restoring workflow script after cached script deploy failure because it changed",
                    workflow_script_id=undo_state.workflow_script_written_snapshot.workflow_script_id,
                    organization_id=organization_id,
                )
        except Exception:
            cleanup_errors.append("workflow_script_restore")
            LOG.warning(
                "Failed to restore workflow script after cached script deploy failure",
                workflow_script_id=undo_state.workflow_script_restore_snapshot.workflow_script_id,
                organization_id=organization_id,
                exc_info=True,
            )
    elif undo_state.workflow_script_created_snapshot is not None:
        try:
            deleted_workflow_script = await app.DATABASE.scripts.soft_delete_workflow_script_if_matches(
                workflow_script=undo_state.workflow_script_created_snapshot,
            )
            if not deleted_workflow_script:
                LOG.warning(
                    "Skipped soft-deleting workflow script after cached script deploy failure because it changed",
                    workflow_script_id=undo_state.workflow_script_created_snapshot.workflow_script_id,
                    organization_id=organization_id,
                )
        except Exception:
            cleanup_errors.append("workflow_script")
            LOG.warning(
                "Failed to soft-delete workflow script after cached script deploy failure",
                workflow_script_id=undo_state.workflow_script_created_snapshot.workflow_script_id,
                organization_id=organization_id,
                exc_info=True,
            )

    if undo_state.script_revision_id is not None:
        try:
            await app.DATABASE.scripts.soft_delete_script_by_revision(
                script_revision_id=undo_state.script_revision_id,
                organization_id=organization_id,
            )
        except Exception:
            cleanup_errors.append("script_revision")
            LOG.warning(
                "Failed to soft-delete script revision after cached script deploy failure",
                script_revision_id=undo_state.script_revision_id,
                organization_id=organization_id,
                exc_info=True,
            )

    if cleanup_errors:
        LOG.error(
            "Cached script deploy failed and cleanup was incomplete",
            workflow_id=plan.workflow.workflow_id,
            organization_id=organization_id,
            cleanup_errors=cleanup_errors,
        )


async def deploy_cached_script(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    request: DeployCachedScriptRequest,
) -> DeployCachedScriptResponse:
    plan = await _build_cached_script_deploy_plan(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        request=request,
    )
    cache_key = _require_cache_key(plan)
    if request.dry_run:
        return _response_from_plan(plan=plan, dry_run=True)

    script = None
    commit_stage = "create_script"
    undo_state = _CachedScriptDeployUndoState()
    try:
        script = await app.DATABASE.scripts.create_script(
            organization_id=organization_id,
            run_id=request.source_workflow_run_id,
        )
        undo_state.script_revision_id = script.script_revision_id
        commit_stage = "persist_script_files"
        await _persist_script_files(
            files=plan.validated_files,
            organization_id=organization_id,
            script_id=script.script_id,
            script_version=script.version,
            script_revision_id=script.script_revision_id,
        )

        commit_stage = "upsert_script_blocks"
        for block in plan.block_plans:
            await app.DATABASE.scripts.upsert_script_block(
                script_revision_id=script.script_revision_id,
                script_id=script.script_id,
                organization_id=organization_id,
                script_block_label=block.label,
                run_signature=block.run_signature,
                requires_agent=block.requires_agent,
            )

        commit_stage = "upsert_workflow_script"
        workflow_script_result = await app.DATABASE.scripts.upsert_workflow_script(
            organization_id=organization_id,
            script_id=script.script_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=plan.workflow.workflow_id,
            workflow_run_id=request.source_workflow_run_id,
            cache_key=cache_key,
            cache_key_value=plan.cache_key_value,
            status=ScriptStatus.published,
            is_pinned=True,
            writer_intent=WorkflowScriptWriterIntent.deploy,
        )
        if workflow_script_result.status == WorkflowScriptUpsertStatus.created:
            undo_state.workflow_script_created_snapshot = workflow_script_result.workflow_script
        elif workflow_script_result.status == WorkflowScriptUpsertStatus.updated:
            undo_state.workflow_script_written_snapshot = workflow_script_result.workflow_script
            undo_state.workflow_script_restore_snapshot = workflow_script_result.previous_workflow_script
            if undo_state.workflow_script_restore_snapshot is None:
                LOG.warning(
                    "Cached script deploy updated workflow script without a previous snapshot",
                    workflow_script_id=workflow_script_result.workflow_script.workflow_script_id,
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    cache_key_value=plan.cache_key_value,
                )

        commit_stage = "update_workflow_dispatch_state"
        code_version = _code_version_for_cache_context(request)
        dispatch_update_result = await app.DATABASE.workflows.update_workflow_dispatch_state_if_latest_with_previous(
            workflow_id=plan.workflow.workflow_id,
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            expected_version=plan.workflow.version,
            run_with="code",
            cache_key=cache_key,
            code_version=code_version,
        )
        previous_dispatch_state = dispatch_update_result.previous_dispatch_state
        undo_state.workflow_dispatch_updated = True
        undo_state.workflow_dispatch_restore_run_with = previous_dispatch_state.run_with
        undo_state.workflow_dispatch_restore_cache_key = previous_dispatch_state.cache_key
        undo_state.workflow_dispatch_restore_code_version = previous_dispatch_state.code_version
        undo_state.workflow_dispatch_written_run_with = "code"
        undo_state.workflow_dispatch_written_cache_key = cache_key
        undo_state.workflow_dispatch_written_code_version = code_version

        return _response_from_plan(
            plan=plan,
            dry_run=False,
            script_id=script.script_id,
            script_revision_id=script.script_revision_id,
            script_version=script.version,
            workflow_script_id=workflow_script_result.workflow_script.workflow_script_id,
            workflow_script_upsert_status=workflow_script_result.status.value,
            script_was_created=True,
        )
    except NotFoundError as exc:
        LOG.exception(
            "cached_script_deploy_commit_failed",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=plan.workflow.workflow_id,
            workflow_version=plan.workflow.version,
            cache_key_value=plan.cache_key_value,
            script_id=script.script_id if script else None,
            commit_stage=commit_stage,
        )
        if undo_state.has_writes:
            await _cleanup_failed_cached_script_deploy(
                organization_id=organization_id,
                plan=plan,
                undo_state=undo_state,
            )
        raise HTTPException(
            status_code=409,
            detail=(
                "Workflow version became stale before deploy commit: expected "
                f"{plan.workflow.workflow_id} v{plan.workflow.version}"
            ),
        ) from exc
    except Exception:
        LOG.exception(
            "cached_script_deploy_commit_failed",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=plan.workflow.workflow_id,
            workflow_version=plan.workflow.version,
            cache_key_value=plan.cache_key_value,
            script_id=script.script_id if script else None,
            commit_stage=commit_stage,
        )
        if undo_state.has_writes:
            await _cleanup_failed_cached_script_deploy(
                organization_id=organization_id,
                plan=plan,
                undo_state=undo_state,
            )
        raise
