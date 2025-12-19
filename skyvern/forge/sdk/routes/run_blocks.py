from typing import Annotated

import structlog
from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request

from skyvern.config import settings
from skyvern.exceptions import MissingBrowserAddressError
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.routes.code_samples import (
    DOWNLOAD_FILES_CODE_SAMPLE_PYTHON,
    DOWNLOAD_FILES_CODE_SAMPLE_TS,
    LOGIN_CODE_SAMPLE_BITWARDEN_PYTHON,
    LOGIN_CODE_SAMPLE_BITWARDEN_TS,
    LOGIN_CODE_SAMPLE_ONEPASSWORD_PYTHON,
    LOGIN_CODE_SAMPLE_ONEPASSWORD_TS,
    LOGIN_CODE_SAMPLE_SKYVERN_PYTHON,
    LOGIN_CODE_SAMPLE_SKYVERN_TS,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody
from skyvern.schemas.run_blocks import BaseRunBlockRequest, CredentialType, DownloadFilesRequest, LoginRequest
from skyvern.schemas.runs import ProxyLocation, RunType, WorkflowRunRequest, WorkflowRunResponse
from skyvern.schemas.workflows import (
    AzureVaultCredentialParameterYAML,
    BitwardenLoginCredentialParameterYAML,
    FileDownloadBlockYAML,
    LoginBlockYAML,
    OnePasswordCredentialParameterYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowParameterYAML,
    WorkflowStatus,
)
from skyvern.services import workflow_service
from skyvern.utils.url_validators import prepend_scheme_and_validate_url

LOG = structlog.get_logger()
DEFAULT_LOGIN_PROMPT = """If you're not on the login page, navigate to login page and login using the credentials given.
First, take actions on promotional popups or cookie prompts that could prevent taking other action on the web page.
If a 2-factor step appears, enter the authentication code.
If you fail to login to find the login page or can't login after several trials, terminate.
If login is completed, you're successful."""


def _validate_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return prepend_scheme_and_validate_url(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def _run_workflow_and_build_response(
    request: Request,
    background_tasks: BackgroundTasks,
    new_workflow: Workflow,
    workflow_id: str,
    organization: Organization,
    run_block_request: BaseRunBlockRequest,
    webhook_url: str | None,
    totp_verification_url: str | None,
    totp_identifier: str | None,
    x_api_key: str | None,
) -> WorkflowRunResponse:
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    legacy_workflow_request = WorkflowRequestBody(
        proxy_location=run_block_request.proxy_location,
        webhook_callback_url=webhook_url,
        totp_identifier=totp_identifier,
        totp_verification_url=totp_verification_url,
        browser_session_id=run_block_request.browser_session_id,
        browser_profile_id=run_block_request.browser_profile_id,
        browser_address=run_block_request.browser_address,
        max_screenshot_scrolls=run_block_request.max_screenshot_scrolling_times,
        extra_http_headers=run_block_request.extra_http_headers,
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
            proxy_location=run_block_request.proxy_location,
            webhook_url=webhook_url,
            totp_url=totp_verification_url,
            totp_identifier=totp_identifier,
            browser_session_id=run_block_request.browser_session_id,
            browser_profile_id=run_block_request.browser_profile_id,
            max_screenshot_scrolls=run_block_request.max_screenshot_scrolling_times,
        ),
        app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}",
        browser_session_id=run_block_request.browser_session_id,
        browser_profile_id=run_block_request.browser_profile_id,
    )


@base_router.post(
    "/run/tasks/login",
    tags=["Agent"],
    response_model=WorkflowRunResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "login",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": LOGIN_CODE_SAMPLE_SKYVERN_PYTHON},
                    {"sdk": "python", "code": LOGIN_CODE_SAMPLE_BITWARDEN_PYTHON},
                    {"sdk": "python", "code": LOGIN_CODE_SAMPLE_ONEPASSWORD_PYTHON},
                    {"sdk": "typescript", "code": LOGIN_CODE_SAMPLE_SKYVERN_TS},
                    {"sdk": "typescript", "code": LOGIN_CODE_SAMPLE_BITWARDEN_TS},
                    {"sdk": "typescript", "code": LOGIN_CODE_SAMPLE_ONEPASSWORD_TS},
                ]
            }
        ],
    },
    description="Log in to a website using either credential stored in Skyvern, Bitwarden, 1Password, or Azure Vault",
    summary="Login Task",
)
async def login(
    request: Request,
    background_tasks: BackgroundTasks,
    login_request: LoginRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> WorkflowRunResponse:
    url = _validate_url(login_request.url)
    totp_verification_url = _validate_url(login_request.totp_url)
    webhook_url = _validate_url(login_request.webhook_url)

    # 1. create empty workflow with a credential parameter
    new_workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(
        organization,
        "Login",
        proxy_location=login_request.proxy_location,
        max_screenshot_scrolling_times=login_request.max_screenshot_scrolling_times,
        extra_http_headers=login_request.extra_http_headers,
        status=WorkflowStatus.auto_generated,
    )
    # 2. add a login block to the workflow
    label = "login"
    yaml_parameters = []
    parameter_key = "credential"
    resolved_totp_identifier = login_request.totp_identifier
    if login_request.credential_type == CredentialType.skyvern:
        if not login_request.credential_id:
            raise HTTPException(status_code=400, detail="credential_id is required to login with Skyvern credential")
        credential = await app.DATABASE.get_credential(login_request.credential_id, organization.organization_id)
        if not credential:
            raise HTTPException(status_code=404, detail=f"Credential {login_request.credential_id} not found")
        if not resolved_totp_identifier:
            resolved_totp_identifier = credential.totp_identifier

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
    elif login_request.credential_type == CredentialType.azure_vault:
        if not login_request.azure_vault_name:
            raise HTTPException(
                status_code=400, detail="azure_vault_name is required to login with Azure Vault credential"
            )
        if not login_request.azure_vault_username_key:
            raise HTTPException(
                status_code=400, detail="azure_vault_username_key is required to login with Azure Vault credential"
            )
        if not login_request.azure_vault_password_key:
            raise HTTPException(
                status_code=400, detail="azure_vault_password_key is required to login with Azure Vault credential"
            )
        yaml_parameters = [
            AzureVaultCredentialParameterYAML(
                key=parameter_key,
                vault_name=login_request.azure_vault_name,
                username_key=login_request.azure_vault_username_key,
                password_key=login_request.azure_vault_password_key,
                totp_secret_key=login_request.azure_vault_totp_secret_key,
            )
        ]

    login_block_yaml = LoginBlockYAML(
        label=label,
        title=label,
        url=url,
        navigation_goal=login_request.prompt or DEFAULT_LOGIN_PROMPT,
        max_steps_per_run=10,
        parameter_keys=[parameter_key],
        totp_verification_url=totp_verification_url,
        totp_identifier=resolved_totp_identifier,
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
    return await _run_workflow_and_build_response(
        request=request,
        background_tasks=background_tasks,
        new_workflow=new_workflow,
        workflow_id=workflow_id,
        organization=organization,
        run_block_request=login_request,
        webhook_url=webhook_url,
        totp_verification_url=totp_verification_url,
        totp_identifier=resolved_totp_identifier,
        x_api_key=x_api_key,
    )


@base_router.post(
    "/run/tasks/download_files",
    tags=["Agent"],
    response_model=WorkflowRunResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "download_files",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": DOWNLOAD_FILES_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": DOWNLOAD_FILES_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Download a file from a website by navigating and clicking download buttons",
    summary="File Download Task",
)
async def download_files(
    request: Request,
    background_tasks: BackgroundTasks,
    download_files_request: DownloadFilesRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> WorkflowRunResponse:
    url = _validate_url(download_files_request.url)
    totp_verification_url = _validate_url(download_files_request.totp_url)
    webhook_url = _validate_url(download_files_request.webhook_url)

    # 1. create empty workflow
    new_workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(
        organization,
        "File Download",
        proxy_location=download_files_request.proxy_location,
        max_screenshot_scrolling_times=download_files_request.max_screenshot_scrolling_times,
        extra_http_headers=download_files_request.extra_http_headers,
        status=WorkflowStatus.auto_generated,
    )

    # 2. add a file download block to the workflow
    label = "file_download"
    file_download_block_yaml = FileDownloadBlockYAML(
        label=label,
        title=label,
        url=url,
        navigation_goal=download_files_request.navigation_goal,
        max_steps_per_run=download_files_request.max_steps_per_run or 10,
        parameter_keys=[],
        totp_verification_url=totp_verification_url,
        totp_identifier=download_files_request.totp_identifier,
        download_suffix=download_files_request.download_suffix,
        download_timeout=download_files_request.download_timeout,
    )
    yaml_blocks = [file_download_block_yaml]
    workflow_definition_yaml = WorkflowDefinitionYAML(
        parameters=[],
        blocks=yaml_blocks,
    )
    workflow_create_request = WorkflowCreateYAMLRequest(
        title=new_workflow.title,
        description=new_workflow.description,
        proxy_location=download_files_request.proxy_location or ProxyLocation.RESIDENTIAL,
        workflow_definition=workflow_definition_yaml,
        status=new_workflow.status,
        max_screenshot_scrolls=download_files_request.max_screenshot_scrolling_times,
    )
    workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=organization,
        request=workflow_create_request,
        workflow_permanent_id=new_workflow.workflow_permanent_id,
    )
    LOG.info("Workflow created", workflow_id=workflow.workflow_id)

    # 3. create and run workflow
    workflow_id = new_workflow.workflow_permanent_id
    return await _run_workflow_and_build_response(
        request=request,
        background_tasks=background_tasks,
        new_workflow=new_workflow,
        workflow_id=workflow_id,
        organization=organization,
        run_block_request=download_files_request,
        webhook_url=webhook_url,
        totp_verification_url=totp_verification_url,
        totp_identifier=download_files_request.totp_identifier,
        x_api_key=x_api_key,
    )
