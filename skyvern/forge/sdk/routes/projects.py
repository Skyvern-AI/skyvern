import base64
import hashlib

import structlog
from fastapi import Depends, HTTPException, Path, Query

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.projects import CreateProjectRequest, CreateProjectResponse, DeployProjectRequest, Project
from skyvern.services import project_service

LOG = structlog.get_logger()


@base_router.post(
    "/projects",
    response_model=CreateProjectResponse,
    summary="Create project",
    description="Create a new project with optional files and metadata",
    tags=["Projects"],
    openapi_extra={
        "x-fern-sdk-method-name": "create_project",
    },
)
@base_router.post(
    "/projects/",
    response_model=CreateProjectResponse,
    include_in_schema=False,
)
async def create_project(
    data: CreateProjectRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateProjectResponse:
    """Create a new project with optional files and metadata."""
    organization_id = current_org.organization_id
    LOG.info(
        "Creating project",
        organization_id=organization_id,
        file_count=len(data.files) if data.files else 0,
    )
    # validate workflow_id and run_id
    if data.workflow_id:
        if not await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id=data.workflow_id, organization_id=organization_id
        ):
            raise WorkflowNotFound(workflow_permanent_id=data.workflow_id)
    if data.run_id:
        if not await app.DATABASE.get_run(run_id=data.run_id, organization_id=organization_id):
            raise HTTPException(status_code=404, detail=f"Run_id {data.run_id} not found")
    try:
        # Create the project in the database
        project = await app.DATABASE.create_project(
            organization_id=organization_id,
            workflow_permanent_id=data.workflow_id,
            run_id=data.run_id,
        )
        # Process files if provided
        file_tree = {}
        file_count = 0
        if data.files:
            file_tree = await project_service.build_file_tree(
                data.files,
                organization_id=organization_id,
                project_id=project.project_id,
                project_version=project.version,
                project_revision_id=project.project_revision_id,
            )
            file_count = len(data.files)
        return CreateProjectResponse(
            project_id=project.project_id,
            version=project.version,
            workflow_id=project.workflow_id,
            run_id=project.run_id,
            file_count=file_count,
            created_at=project.created_at,
            file_tree=file_tree,
        )
    except Exception as e:
        LOG.error("Failed to create project", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create project")


@legacy_base_router.get("/projects/{project_id}")
@legacy_base_router.get("/projects/{project_id}/", include_in_schema=False)
@base_router.get(
    "/projects/{project_id}",
    response_model=Project,
    summary="Get project by ID",
    description="Retrieves a specific project by its ID",
    tags=["Projects"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_project",
    },
)
@base_router.get(
    "/projects/{project_id}/",
    response_model=Project,
    include_in_schema=False,
)
async def get_project(
    project_id: str = Path(
        ...,
        description="The unique identifier of the project",
        examples=["proj_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Project:
    """Get a project by its ID."""
    LOG.info(
        "Getting project",
        organization_id=current_org.organization_id,
        project_id=project_id,
    )

    project = await app.DATABASE.get_project(
        project_id=project_id,
        organization_id=current_org.organization_id,
    )

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return project


@legacy_base_router.get("/projects")
@legacy_base_router.get("/projects/", include_in_schema=False)
@base_router.get(
    "/projects",
    response_model=list[Project],
    summary="Get all projects",
    description="Retrieves a paginated list of projects for the current organization",
    tags=["Projects"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_projects",
    },
)
@base_router.get(
    "/projects/",
    response_model=list[Project],
    include_in_schema=False,
)
async def get_projects(
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
) -> list[Project]:
    """Get all projects for the current organization."""
    LOG.info(
        "Getting projects",
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
    )

    projects = await app.DATABASE.get_projects(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
    )

    return projects


@base_router.post(
    "/projects/{project_id}/deploy",
    response_model=CreateProjectResponse,
    summary="Deploy project",
    description="Deploy a project with updated files, creating a new version",
    tags=["Projects"],
    openapi_extra={
        "x-fern-sdk-method-name": "deploy_project",
    },
)
@base_router.post(
    "/projects/{project_id}/deploy/",
    response_model=CreateProjectResponse,
    include_in_schema=False,
)
async def deploy_project(
    data: DeployProjectRequest,
    project_id: str = Path(
        ...,
        description="The unique identifier of the project",
        examples=["proj_abc123"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateProjectResponse:
    """Deploy a project with updated files, creating a new version."""
    LOG.info(
        "Deploying project",
        organization_id=current_org.organization_id,
        project_id=project_id,
        file_count=len(data.files) if data.files else 0,
    )

    try:
        # Get the latest version of the project
        latest_project = await app.DATABASE.get_project(
            project_id=project_id,
            organization_id=current_org.organization_id,
        )

        if not latest_project:
            raise HTTPException(status_code=404, detail="Project not found")

        # Create a new version of the project
        new_version = latest_project.version + 1
        new_project_revision = await app.DATABASE.create_project(
            organization_id=current_org.organization_id,
            workflow_permanent_id=latest_project.workflow_id,
            run_id=latest_project.run_id,
            project_id=project_id,  # Use the same project_id for versioning
            version=new_version,
        )

        # Process files if provided
        file_tree = {}
        file_count = 0
        if data.files:
            file_tree = await project_service.build_file_tree(
                data.files,
                organization_id=current_org.organization_id,
                project_id=new_project_revision.project_id,
                project_version=new_project_revision.version,
                project_revision_id=new_project_revision.project_revision_id,
            )
            file_count = len(data.files)

            # Create project file records
            for file in data.files:
                content_bytes = base64.b64decode(file.content)
                content_hash = hashlib.sha256(content_bytes).hexdigest()
                file_size = len(content_bytes)

                # Extract file name from path
                file_name = file.path.split("/")[-1]

                await app.DATABASE.create_project_file(
                    project_revision_id=new_project_revision.project_revision_id,
                    project_id=new_project_revision.project_id,
                    organization_id=new_project_revision.organization_id,
                    file_path=file.path,
                    file_name=file_name,
                    file_type="file",
                    content_hash=f"sha256:{content_hash}",
                    file_size=file_size,
                    mime_type=file.mime_type,
                    encoding=file.encoding,
                )

        return CreateProjectResponse(
            project_id=new_project_revision.project_id,
            version=new_project_revision.version,
            workflow_id=new_project_revision.workflow_id,
            run_id=new_project_revision.run_id,
            file_count=file_count,
            created_at=new_project_revision.created_at,
            file_tree=file_tree,
        )

    except HTTPException:
        raise
    except Exception as e:
        LOG.error("Failed to deploy project", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to deploy project")
