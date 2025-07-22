from typing import Annotated

import structlog
from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request

from skyvern.config import settings
from skyvern.exceptions import MissingBrowserAddressError
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.routes.code_samples import (
    LOGIN_CODE_SAMPLE_BITWARDEN,
    LOGIN_CODE_SAMPLE_ONEPASSWORD,
    LOGIN_CODE_SAMPLE_SKYVERN,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.models.yaml import (
    BitwardenLoginCredentialParameterYAML,
    LoginBlockYAML,
    OnePasswordCredentialParameterYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowParameterYAML,
)
from skyvern.schemas.run_blocks import CredentialType, LoginRequest
from skyvern.schemas.runs import ProxyLocation, RunType, WorkflowRunRequest, WorkflowRunResponse
from skyvern.services import workflow_service

LOG = structlog.get_logger()
DEFAULT_LOGIN_PROMPT = """If you're not on the login page, navigate to login page and login using the credentials given.
First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page.
If a 2-factor step appears, enter the authentication code.
If you fail to login to find the login page or can't login after several trials, terminate.
If login is completed, you're successful."""


@base_router.post(
    "/run/tasks/login",
    tags=["Agent"],
    response_model=WorkflowRunResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "login",
        "x-fern-examples": [
            {
                "code-samples": [
                    {
                        "sdk": "python",
                        "code": LOGIN_CODE_SAMPLE_SKYVERN,
                    },
                    {
                        "sdk": "python",
                        "code": LOGIN_CODE_SAMPLE_BITWARDEN,
                    },
                    {
                        "sdk": "python",
                        "code": LOGIN_CODE_SAMPLE_ONEPASSWORD,
                    },
                ]
            }
        ],
    },
    description="Log in to a website using either credential stored in Skyvern, Bitwarden or 1Password",
    summary="Login Task",
)
async def login(
    request: Request,
    background_tasks: BackgroundTasks,
    login_request: LoginRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> WorkflowRunResponse:
    # 1. create empty workflow with a credential parameter
    new_workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(
        organization,
        "Login",
        proxy_location=login_request.proxy_location,
        max_screenshot_scrolling_times=login_request.max_screenshot_scrolling_times,
        extra_http_headers=login_request.extra_http_headers,
    )
    # 2. add a login block to the workflow
    label = "login"
    yaml_parameters = []
    parameter_key = "credential"
    if login_request.credential_type == CredentialType.skyvern:
        if not login_request.credential_id:
            raise HTTPException(status_code=400, detail="credential_id is required to login with Skyvern credential")
        credential = await app.DATABASE.get_credential(login_request.credential_id, organization.organization_id)
        if not credential:
            raise HTTPException(status_code=404, detail=f"Credential {login_request.credential_id} not found")

        yaml_parameters = [
            WorkflowParameterYAML(
                key=parameter_key,
                workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
                description="The ID of the credential to use for login",
                default_value=login_request.credential_id,
            )
        ]
    elif login_request.credential_type == CredentialType.bitwarden:
        yaml_parameters = [
            BitwardenLoginCredentialParameterYAML(
                key=parameter_key,
                collection_id=login_request.bitwarden_collection_id,
                item_id=login_request.bitwarden_item_id,
                url=login_request.url,
                description="The ID of the bitwarden collection to use for login",
                bitwarden_client_id_aws_secret_key="SKYVERN_BITWARDEN_CLIENT_ID",
                bitwarden_client_secret_aws_secret_key="SKYVERN_BITWARDEN_CLIENT_SECRET",
                bitwarden_master_password_aws_secret_key="SKYVERN_BITWARDEN_MASTER_PASSWORD",
            )
        ]
    elif login_request.credential_type == CredentialType.onepassword:
        if not login_request.onepassword_vault_id:
            raise HTTPException(
                status_code=400, detail="onepassword_vault_id is required to login with 1Password credential"
            )
        if not login_request.onepassword_item_id:
            raise HTTPException(
                status_code=400, detail="onepassword_item_id is required to login with 1Password credential"
            )
        yaml_parameters = [
            OnePasswordCredentialParameterYAML(
                key=parameter_key,
                vault_id=login_request.onepassword_vault_id,
                item_id=login_request.onepassword_item_id,
            )
        ]

    login_block_yaml = LoginBlockYAML(
        label=label,
        title=label,
        url=login_request.url,
        navigation_goal=login_request.prompt or DEFAULT_LOGIN_PROMPT,
        max_steps_per_run=10,
        parameter_keys=[parameter_key],
        totp_verification_url=login_request.totp_url,
        totp_identifier=login_request.totp_identifier,
    )
    yaml_blocks = [login_block_yaml]
    workflow_definition_yaml = WorkflowDefinitionYAML(
        parameters=yaml_parameters,
        blocks=yaml_blocks,
    )
    workflow_create_request = WorkflowCreateYAMLRequest(
        title=new_workflow.title,
        description=new_workflow.description,
        proxy_location=login_request.proxy_location or ProxyLocation.RESIDENTIAL,
        workflow_definition=workflow_definition_yaml,
        status=new_workflow.status,
        max_screenshot_scrolls=login_request.max_screenshot_scrolling_times,
    )
    workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=organization,
        request=workflow_create_request,
        workflow_permanent_id=new_workflow.workflow_permanent_id,
    )
    LOG.info("Workflow created", workflow_id=workflow.workflow_id)

    # 3. create and run workflow with the credential
    workflow_id = new_workflow.workflow_permanent_id
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    legacy_workflow_request = WorkflowRequestBody(
        proxy_location=login_request.proxy_location,
        webhook_callback_url=login_request.webhook_url,
        totp_identifier=login_request.totp_identifier,
        totp_verification_url=login_request.totp_url,
        browser_session_id=login_request.browser_session_id,
        max_screenshot_scrolls=login_request.max_screenshot_scrolling_times,
        extra_http_headers=login_request.extra_http_headers,
    )

    try:
        workflow_run = await workflow_service.run_workflow(
            workflow_id=workflow_id,
            organization=organization,
            workflow_request=legacy_workflow_request,
            template=False,
            version=None,
            api_key=x_api_key or None,
            request_id=request_id,
            request=request,
            background_tasks=background_tasks,
        )
    except MissingBrowserAddressError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return WorkflowRunResponse(
        run_id=workflow_run.workflow_run_id,
        run_type=RunType.workflow_run,
        status=str(workflow_run.status),
        failure_reason=workflow_run.failure_reason,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        run_request=WorkflowRunRequest(
            workflow_id=new_workflow.workflow_id,
            title=new_workflow.title,
            proxy_location=login_request.proxy_location,
            webhook_url=login_request.webhook_url,
            totp_url=login_request.totp_url,
            totp_identifier=login_request.totp_identifier,
            browser_session_id=login_request.browser_session_id,
            max_screenshot_scrolls=login_request.max_screenshot_scrolling_times,
        ),
        app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/workflows/{workflow_run.workflow_permanent_id}/{workflow_run.workflow_run_id}",
        browser_session_id=login_request.browser_session_id,
    )
