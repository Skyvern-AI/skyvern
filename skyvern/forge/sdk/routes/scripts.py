import asyncio
import base64
import hashlib
from typing import TYPE_CHECKING

import structlog
from fastapi import BackgroundTasks, Depends, HTTPException, Path, Query, Request

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.models import WorkflowScriptModel

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.scripts import (
    ClearCacheResponse,
    CreateScriptRequest,
    CreateScriptResponse,
    DeployScriptRequest,
    FallbackEpisodeListResponse,
    PinScriptRequest,
    PinScriptResponse,
    ReviewScriptRequest,
    ReviewScriptResponse,
    Script,
    ScriptBlocksRequest,
    ScriptBlocksResponse,
    ScriptCacheKeyValuesResponse,
    ScriptFallbackEpisode,
    ScriptRunsResponse,
    ScriptRunSummary,
    ScriptStatus,
    ScriptVersionCompareResponse,
    ScriptVersionDetailResponse,
    ScriptVersionListResponse,
    ScriptVersionSummary,
    WorkflowScriptsListResponse,
    WorkflowScriptSummary,
)
from skyvern.services import script_service, workflow_script_service
from skyvern.services.script_reviewer import ScriptReviewer, load_filtered_run_param_values, store_review_artifacts
from skyvern.services.workflow_script_service import (
    create_script_version_from_review,
    extract_cached_blocks_from_source,
)

LOG = structlog.get_logger()


async def _load_main_script_content(
    organization_id: str,
    script_revision_id: str,
) -> str | None:
    """Load the main.py content from a script revision, if it exists."""
    script_files = await app.DATABASE.scripts.get_script_files(
        script_revision_id=script_revision_id,
        organization_id=organization_id,
    )
    for f in script_files:
        if f.file_path == "main.py" and f.artifact_id:
            artifact = await app.DATABASE.artifacts.get_artifact_by_id(f.artifact_id, organization_id)
            if artifact:
                data = await app.STORAGE.retrieve_artifact(artifact)
                if data:
                    try:
                        return data.decode("utf-8") if isinstance(data, bytes) else data
                    except UnicodeDecodeError:
                        LOG.error(
                            "main.py content is not valid UTF-8",
                            script_revision_id=script_revision_id,
                            organization_id=organization_id,
                        )
    return None


async def get_script_blocks_response(
    organization_id: str,
    workflow_permanent_id: str,
    script_revision_id: str,
    include_main_script: bool = False,
    script_id: str | None = None,
    version: int | None = None,
) -> ScriptBlocksResponse:
    script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
        script_revision_id=script_revision_id,
        organization_id=organization_id,
    )

    if not script_blocks:
        LOG.info(
            "No script block found for workflow",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            script_revision_id=script_revision_id,
        )
        main_script = None
        if include_main_script:
            main_script = await _load_main_script_content(
                organization_id=organization_id,
                script_revision_id=script_revision_id,
            )
        return ScriptBlocksResponse(blocks={}, main_script=main_script, script_id=script_id, version=version)

    result: dict[str, str] = {}
    main_py_block_codes: dict[str, str] | None = None

    # TODO(jdo): make concurrent to speed up
    for script_block in script_blocks:
        script_file_id = script_block.script_file_id

        if not script_file_id:
            # Reviewer-created blocks have no script_file_id — fall back to
            # extracting the block code from main.py (lazy-loaded once).
            block_label = script_block.script_block_label
            if main_py_block_codes is None:
                content = await _load_main_script_content(
                    organization_id=organization_id,
                    script_revision_id=script_revision_id,
                )
                main_py_block_codes = extract_cached_blocks_from_source(content) if content else {}

            if block_label in main_py_block_codes:
                result[block_label] = main_py_block_codes[block_label]
                continue

            LOG.info(
                "No script file ID found for script block and block not in main.py",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                script_revision_id=script_revision_id,
                block_label=block_label,
            )
            continue

        script_file = await app.DATABASE.scripts.get_script_file_by_id(
            script_revision_id=script_revision_id,
            file_id=script_file_id,
            organization_id=organization_id,
        )

        if not script_file:
            LOG.info(
                "No script file found for script block",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                script_revision_id=script_revision_id,
                block_label=script_block.script_block_label,
                script_file_id=script_file_id,
            )
            continue

        artifact_id = script_file.artifact_id

        if not artifact_id:
            LOG.info(
                "No artifact ID found for script file",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                script_revision_id=script_revision_id,
                block_label=script_block.script_block_label,
                script_file_id=script_file_id,
            )
            continue

        artifact = await app.DATABASE.artifacts.get_artifact_by_id(
            artifact_id,
            organization_id,
        )

        if not artifact:
            LOG.error(
                "No artifact found for script file",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                script_revision_id=script_revision_id,
                block_label=script_block.script_block_label,
                script_file_id=script_file_id,
                artifact_id=artifact_id,
            )
            continue

        data = await app.STORAGE.retrieve_artifact(artifact)

        if not data:
            LOG.error(
                "No data found for artifact",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                block_label=script_block.script_block_label,
                script_revision_id=script_block.script_revision_id,
                file_id=script_file_id,
                artifact_id=artifact_id,
            )
            continue

        try:
            decoded_data = data.decode("utf-8")
            result[script_block.script_block_label] = decoded_data
        except UnicodeDecodeError:
            LOG.error(
                "File content is not valid UTF-8 text",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                block_label=script_block.script_block_label,
                script_revision_id=script_block.script_revision_id,
                file_id=script_file_id,
                artifact_id=artifact_id,
            )
            continue

    main_script = None
    if include_main_script:
        main_script = await _load_main_script_content(
            organization_id=organization_id,
            script_revision_id=script_revision_id,
        )

    return ScriptBlocksResponse(blocks=result, main_script=main_script, script_id=script_id, version=version)


@base_router.post(
    "/scripts",
    response_model=CreateScriptResponse,
    summary="Create script",
    description="Create a new script with optional files and metadata",
    tags=["Scripts"],
    openapi_extra={
        "x-fern-sdk-method-name": "create_script",
    },
)
@base_router.post(
    "/scripts/",
    response_model=CreateScriptResponse,
    include_in_schema=False,
)
async def create_script(
    data: CreateScriptRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateScriptResponse:
    """Create a new script with optional files and metadata."""
    return await script_service.create_script(
        organization_id=current_org.organization_id,
        workflow_id=data.workflow_id,
        run_id=data.run_id,
        files=data.files,
    )


@base_router.get(
    "/scripts/{script_id}",
    response_model=Script,
    summary="Get script by ID",
    description="Retrieves a specific script by its ID",
    tags=["Scripts"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_script",
    },
)
@base_router.get(
    "/scripts/{script_id}/",
    response_model=Script,
    include_in_schema=False,
)
async def get_script(
    script_id: str = Path(
        ...,
        description="The unique identifier of the script",
        examples=["s_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Script:
    """Get a script by its ID."""
    LOG.info(
        "Getting script",
        organization_id=current_org.organization_id,
        script_id=script_id,
    )

    script = await app.DATABASE.scripts.get_script(
        script_id=script_id,
        organization_id=current_org.organization_id,
    )

    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    return script


@base_router.get(
    "/scripts/{script_id}/versions",
    include_in_schema=False,
    response_model=ScriptVersionListResponse,
)
@base_router.get(
    "/scripts/{script_id}/versions/",
    include_in_schema=False,
    response_model=ScriptVersionListResponse,
)
async def get_script_versions(
    script_id: str = Path(..., description="The script ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptVersionListResponse:
    """List all versions of a script."""
    scripts = await app.DATABASE.scripts.get_script_versions(
        script_id=script_id,
        organization_id=current_org.organization_id,
    )
    versions = [
        ScriptVersionSummary(
            version=s.version,
            script_revision_id=s.script_revision_id,
            created_at=s.created_at,
            run_id=s.run_id,
        )
        for s in scripts
    ]
    return ScriptVersionListResponse(versions=versions)


@base_router.get(
    "/scripts/{script_id}/versions/{version}",
    include_in_schema=False,
    response_model=ScriptBlocksResponse,
)
@base_router.get(
    "/scripts/{script_id}/versions/{version}/",
    include_in_schema=False,
    response_model=ScriptBlocksResponse,
)
async def get_script_version_code(
    script_id: str = Path(..., description="The script ID"),
    version: int = Path(..., description="The version number"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptBlocksResponse:
    """Get a specific version's code blocks."""
    script = await app.DATABASE.scripts.get_script(
        script_id=script_id,
        organization_id=current_org.organization_id,
        version=version,
    )
    if not script:
        raise HTTPException(status_code=404, detail="Script version not found")

    # script_id doubles as workflow_permanent_id for script-based lookups
    return await get_script_blocks_response(
        script_revision_id=script.script_revision_id,
        organization_id=current_org.organization_id,
        workflow_permanent_id=script_id,
        include_main_script=True,
        script_id=script.script_id,
        version=script.version,
    )


@base_router.get(
    "/scripts/{script_id}/compare",
    include_in_schema=False,
    response_model=ScriptVersionCompareResponse,
)
@base_router.get(
    "/scripts/{script_id}/compare/",
    include_in_schema=False,
    response_model=ScriptVersionCompareResponse,
)
async def compare_script_versions(
    script_id: str = Path(..., description="The script ID"),
    base: int = Query(..., description="Base version number"),
    compare: int = Query(..., description="Compare version number"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptVersionCompareResponse:
    """Compare two script versions side by side."""
    organization_id = current_org.organization_id

    base_script, compare_script = await asyncio.gather(
        app.DATABASE.scripts.get_script(script_id=script_id, organization_id=organization_id, version=base),
        app.DATABASE.scripts.get_script(script_id=script_id, organization_id=organization_id, version=compare),
    )
    if not base_script:
        raise HTTPException(status_code=404, detail=f"Base version {base} not found")
    if not compare_script:
        raise HTTPException(status_code=404, detail=f"Compare version {compare} not found")

    base_response, compare_response = await asyncio.gather(
        get_script_blocks_response(
            script_revision_id=base_script.script_revision_id,
            organization_id=organization_id,
            workflow_permanent_id=script_id,
            include_main_script=True,
            script_id=base_script.script_id,
            version=base_script.version,
        ),
        get_script_blocks_response(
            script_revision_id=compare_script.script_revision_id,
            organization_id=organization_id,
            workflow_permanent_id=script_id,
            include_main_script=True,
            script_id=compare_script.script_id,
            version=compare_script.version,
        ),
    )

    return ScriptVersionCompareResponse(
        script_id=script_id,
        base_version=base_script.version,
        base_blocks=base_response.blocks,
        base_main_script=base_response.main_script,
        base_created_at=base_script.created_at,
        base_run_id=base_script.run_id,
        compare_version=compare_script.version,
        compare_blocks=compare_response.blocks,
        compare_main_script=compare_response.main_script,
        compare_created_at=compare_script.created_at,
        compare_run_id=compare_script.run_id,
    )


@base_router.get(
    "/scripts/{script_id}/versions/{version}/detail",
    include_in_schema=False,
    response_model=ScriptVersionDetailResponse,
)
@base_router.get(
    "/scripts/{script_id}/versions/{version}/detail/",
    include_in_schema=False,
    response_model=ScriptVersionDetailResponse,
)
async def get_script_version_detail(
    script_id: str = Path(..., description="The script ID"),
    version: int = Path(..., description="The version number"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptVersionDetailResponse:
    """Get full detail for a specific script version, including code blocks and metadata."""
    organization_id = current_org.organization_id

    script = await app.DATABASE.scripts.get_script(
        script_id=script_id,
        organization_id=organization_id,
        version=version,
    )
    if not script:
        raise HTTPException(status_code=404, detail="Script version not found")

    # script_id doubles as workflow_permanent_id for script-based lookups
    blocks_response, fallback_episode_count = await asyncio.gather(
        get_script_blocks_response(
            script_revision_id=script.script_revision_id,
            organization_id=organization_id,
            workflow_permanent_id=script_id,
            include_main_script=True,
            script_id=script.script_id,
            version=script.version,
        ),
        app.DATABASE.scripts.get_fallback_episodes_count(
            organization_id=organization_id,
            script_revision_id=script.script_revision_id,
        ),
    )

    return ScriptVersionDetailResponse(
        script_id=script.script_id,
        script_revision_id=script.script_revision_id,
        version=script.version,
        created_at=script.created_at,
        run_id=script.run_id,
        blocks=blocks_response.blocks,
        main_script=blocks_response.main_script,
        fallback_episode_count=fallback_episode_count,
    )


@base_router.get(
    "/scripts",
    response_model=list[Script],
    summary="Get all scripts",
    description="Retrieves a paginated list of scripts for the current organization",
    tags=["Scripts"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_scripts",
    },
)
@base_router.get(
    "/scripts/",
    response_model=list[Script],
    include_in_schema=False,
)
async def get_scripts(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(
        1,
        ge=1,
        description="Page number for pagination",
        examples=[1],
    ),
    page_size: int = Query(
        10,
        ge=1,
        description="Number of items per page",
        examples=[10],
    ),
) -> list[Script]:
    """Get all scripts for the current organization."""
    LOG.info(
        "Getting scripts",
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
    )

    scripts = await app.DATABASE.scripts.get_scripts(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
    )

    return scripts


@base_router.post(
    "/scripts/{script_id}/deploy",
    response_model=CreateScriptResponse,
    summary="Deploy script",
    description="Deploy a script with updated files, creating a new version",
    tags=["Scripts"],
    openapi_extra={
        "x-fern-sdk-method-name": "deploy_script",
    },
)
@base_router.post(
    "/scripts/{script_id}/deploy/",
    response_model=CreateScriptResponse,
    include_in_schema=False,
)
async def deploy_script(
    data: DeployScriptRequest,
    script_id: str = Path(
        ...,
        description="The unique identifier of the script",
        examples=["s_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateScriptResponse:
    """Deploy a script with updated files, creating a new version."""
    LOG.info(
        "Deploying script",
        organization_id=current_org.organization_id,
        script_id=script_id,
        file_count=len(data.files) if data.files else 0,
    )

    try:
        # Get the latest version of the script
        latest_script = await app.DATABASE.scripts.get_latest_script_version(
            script_id=script_id,
            organization_id=current_org.organization_id,
        )

        if not latest_script:
            raise HTTPException(status_code=404, detail="Script not found")

        # Create a new version of the script
        new_version = latest_script.version + 1
        new_script = await app.DATABASE.scripts.create_script(
            organization_id=current_org.organization_id,
            run_id=latest_script.run_id,
            script_id=script_id,
            version=new_version,
        )

        # Fetch source files from the base revision to build the old->new ID mapping
        source_files = await app.DATABASE.scripts.get_script_files(
            script_revision_id=latest_script.script_revision_id,
            organization_id=current_org.organization_id,
        )
        source_file_by_path = {f.file_path: f for f in source_files}

        # Track old file_id -> new file_id so blocks can be re-pointed
        old_to_new_file_id: dict[str, str] = {}

        # Process uploaded files — upload to artifact storage and create DB records
        file_count = 0
        deployed_file_paths: set[str] = set()
        if data.files:
            file_count = len(data.files)
            for file in data.files:
                content_bytes = base64.b64decode(file.content)
                content_hash = hashlib.sha256(content_bytes).hexdigest()
                file_name = file.path.split("/")[-1]
                deployed_file_paths.add(file.path)

                artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                    organization_id=current_org.organization_id,
                    script_id=new_script.script_id,
                    script_version=new_script.version,
                    file_path=file.path,
                    data=content_bytes,
                )
                new_file = await app.DATABASE.scripts.create_script_file(
                    script_revision_id=new_script.script_revision_id,
                    script_id=new_script.script_id,
                    organization_id=current_org.organization_id,
                    file_path=file.path,
                    file_name=file_name,
                    file_type="file",
                    content_hash=f"sha256:{content_hash}",
                    file_size=len(content_bytes),
                    mime_type=file.mime_type or "text/x-python",
                    artifact_id=artifact_id,
                )
                # Map old file ID to new so blocks referencing replaced files get updated
                old_source = source_file_by_path.get(file.path)
                if old_source:
                    old_to_new_file_id[old_source.file_id] = new_file.file_id

        # Copy files from the base revision that weren't replaced by the deploy
        for f in source_files:
            if f.file_path in deployed_file_paths:
                continue
            new_file = await app.DATABASE.scripts.create_script_file(
                script_revision_id=new_script.script_revision_id,
                script_id=new_script.script_id,
                organization_id=current_org.organization_id,
                file_path=f.file_path,
                file_name=f.file_name,
                file_type=f.file_type,
                content_hash=f.content_hash,
                file_size=f.file_size,
                mime_type=f.mime_type,
                encoding=f.encoding or "utf-8",
                artifact_id=f.artifact_id,
            )
            old_to_new_file_id[f.file_id] = new_file.file_id

        # Copy existing script blocks, re-pointing file IDs to the new revision's files
        existing_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
            script_revision_id=latest_script.script_revision_id,
            organization_id=current_org.organization_id,
        )
        for sb in existing_blocks:
            new_file_id = old_to_new_file_id.get(sb.script_file_id, sb.script_file_id) if sb.script_file_id else None
            await app.DATABASE.scripts.create_script_block(
                organization_id=current_org.organization_id,
                script_id=new_script.script_id,
                script_revision_id=new_script.script_revision_id,
                script_block_label=sb.script_block_label,
                script_file_id=new_file_id,
                run_signature=sb.run_signature,
                workflow_run_id=sb.workflow_run_id,
                workflow_run_block_id=sb.workflow_run_block_id,
                input_fields=sb.input_fields,
                requires_agent=sb.requires_agent,
            )

        return CreateScriptResponse(
            script_id=new_script.script_id,
            version=new_script.version,
            run_id=new_script.run_id,
            file_count=file_count,
            created_at=new_script.created_at,
            file_tree={},
        )

    except HTTPException:
        raise
    except Exception as e:
        LOG.error("Failed to deploy script", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to deploy script")


@base_router.post(
    "/scripts/{script_id}/run",
    summary="Run script",
    description="Run a script",
    tags=["Scripts"],
)
@base_router.post(
    "/scripts/{script_id}/run/",
    include_in_schema=False,
)
async def run_script(
    request: Request,
    background_tasks: BackgroundTasks,
    script_id: str = Path(
        ...,
        description="The unique identifier of the script",
        examples=["s_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    """Run a script."""
    raise HTTPException(status_code=400, detail="Not implemented")
    # await script_service.execute_script(
    #     script_id=script_id,
    #     organization_id=current_org.organization_id,
    #     background_tasks=background_tasks,
    # )
    await AsyncExecutorFactory.get_executor().execute_script(
        request=request,
        script_id=script_id,
        organization_id=current_org.organization_id,
        background_tasks=background_tasks,
    )


@base_router.post(
    "/scripts/{workflow_permanent_id}/blocks",
    include_in_schema=False,
    response_model=ScriptBlocksResponse,
)
@base_router.post(
    "/scripts/{workflow_permanent_id}/blocks/",
    include_in_schema=False,
    response_model=ScriptBlocksResponse,
)
async def get_workflow_script_blocks(
    workflow_permanent_id: str,
    block_script_request: ScriptBlocksRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptBlocksResponse:
    empty = ScriptBlocksResponse(blocks={})
    cache_key_value = block_script_request.cache_key_value

    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    include_main_script = True
    workflow_run_id = block_script_request.workflow_run_id
    if workflow_run_id:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=current_org.organization_id,
        )
        if not workflow_run:
            workflow_run_id = None
        else:
            # Try to find the script version pinned to this specific run first.
            # get_workflow_script() always resolves the latest version for a
            # cache_key_value, but the Code tab should show the version that was
            # active when this run executed (SKY-8448).
            workflow_script = await app.DATABASE.scripts.get_workflow_script(
                organization_id=current_org.organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                statuses=[ScriptStatus.published],
            )
            if workflow_script:
                published_script, _is_pinned = await workflow_script_service.get_workflow_script_by_cache_key_value(
                    organization_id=current_org.organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    cache_key_value=workflow_script.cache_key_value,
                    workflow_run_id=workflow_run_id,
                    statuses=[ScriptStatus.published],
                )
                if published_script:
                    return await get_script_blocks_response(
                        script_revision_id=published_script.script_revision_id,
                        organization_id=current_org.organization_id,
                        workflow_permanent_id=workflow_permanent_id,
                        include_main_script=include_main_script,
                        script_id=published_script.script_id,
                        version=published_script.version,
                    )

            # Fall back to latest version (for runs without a workflow_script entry)
            published_script, _rendered_cache_key, _is_pinned = await workflow_script_service.get_workflow_script(
                workflow=workflow,
                workflow_run=workflow_run,
                status=ScriptStatus.published,
            )
            if published_script:
                return await get_script_blocks_response(
                    script_revision_id=published_script.script_revision_id,
                    organization_id=current_org.organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    include_main_script=include_main_script,
                    script_id=published_script.script_id,
                    version=published_script.version,
                )

    cache_key = block_script_request.cache_key or workflow.cache_key or ""
    status = block_script_request.status

    script, _is_pinned = await workflow_script_service.get_workflow_script_by_cache_key_value(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_run_id=block_script_request.workflow_run_id,
        cache_key_value=cache_key_value,
        cache_key=cache_key,
        statuses=[status] if status else None,
    )

    if not script:
        LOG.info(
            "No scripts found for workflow",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=current_org.organization_id,
            cache_key_value=cache_key_value,
            cache_key=cache_key,
        )
        return empty

    return await get_script_blocks_response(
        script_revision_id=script.script_revision_id,
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        include_main_script=include_main_script,
        script_id=script.script_id,
        version=script.version,
    )


@base_router.get(
    "/scripts/{workflow_permanent_id}/{cache_key}/values",
    include_in_schema=False,
    response_model=ScriptCacheKeyValuesResponse,
)
@base_router.get(
    "/scripts/{workflow_permanent_id}/{cache_key}/values/",
    include_in_schema=False,
    response_model=ScriptCacheKeyValuesResponse,
)
async def get_workflow_cache_key_values(
    workflow_permanent_id: str,
    cache_key: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(
        1,
        ge=1,
        description="Page number for pagination",
        examples=[1],
    ),
    page_size: int = Query(
        100,
        ge=1,
        description="Number of items per page",
        examples=[100],
    ),
    filter: str | None = Query(
        None,
        description="Filter values by a substring",
        examples=["value1", "value2"],
    ),
) -> ScriptCacheKeyValuesResponse:
    # TODO(jdo): concurrent-ize

    values = await app.DATABASE.scripts.get_workflow_cache_key_values(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key=cache_key,
        page=page,
        page_size=page_size,
        filter=filter,
    )

    total_count = await app.DATABASE.scripts.get_workflow_cache_key_count(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key=cache_key,
    )

    filtered_count = await app.DATABASE.scripts.get_workflow_cache_key_count(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key=cache_key,
        filter=filter,
    )

    return ScriptCacheKeyValuesResponse(
        filtered_count=filtered_count,
        page=page,
        page_size=page_size,
        total_count=total_count,
        values=values,
    )


@base_router.get(
    "/scripts/workflows/{workflow_permanent_id}",
    include_in_schema=False,
    response_model=WorkflowScriptsListResponse,
)
@base_router.get(
    "/scripts/workflows/{workflow_permanent_id}/",
    include_in_schema=False,
    response_model=WorkflowScriptsListResponse,
)
async def list_workflow_scripts(
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowScriptsListResponse:
    """List all scripts (cache key variants) for a workflow with version stats."""
    organization_id = current_org.organization_id

    # Verify workflow exists (consistent with other script endpoints)
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workflow_scripts = await app.DATABASE.scripts.get_workflow_scripts_by_permanent_id(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        statuses=[ScriptStatus.published],
    )

    if not workflow_scripts:
        return WorkflowScriptsListResponse(scripts=[])

    # Group by cache_key_value -- one row per variant.
    # Multiple runs can reference the same script; collapse them.
    by_cache_key: dict[str, list[WorkflowScriptModel]] = {}
    for ws in workflow_scripts:
        by_cache_key.setdefault(ws.cache_key_value, []).append(ws)

    # Pick the most recent script per cache_key_value.
    # Relies on get_workflow_scripts_by_permanent_id() ORDER BY modified_at DESC.
    representatives: list[WorkflowScriptModel] = []
    for _ckv, group in by_cache_key.items():
        representatives.append(group[0])

    if not representatives:
        return WorkflowScriptsListResponse(scripts=[])

    # Batch queries for version stats and run stats (success_rate + total_runs
    # computed from the same DB population for consistency).
    # These are independent -- run in parallel.
    rep_script_ids = [ws.script_id for ws in representatives]
    version_stats, run_stats = await asyncio.gather(
        app.DATABASE.scripts.get_script_version_stats(
            organization_id=organization_id,
            script_ids=rep_script_ids,
        ),
        app.DATABASE.scripts.get_script_run_stats(
            organization_id=organization_id,
            script_ids=rep_script_ids,
        ),
    )

    summaries = []
    for ws in representatives:
        latest_version, version_count = version_stats.get(ws.script_id, (0, 0))
        if version_count == 0:
            continue

        try:
            status = ScriptStatus(ws.status) if ws.status else ScriptStatus.published
        except ValueError:
            status = ScriptStatus.published

        success_rate, total_runs = run_stats.get(ws.script_id, (None, 0))

        summaries.append(
            WorkflowScriptSummary(
                script_id=ws.script_id,
                cache_key=ws.cache_key,
                cache_key_value=ws.cache_key_value,
                status=status,
                latest_version=latest_version,
                version_count=version_count,
                total_runs=total_runs,
                success_rate=success_rate,
                is_pinned=bool(ws.is_pinned),
                created_at=ws.created_at,
                modified_at=ws.modified_at,
            )
        )

    # Sort: published first, then by modified_at DESC
    summaries.sort(key=lambda s: (s.status != ScriptStatus.published, -s.modified_at.timestamp()))
    return WorkflowScriptsListResponse(scripts=summaries)


@base_router.get(
    "/scripts/{script_id}/runs",
    include_in_schema=False,
)
@base_router.get(
    "/scripts/{script_id}/runs/",
    include_in_schema=False,
)
async def get_script_runs(
    script_id: str = Path(..., description="The script ID"),
    page_size: int = Query(50, ge=1, le=100),
    version: int | None = Query(None, description="Filter runs to a specific script version"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptRunsResponse:
    """Get workflow runs associated with a specific script, with status counts."""
    organization_id = current_org.organization_id

    # Verify script exists
    script = await app.DATABASE.scripts.get_script(script_id=script_id, organization_id=organization_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    created_after = None
    created_before = None

    if version is not None:
        # Get all versions to determine the time window for this version
        all_versions = await app.DATABASE.scripts.get_script_versions(
            script_id=script_id,
            organization_id=organization_id,
        )
        # versions is ordered by version DESC
        version_found = False
        for i, v in enumerate(all_versions):
            if v.version == version:
                created_after = v.created_at
                # Next higher version (i-1 in DESC order) sets the upper bound
                if i > 0:
                    created_before = all_versions[i - 1].created_at
                version_found = True
                break
        if not version_found:
            raise HTTPException(status_code=404, detail=f"Script version {version} not found")

    runs, total_count, status_counts, avg_fallbacks_per_run = await app.DATABASE.scripts.get_workflow_runs_for_script(
        organization_id=organization_id,
        script_id=script_id,
        page_size=page_size,
        created_after=created_after,
        created_before=created_before,
    )

    return ScriptRunsResponse(
        runs=[
            ScriptRunSummary(
                workflow_run_id=r.workflow_run_id,
                status=r.status or "unknown",
                started_at=r.started_at,
                finished_at=r.finished_at,
                created_at=r.created_at,
                failure_reason=r.failure_reason,
            )
            for r in runs
        ],
        total_count=total_count,
        status_counts=status_counts,
        avg_fallbacks_per_run=avg_fallbacks_per_run,
    )


@base_router.delete(
    "/scripts/{workflow_permanent_id}/value",
    include_in_schema=False,
)
@base_router.delete(
    "/scripts/{workflow_permanent_id}/value/",
    include_in_schema=False,
)
async def delete_workflow_cache_key_value(
    workflow_permanent_id: str,
    cache_key_value: str = Query(alias="cache-key-value"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, str]:
    """Delete a specific cache key value for a workflow."""
    LOG.info(
        "Deleting workflow cache key value",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
    )

    # Verify workflow exists
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Delete the cache key value
    deleted = await app.DATABASE.scripts.delete_workflow_cache_key_value(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
    )

    if not deleted:
        raise HTTPException(status_code=404, detail="Cache key value not found")

    # Clear in-memory cache so stale entries aren't served after deletion
    cache_cleared_count = workflow_script_service.clear_workflow_script_cache(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    LOG.info(
        "Deleted workflow cache key value",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
        cache_cleared_count=cache_cleared_count,
    )

    return {"message": "Cache key value deleted successfully"}


@base_router.delete(
    "/scripts/{workflow_permanent_id}/cache",
    response_model=ClearCacheResponse,
    summary="Clear cached scripts for workflow",
    description="Clear all cached scripts for a specific workflow. This will trigger script regeneration on subsequent runs.",
    tags=["Scripts"],
    openapi_extra={
        "x-fern-sdk-method-name": "clear_workflow_cache",
    },
    include_in_schema=False,
)
@base_router.delete(
    "/scripts/{workflow_permanent_id}/cache/",
    response_model=ClearCacheResponse,
    include_in_schema=False,
)
async def clear_workflow_cache(
    workflow_permanent_id: str = Path(
        ...,
        description="The workflow permanent ID to clear cache for",
        examples=["wpid_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ClearCacheResponse:
    """Clear all cached scripts for a specific workflow."""
    LOG.info(
        "Clearing workflow cache",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    # Verify workflow exists
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Clear database cache (soft delete)
    deleted_count = await app.DATABASE.scripts.delete_workflow_scripts_by_permanent_id(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    # Clear in-memory cache
    cache_cleared_count = workflow_script_service.clear_workflow_script_cache(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    LOG.info(
        "Cleared workflow cache",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        deleted_count=deleted_count,
        cache_cleared_count=cache_cleared_count,
    )

    return ClearCacheResponse(
        deleted_count=deleted_count,
        message=f"Successfully cleared {deleted_count} database record(s) and {cache_cleared_count} in-memory cache entry(s) for workflow {workflow_permanent_id}",
    )


@base_router.post(
    "/scripts/{workflow_permanent_id}/pin",
    include_in_schema=False,
    response_model=PinScriptResponse,
)
@base_router.post(
    "/scripts/{workflow_permanent_id}/pin/",
    include_in_schema=False,
    response_model=PinScriptResponse,
)
async def pin_workflow_script(
    data: PinScriptRequest,
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> PinScriptResponse:
    """Pin a script for a specific cache key value, preventing auto-updates."""
    LOG.info(
        "Pinning workflow script",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
    )

    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    result = await app.DATABASE.scripts.pin_workflow_script(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
        pinned_by=None,
    )

    if not result:
        raise HTTPException(status_code=404, detail="No script found for the given cache key value")

    return PinScriptResponse(
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
        is_pinned=True,
        pinned_at=result.pinned_at,
    )


@base_router.post(
    "/scripts/{workflow_permanent_id}/unpin",
    include_in_schema=False,
    response_model=PinScriptResponse,
)
@base_router.post(
    "/scripts/{workflow_permanent_id}/unpin/",
    include_in_schema=False,
    response_model=PinScriptResponse,
)
async def unpin_workflow_script(
    data: PinScriptRequest,
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> PinScriptResponse:
    """Unpin a script for a specific cache key value, allowing auto-updates."""
    LOG.info(
        "Unpinning workflow script",
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
    )

    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    result = await app.DATABASE.scripts.unpin_workflow_script(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
    )

    if not result:
        raise HTTPException(status_code=404, detail="No script found for the given cache key value")

    return PinScriptResponse(
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=data.cache_key_value,
        is_pinned=False,
        pinned_at=None,
    )


@base_router.post(
    "/scripts/{workflow_permanent_id}/review",
    include_in_schema=False,
    response_model=ReviewScriptResponse,
)
@base_router.post(
    "/scripts/{workflow_permanent_id}/review/",
    include_in_schema=False,
    response_model=ReviewScriptResponse,
)
async def review_script_with_instructions(
    data: ReviewScriptRequest,
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ReviewScriptResponse:
    """Review and fix a script using user-provided instructions.

    Uses the script reviewer pipeline to update the script based on the user's
    instructions. When a workflow_run_id is provided, fallback episodes from
    that run are included as context.
    """
    organization_id = current_org.organization_id

    # Enforce CODE_BLOCK_ENABLED feature flag server-side (mirrors frontend gating).
    # When ENABLE_CODE_BLOCK=True (self-hosted), all orgs have code block access by default
    # so the PostHog check is skipped — self-hosted operators control their own deployment.
    if not settings.ENABLE_CODE_BLOCK and app.EXPERIMENTATION_PROVIDER:
        code_block_enabled = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            "CODE_BLOCK_ENABLED",
            organization_id,
            properties={"organization_id": organization_id},
        )
        if not code_block_enabled:
            raise HTTPException(status_code=403, detail="Script editing is not enabled for this organization")

    # Load the workflow
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Load fallback episodes and the script associated with the run
    episodes = []
    workflow_run = None
    run_parameter_values: dict[str, str] = {}
    latest_script = None
    if data.workflow_run_id:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=data.workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise HTTPException(status_code=404, detail="Workflow run not found")
        if workflow_run.workflow_permanent_id != workflow_permanent_id:
            raise HTTPException(status_code=400, detail="Workflow run does not belong to this workflow")
        # Look up the specific script used by this run
        run_workflow_script = await app.DATABASE.scripts.get_workflow_script(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=data.workflow_run_id,
        )
        if run_workflow_script:
            latest_script = await app.DATABASE.scripts.get_latest_script_version(
                script_id=run_workflow_script.script_id,
                organization_id=organization_id,
            )
        episodes = await app.DATABASE.scripts.get_fallback_episodes(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=data.workflow_run_id,
            page=1,
            page_size=50,
        )
        run_parameter_values = await load_filtered_run_param_values(data.workflow_run_id)

    # Fall back to any published script if run-specific lookup didn't find one
    if not latest_script:
        latest_script = await workflow_script_service.get_latest_published_script(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
    if not latest_script:
        raise HTTPException(status_code=404, detail="No published script found for this workflow")

    # Run the reviewer
    reviewer = ScriptReviewer()
    review_results = await reviewer.review_with_user_instructions(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        script_revision_id=latest_script.script_revision_id,
        user_instructions=data.user_instructions,
        # Convert empty list/dict to None so the reviewer uses the no-episodes path
        episodes=episodes or None,
        run_parameter_values=run_parameter_values or None,
    )

    if not review_results:
        return ReviewScriptResponse(
            script_id=latest_script.script_id,
            version=latest_script.version,
            updated_blocks=[],
            message="No changes were needed — the current code already satisfies your instructions.",
        )

    # Extract code-only dict for creating the script version
    updated_blocks = {label: r.code for label, r in review_results.items()}

    # Create a new script version with the updated blocks
    new_script = await create_script_version_from_review(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        base_script=latest_script,
        updated_blocks=updated_blocks,
        workflow=workflow,
        workflow_run=workflow_run,
    )

    if not new_script:
        raise HTTPException(status_code=500, detail="Failed to create new script version")

    # Store reviewer artifacts (prompt + LLM response) for each block
    await store_review_artifacts(
        organization_id=organization_id,
        script_id=new_script.script_id,
        script_version=new_script.version,
        review_results=review_results,
    )

    LOG.info(
        "Script reviewed with user instructions",
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        script_id=new_script.script_id,
        version=new_script.version,
        updated_blocks=list(review_results.keys()),
    )

    return ReviewScriptResponse(
        script_id=new_script.script_id,
        version=new_script.version,
        updated_blocks=list(review_results.keys()),
    )


@base_router.get(
    "/workflows/{workflow_permanent_id}/fallback-episodes",
    include_in_schema=False,
    response_model=FallbackEpisodeListResponse,
)
@base_router.get(
    "/workflows/{workflow_permanent_id}/fallback-episodes/",
    include_in_schema=False,
    response_model=FallbackEpisodeListResponse,
)
async def get_fallback_episodes(
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    workflow_run_id: str | None = Query(None, description="Filter by workflow run ID"),
    block_label: str | None = Query(None, description="Filter by block label"),
    reviewed: bool | None = Query(None, description="Filter by reviewed status"),
    fallback_type: str | None = Query(None, description="Filter by fallback type"),
) -> FallbackEpisodeListResponse:
    # Verify workflow exists
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    episodes = await app.DATABASE.scripts.get_fallback_episodes(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        page=page,
        page_size=page_size,
        workflow_run_id=workflow_run_id,
        block_label=block_label,
        reviewed=reviewed,
        fallback_type=fallback_type,
    )
    total_count = await app.DATABASE.scripts.get_fallback_episodes_count(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_run_id=workflow_run_id,
        block_label=block_label,
        reviewed=reviewed,
        fallback_type=fallback_type,
    )
    return FallbackEpisodeListResponse(
        episodes=episodes,
        page=page,
        page_size=page_size,
        total_count=total_count,
    )


@base_router.get(
    "/workflows/{workflow_permanent_id}/fallback-episodes/{episode_id}",
    include_in_schema=False,
    response_model=ScriptFallbackEpisode,
)
@base_router.get(
    "/workflows/{workflow_permanent_id}/fallback-episodes/{episode_id}/",
    include_in_schema=False,
    response_model=ScriptFallbackEpisode,
)
async def get_fallback_episode(
    workflow_permanent_id: str = Path(..., description="The workflow permanent ID"),
    episode_id: str = Path(..., description="The episode ID"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ScriptFallbackEpisode:
    # Verify workflow exists
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    episode = await app.DATABASE.scripts.get_fallback_episode(
        episode_id=episode_id,
        organization_id=current_org.organization_id,
    )
    if not episode:
        raise HTTPException(status_code=404, detail="Fallback episode not found")
    if episode.workflow_permanent_id != workflow_permanent_id:
        raise HTTPException(status_code=404, detail="Fallback episode not found")
    return episode
