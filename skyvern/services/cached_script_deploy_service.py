from __future__ import annotations

import base64

from fastapi import HTTPException

from skyvern.core.script_generations.script_block_extractor import (
    RunSignatureValidationError,
    ScriptBlockExtractionError,
    extract_script_blocks,
)
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.scripts import (
    DeployCachedScriptBlockPlan,
    DeployCachedScriptRequest,
    DeployCachedScriptResponse,
    FileEncoding,
    ScriptFileCreate,
)
from skyvern.services.workflow_script_service import CacheKeyResolutionError, resolve_cache_key_value


def _decode_script_file(file: ScriptFileCreate) -> str:
    if file.encoding == FileEncoding.BASE64:
        try:
            return base64.b64decode(file.content).decode("utf-8")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"File {file.path!r} is not valid base64 UTF-8") from exc
    return file.content


def _main_py(files: list[ScriptFileCreate]) -> str:
    for file in files:
        if file.path == "main.py":
            return _decode_script_file(file)
    raise HTTPException(status_code=400, detail="Cached script deploy requires a main.py file")


def _workflow_with_cache_key(workflow: Workflow, cache_key: str | None) -> Workflow:
    if cache_key is None or cache_key == workflow.cache_key:
        return workflow
    return workflow.model_copy(update={"cache_key": cache_key})


async def dry_run_cached_script_deploy(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    request: DeployCachedScriptRequest,
) -> DeployCachedScriptResponse:
    if not request.dry_run:
        raise HTTPException(
            status_code=400, detail="Commit mode is not enabled for deploy_cached yet; use dry_run=true"
        )

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
    main_py = _main_py(request.files)

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
            block_type=block.block_type,
            is_cacheable=block.is_cacheable,
            is_compound=block.is_compound,
            missing_globals=list(block.missing_globals),
            requires_agent=request.requires_agent_overrides.get(block.label, False),
        )
        for block in extraction.blocks
    ]

    return DeployCachedScriptResponse(
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.version,
        cache_key=proposed_workflow.cache_key,
        cache_key_value=cache_key_value,
        dry_run=True,
        would_create_script=True,
        cacheable_block_count=len(cacheable_blocks),
        skipped_block_labels=[block.label for block in extraction.blocks if not block.is_cacheable],
        blocks=block_plans,
        warnings=extraction.warnings,
    )
