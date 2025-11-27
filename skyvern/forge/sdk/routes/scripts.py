import base64
import hashlib

import structlog
from fastapi import BackgroundTasks, Depends, HTTPException, Path, Query, Request

from skyvern.forge import app
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.scripts import (
    CreateScriptRequest,
    CreateScriptResponse,
    DeployScriptRequest,
    Script,
    ScriptBlocksRequest,
    ScriptBlocksResponse,
    ScriptCacheKeyValuesResponse,
    ScriptStatus,
)
from skyvern.services import script_service, workflow_script_service

LOG = structlog.get_logger()


async def get_script_blocks_response(
    organization_id: str,
    workflow_permanent_id: str,
    script_revision_id: str,
) -> ScriptBlocksResponse:
    script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
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
        return ScriptBlocksResponse(blocks={})

    result: dict[str, str] = {}

    # TODO(jdo): make concurrent to speed up
    for script_block in script_blocks:
        script_file_id = script_block.script_file_id

        if not script_file_id:
            LOG.info(
                "No script file ID found for script block",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                script_revision_id=script_revision_id,
                block_label=script_block.script_block_label,
            )
            continue

        script_file = await app.DATABASE.get_script_file_by_id(
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

        artifact = await app.DATABASE.get_artifact_by_id(
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
    return ScriptBlocksResponse(blocks=result)


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

    script = await app.DATABASE.get_script(
        script_id=script_id,
        organization_id=current_org.organization_id,
    )

    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    return script


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

    scripts = await app.DATABASE.get_scripts(
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
    raise HTTPException(status_code=400, detail="Not implemented")

    try:
        # Get the latest version of the script
        latest_script = await app.DATABASE.get_script(
            script_id=script_id,
            organization_id=current_org.organization_id,
        )

        if not latest_script:
            raise HTTPException(status_code=404, detail="Script not found")

        # Create a new version of the script
        new_version = latest_script.version + 1
        new_script_revision = await app.DATABASE.create_script(
            organization_id=current_org.organization_id,
            run_id=latest_script.run_id,
            script_id=script_id,  # Use the same script_id for versioning
            version=new_version,
        )

        # Process files if provided
        file_tree = {}
        file_count = 0
        if data.files:
            file_tree = await script_service.build_file_tree(
                data.files,
                organization_id=current_org.organization_id,
                script_id=new_script_revision.script_id,
                script_version=new_script_revision.version,
                script_revision_id=new_script_revision.script_revision_id,
            )
            file_count = len(data.files)

            # Create script file records
            for file in data.files:
                content_bytes = base64.b64decode(file.content)
                content_hash = hashlib.sha256(content_bytes).hexdigest()
                file_size = len(content_bytes)

                # Extract file name from path
                file_name = file.path.split("/")[-1]

                await app.DATABASE.create_script_file(
                    script_revision_id=new_script_revision.script_revision_id,
                    script_id=new_script_revision.script_id,
                    organization_id=new_script_revision.organization_id,
                    file_path=file.path,
                    file_name=file_name,
                    file_type="file",
                    content_hash=f"sha256:{content_hash}",
                    file_size=file_size,
                    mime_type=file.mime_type,
                    encoding=file.encoding,
                )

        return CreateScriptResponse(
            script_id=new_script_revision.script_id,
            version=new_script_revision.version,
            run_id=new_script_revision.run_id,
            file_count=file_count,
            created_at=new_script_revision.created_at,
            file_tree=file_tree,
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

    workflow = await app.DATABASE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    workflow_run_id = block_script_request.workflow_run_id
    if workflow_run_id:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=current_org.organization_id,
        )
        if not workflow_run:
            workflow_run_id = None
        else:
            # find the published script if any and return that
            published_script, _ = await workflow_script_service.get_workflow_script(
                workflow=workflow,
                workflow_run=workflow_run,
                status=ScriptStatus.published,
            )
            if published_script:
                return await get_script_blocks_response(
                    script_revision_id=published_script.script_revision_id,
                    organization_id=current_org.organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                )

    cache_key = block_script_request.cache_key or workflow.cache_key or ""
    status = block_script_request.status

    script = await app.DATABASE.get_workflow_script_by_cache_key_value(
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

    values = await app.DATABASE.get_workflow_cache_key_values(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key=cache_key,
        page=page,
        page_size=page_size,
        filter=filter,
    )

    total_count = await app.DATABASE.get_workflow_cache_key_count(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key=cache_key,
    )

    filtered_count = await app.DATABASE.get_workflow_cache_key_count(
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
    workflow = await app.DATABASE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Delete the cache key value
    deleted = await app.DATABASE.delete_workflow_cache_key_value(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
    )

    if not deleted:
        raise HTTPException(status_code=404, detail="Cache key value not found")

    return {"message": "Cache key value deleted successfully"}
