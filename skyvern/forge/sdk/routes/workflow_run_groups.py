from fastapi import Depends

from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.browser_session_artifacts import (
    browser_session_artifact_read,
    redact_workflow_run_artifact_rows,
)
from skyvern.forge.sdk.services.request_principal import get_request_principal
from skyvern.schemas.workflow_run_groups import WorkflowRunGroupCreateRequest, WorkflowRunGroupResponse
from skyvern.services import workflow_run_group_service


async def _redact_group(response: WorkflowRunGroupResponse, organization_id: str) -> WorkflowRunGroupResponse:
    with browser_session_artifact_read(get_request_principal()):
        items = await redact_workflow_run_artifact_rows(response.items, organization_id)
    return response.model_copy(update={"items": items})


@legacy_base_router.post("/workflow_run_groups", include_in_schema=False)
@base_router.post("/workflow_run_groups", response_model=WorkflowRunGroupResponse, include_in_schema=False)
async def submit_workflow_run_group(
    body: WorkflowRunGroupCreateRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunGroupResponse:
    await PermissionCheckerFactory.get_instance().check(organization)
    response = await workflow_run_group_service.submit_workflow_run_group(organization, body)
    return await _redact_group(response, organization.organization_id)


@legacy_base_router.get("/workflow_run_groups/{workflow_run_group_id}", include_in_schema=False)
@base_router.get(
    "/workflow_run_groups/{workflow_run_group_id}", response_model=WorkflowRunGroupResponse, include_in_schema=False
)
async def get_workflow_run_group(
    workflow_run_group_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunGroupResponse:
    response = await workflow_run_group_service.get_workflow_run_group(
        workflow_run_group_id, organization.organization_id
    )
    return await _redact_group(response, organization.organization_id)


@legacy_base_router.post("/workflow_run_groups/{workflow_run_group_id}/cancel", include_in_schema=False)
@base_router.post(
    "/workflow_run_groups/{workflow_run_group_id}/cancel",
    response_model=WorkflowRunGroupResponse,
    include_in_schema=False,
)
async def cancel_workflow_run_group(
    workflow_run_group_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunGroupResponse:
    response = await workflow_run_group_service.cancel_workflow_run_group(
        workflow_run_group_id, organization.organization_id
    )
    return await _redact_group(response, organization.organization_id)
