import base64
import hashlib

import structlog
from fastapi import BackgroundTasks, Depends, HTTPException, Path, Query, Request

from skyvern.forge import app
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.scripts import CreateScriptRequest, CreateScriptResponse, DeployScriptRequest, Script
from skyvern.services import script_service

LOG = structlog.get_logger()


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
    organization_id = current_org.organization_id
    LOG.info(
        "Creating script",
        organization_id=organization_id,
        file_count=len(data.files) if data.files else 0,
    )
    if data.run_id:
        if not await app.DATABASE.get_run(run_id=data.run_id, organization_id=organization_id):
            raise HTTPException(status_code=404, detail=f"Run_id {data.run_id} not found")
    try:
        # Create the script in the database
        script = await app.DATABASE.create_script(
            organization_id=organization_id,
            run_id=data.run_id,
        )
        # Process files if provided
        file_tree = {}
        file_count = 0
        if data.files:
            file_tree = await script_service.build_file_tree(
                data.files,
                organization_id=organization_id,
                script_id=script.script_id,
                script_version=script.version,
                script_revision_id=script.script_revision_id,
            )
            file_count = len(data.files)
        return CreateScriptResponse(
            script_id=script.script_id,
            version=script.version,
            run_id=script.run_id,
            file_count=file_count,
            created_at=script.created_at,
            file_tree=file_tree,
        )
    except Exception as e:
        LOG.error("Failed to create script", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create script")


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
