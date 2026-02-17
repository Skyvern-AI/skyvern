"""Credential management API endpoints.

SECURITY INVARIANT — NO RAW CREDENTIAL RETRIEVAL
=================================================
Credential endpoints must NEVER return sensitive credential data (passwords,
TOTP secrets, full card numbers, CVVs, expiration dates, card holder names,
or secret values) in any API response. The only fields that may be returned
are non-sensitive metadata:

  - Password credentials: ``username``, ``totp_type``, ``totp_identifier``
  - Credit card credentials: ``last_four``, ``brand``
  - Secret credentials: ``secret_label``

This is enforced by the ``*CredentialResponse`` Pydantic models and the
``_convert_to_response()`` helper. When adding new credential types or
modifying existing ones, ensure that:

  1. The response model never includes the raw secret material.
  2. The ``_convert_to_response()`` function only maps non-sensitive fields.
  3. No endpoint (including ``get_credential`` and ``get_credentials``) ever
     fetches and returns the decrypted secret from the vault.

Violating this invariant would allow any caller with a valid API key to
exfiltrate stored passwords, card numbers, and secrets — which is the
exact threat the vault architecture is designed to prevent.
"""

import asyncio
import json

import structlog
from fastapi import BackgroundTasks, Body, Depends, HTTPException, Path, Query

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.routes.code_samples import (
    CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_PYTHON,
    CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_TS,
    CREATE_CREDENTIAL_CODE_SAMPLE_PYTHON,
    CREATE_CREDENTIAL_CODE_SAMPLE_TS,
    DELETE_CREDENTIAL_CODE_SAMPLE_PYTHON,
    DELETE_CREDENTIAL_CODE_SAMPLE_TS,
    GET_CREDENTIAL_CODE_SAMPLE_PYTHON,
    GET_CREDENTIAL_CODE_SAMPLE_TS,
    GET_CREDENTIALS_CODE_SAMPLE_PYTHON,
    GET_CREDENTIALS_CODE_SAMPLE_TS,
    SEND_TOTP_CODE_CODE_SAMPLE_PYTHON,
    SEND_TOTP_CODE_CODE_SAMPLE_TS,
)
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    CreditCardCredentialResponse,
    NonEmptyPasswordCredential,
    PasswordCredentialResponse,
    SecretCredentialResponse,
    TestCredentialRequest,
    TestCredentialResponse,
    TestCredentialStatusResponse,
    TestLoginRequest,
    TestLoginResponse,
    UpdateCredentialRequest,
)
from skyvern.forge.sdk.schemas.organizations import (
    AzureClientSecretCredentialResponse,
    CreateAzureClientSecretCredentialRequest,
    CreateCustomCredentialServiceConfigRequest,
    CreateOnePasswordTokenRequest,
    CreateOnePasswordTokenResponse,
    CustomCredentialServiceConfigResponse,
    Organization,
)
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCode, TOTPCodeCreate
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.bitwarden import BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.schemas.workflows import (
    LoginBlockYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowParameterYAML,
    WorkflowStatus,
)
from skyvern.services.otp_service import OTPValue, parse_otp_login

LOG = structlog.get_logger()


async def fetch_credential_item_background(item_id: str) -> None:
    """
    Background task to fetch the recently added credential item from Bitwarden.
    This triggers Bitwarden to sync the vault earlier so the next request does not have to wait for the sync.
    """
    try:
        LOG.info("Pre-fetching credential item from Bitwarden in background", item_id=item_id)
        credential_item = await BitwardenService.get_credential_item(item_id)
        LOG.info("Successfully fetched credential item from Bitwarden", item_id=item_id, name=credential_item.name)
    except Exception as e:
        LOG.exception("Failed to fetch credential item from Bitwarden in background", item_id=item_id, error=str(e))


@legacy_base_router.post("/totp")
@legacy_base_router.post("/totp/", include_in_schema=False)
@base_router.post(
    "/credentials/totp",
    response_model=TOTPCode,
    summary="Send TOTP code",
    description="Forward a TOTP (2FA, MFA) email or sms message containing the code to Skyvern. This endpoint stores the code in database so that Skyvern can use it while running tasks/workflows.",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "send_totp_code",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": SEND_TOTP_CODE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": SEND_TOTP_CODE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.post(
    "/credentials/totp/",
    response_model=TOTPCode,
    include_in_schema=False,
)
async def send_totp_code(
    data: TOTPCodeCreate,
    curr_org: Organization = Depends(org_auth_service.get_current_org),
) -> TOTPCode:
    LOG.info(
        "Saving OTP code",
        organization_id=curr_org.organization_id,
        totp_identifier=data.totp_identifier,
        task_id=data.task_id,
        workflow_id=data.workflow_id,
        workflow_run_id=data.workflow_run_id,
    )
    # validate task_id, workflow_id, workflow_run_id are valid ids in db if provided
    if data.task_id:
        task = await app.DATABASE.get_task(data.task_id, curr_org.organization_id)
        if not task:
            raise HTTPException(status_code=400, detail=f"Invalid task id: {data.task_id}")
    if data.workflow_id:
        workflow = await app.DATABASE.get_workflow(data.workflow_id, curr_org.organization_id)
        if not workflow:
            raise HTTPException(status_code=400, detail=f"Invalid workflow id: {data.workflow_id}")
    if data.workflow_run_id:
        workflow_run = await app.DATABASE.get_workflow_run(data.workflow_run_id, curr_org.organization_id)
        if not workflow_run:
            raise HTTPException(status_code=400, detail=f"Invalid workflow run id: {data.workflow_run_id}")
    content = data.content.strip()
    otp_value: OTPValue | None = OTPValue(value=content, type=data.type or OTPType.TOTP)
    # We assume the user is sending the code directly when the length of code is less than or equal to 10
    if len(content) > 10:
        otp_value = await parse_otp_login(content, curr_org.organization_id, enforced_otp_type=data.type)

    if not otp_value:
        LOG.error(
            "Failed to parse otp login",
            totp_identifier=data.totp_identifier,
            task_id=data.task_id,
            workflow_id=data.workflow_id,
            workflow_run_id=data.workflow_run_id,
            content=data.content,
        )
        raise HTTPException(status_code=400, detail="Failed to parse otp login")

    return await app.DATABASE.create_otp_code(
        organization_id=curr_org.organization_id,
        totp_identifier=data.totp_identifier,
        content=data.content,
        code=otp_value.value,
        task_id=data.task_id,
        workflow_id=data.workflow_id,
        workflow_run_id=data.workflow_run_id,
        source=data.source,
        expired_at=data.expired_at,
        otp_type=otp_value.get_otp_type(),
    )


@base_router.get(
    "/credentials/totp",
    response_model=list[TOTPCode],
    summary="List TOTP codes",
    description="Retrieves recent TOTP codes for the current organization.",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_totp_codes",
    },
    include_in_schema=False,
)
@base_router.get(
    "/credentials/totp/",
    response_model=list[TOTPCode],
    include_in_schema=False,
)
async def get_totp_codes(
    curr_org: Organization = Depends(org_auth_service.get_current_org),
    totp_identifier: str | None = Query(
        None,
        description="Filter by TOTP identifier such as an email or phone number.",
        examples=["john.doe@example.com"],
    ),
    workflow_run_id: str | None = Query(
        None,
        description="Filter by workflow run ID.",
        examples=["wr_123456"],
    ),
    otp_type: OTPType | None = Query(
        None,
        description="Filter by OTP type (e.g. totp, magic_link).",
        examples=[OTPType.TOTP.value],
    ),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="Maximum number of codes to return.",
    ),
) -> list[TOTPCode]:
    codes = await app.DATABASE.get_recent_otp_codes(
        organization_id=curr_org.organization_id,
        limit=limit,
        valid_lifespan_minutes=None,
        otp_type=otp_type,
        workflow_run_id=workflow_run_id,
        totp_identifier=totp_identifier,
    )

    return codes


@legacy_base_router.post("/credentials")
@legacy_base_router.post("/credentials/", include_in_schema=False)
@base_router.post(
    "/credentials",
    response_model=CredentialResponse,
    status_code=201,
    summary="Create credential",
    description="Creates a new credential for the current organization",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "create_credential",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": CREATE_CREDENTIAL_CODE_SAMPLE_PYTHON},
                    {"sdk": "python", "code": CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_PYTHON},
                    {"sdk": "typescript", "code": CREATE_CREDENTIAL_CODE_SAMPLE_TS},
                    {"sdk": "typescript", "code": CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD_TS},
                ]
            }
        ],
    },
)
@base_router.post(
    "/credentials/",
    response_model=CredentialResponse,
    status_code=201,
    include_in_schema=False,
)
async def create_credential(
    background_tasks: BackgroundTasks,
    data: CreateCredentialRequest = Body(
        ...,
        description="The credential data to create",
        example={
            "name": "My Credential",
            "credential_type": "PASSWORD",
            "credential": {"username": "user@example.com", "password": "securepassword123", "totp": "JBSWY3DPEHPK3PXP"},
        },
        openapi_extra={"x-fern-sdk-parameter-name": "data"},
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CredentialResponse:
    credential_service = await _get_credential_vault_service()

    credential = await credential_service.create_credential(organization_id=current_org.organization_id, data=data)

    if credential.vault_type == CredentialVaultType.BITWARDEN:
        # Early resyncing the Bitwarden vault
        background_tasks.add_task(fetch_credential_item_background, credential.item_id)

    if data.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=data.credential.username,
            totp_type=data.credential.totp_type if hasattr(data.credential, "totp_type") else "none",
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=data.credential_type,
            name=data.name,
        )
    elif data.credential_type == CredentialType.CREDIT_CARD:
        credential_response = CreditCardCredentialResponse(
            last_four=data.credential.card_number[-4:],
            brand=data.credential.card_brand,
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=data.credential_type,
            name=data.name,
        )
    elif data.credential_type == CredentialType.SECRET:
        credential_response = SecretCredentialResponse(secret_label=data.credential.secret_label)
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=data.credential_type,
            name=data.name,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported credential type: {data.credential_type}")



@base_router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialResponse,
    summary="Rename credential",
    description="Updates a credential's metadata (e.g. name) without changing the stored secret.",
    tags=["Credentials"],
)
@base_router.patch(
    "/credentials/{credential_id}/",
    response_model=CredentialResponse,
    include_in_schema=False,
)
async def rename_credential(
    credential_id: str = Path(
        ...,
        description="The unique identifier of the credential to update",
        examples=["cred_1234567890"],
    ),
    data: UpdateCredentialRequest = Body(
        ...,
        description="The credential fields to update",
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CredentialResponse:
    credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not credential:
        raise HTTPException(status_code=404, detail=f"Credential not found, credential_id={credential_id}")

    updated = await app.DATABASE.update_credential(
        credential_id=credential_id,
        organization_id=current_org.organization_id,
        name=data.name,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update credential")

    return _convert_to_response(updated)


@legacy_base_router.put("/credentials/{credential_id}")
@legacy_base_router.put("/credentials/{credential_id}/", include_in_schema=False)
@base_router.post(
    "/credentials/{credential_id}/update",
    response_model=CredentialResponse,
    summary="Update credential",
    description="Overwrites the stored credential data (e.g. username/password) while keeping the same credential_id.",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "update_credential",
    },
)
@base_router.post(
    "/credentials/{credential_id}/update/",
    response_model=CredentialResponse,
    include_in_schema=False,
)
async def update_credential(
    background_tasks: BackgroundTasks,
    credential_id: str = Path(
        ...,
        description="The unique identifier of the credential to update",
        examples=["cred_1234567890"],
        openapi_extra={"x-fern-sdk-parameter-name": "credential_id"},
    ),
    data: CreateCredentialRequest = Body(
        ...,
        description="The new credential data to store",
        example={
            "name": "My Credential",
            "credential_type": "PASSWORD",
            "credential": {"username": "user@example.com", "password": "newpassword123"},
        },
        openapi_extra={"x-fern-sdk-parameter-name": "data"},
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CredentialResponse:
    existing_credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not existing_credential:
        raise HTTPException(status_code=404, detail=f"Credential not found, credential_id={credential_id}")

    vault_type = existing_credential.vault_type or CredentialVaultType.BITWARDEN
    credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
    if not credential_service:
        raise HTTPException(status_code=400, detail="Unsupported credential storage type")

    old_item_id = existing_credential.item_id

    updated_credential = await credential_service.update_credential(
        credential=existing_credential,
        data=data,
    )

    # Schedule background cleanup of old vault item if the item_id changed
    if old_item_id != updated_credential.item_id:
        background_tasks.add_task(
            credential_service.post_delete_credential_item,
            old_item_id,
            existing_credential.organization_id,
        )

    if updated_credential.vault_type == CredentialVaultType.BITWARDEN:
        background_tasks.add_task(fetch_credential_item_background, updated_credential.item_id)

    return _convert_to_response(updated_credential)


@legacy_base_router.delete("/credentials/{credential_id}")
@legacy_base_router.delete("/credentials/{credential_id}/", include_in_schema=False)
@base_router.post(
    "/credentials/{credential_id}/delete",
    status_code=204,
    summary="Delete credential",
    description="Deletes a specific credential by its ID",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "delete_credential",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": DELETE_CREDENTIAL_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": DELETE_CREDENTIAL_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.post(
    "/credentials/{credential_id}/delete/",
    status_code=204,
    include_in_schema=False,
)
async def delete_credential(
    background_tasks: BackgroundTasks,
    credential_id: str = Path(
        ...,
        description="The unique identifier of the credential to delete",
        examples=["cred_1234567890"],
        openapi_extra={"x-fern-sdk-parameter-name": "credential_id"},
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not credential:
        raise HTTPException(status_code=404, detail=f"Credential not found, credential_id={credential_id}")

    vault_type = credential.vault_type or CredentialVaultType.BITWARDEN
    credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
    if not credential_service:
        raise HTTPException(status_code=400, detail="Unsupported credential storage type")

    await credential_service.delete_credential(credential)

    # Schedule background cleanup if the service implements it
    if vault_type != CredentialVaultType.CUSTOM:
        background_tasks.add_task(
            credential_service.post_delete_credential_item,
            credential.item_id,
            credential.organization_id,
        )

    return None


@legacy_base_router.get("/credentials/{credential_id}")
@legacy_base_router.get("/credentials/{credential_id}/", include_in_schema=False)
@base_router.get(
    "/credentials/{credential_id}",
    response_model=CredentialResponse,
    summary="Get credential by ID",
    description="Retrieves a specific credential by its ID",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_credential",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_CREDENTIAL_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_CREDENTIAL_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.get(
    "/credentials/{credential_id}/",
    response_model=CredentialResponse,
    include_in_schema=False,
)
async def get_credential(
    credential_id: str = Path(
        ...,
        description="The unique identifier of the credential",
        examples=["cred_1234567890"],
        openapi_extra={"x-fern-sdk-parameter-name": "credential_id"},
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CredentialResponse:
    """Return non-sensitive metadata for a single credential.

    SECURITY: This endpoint intentionally does NOT return the raw secret
    material (password, card number, CVV, secret value, etc.). Only
    non-sensitive fields are included in the response. See the module
    docstring for the full security invariant.
    """
    credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    return _convert_to_response(credential)


@legacy_base_router.get("/credentials")
@legacy_base_router.get("/credentials/", include_in_schema=False)
@base_router.get(
    "/credentials",
    response_model=list[CredentialResponse],
    summary="Get all credentials",
    description="Retrieves a paginated list of credentials for the current organization",
    tags=["Credentials"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_credentials",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_CREDENTIALS_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_CREDENTIALS_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.get(
    "/credentials/",
    response_model=list[CredentialResponse],
    include_in_schema=False,
)
async def get_credentials(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(
        1,
        ge=1,
        description="Page number for pagination",
        examples=[1],
        openapi_extra={"x-fern-sdk-parameter-name": "page"},
    ),
    page_size: int = Query(
        10,
        ge=1,
        description="Number of items per page",
        examples=[10],
        openapi_extra={"x-fern-sdk-parameter-name": "page_size"},
    ),
) -> list[CredentialResponse]:
    """Return non-sensitive metadata for all credentials (paginated).

    SECURITY: Like ``get_credential``, this endpoint never returns raw secret
    material. See the module docstring for the full security invariant.
    """
    credentials = await app.DATABASE.get_credentials(current_org.organization_id, page=page, page_size=page_size)
    return [_convert_to_response(credential) for credential in credentials]


@base_router.get(
    "/credentials/onepassword/get",
    response_model=CreateOnePasswordTokenResponse,
    summary="Get OnePassword service account token",
    description="Retrieves the current OnePassword service account token for the organization.",
    include_in_schema=False,
)
@base_router.get(
    "/credentials/onepassword/get/",
    response_model=CreateOnePasswordTokenResponse,
    include_in_schema=False,
)
async def get_onepassword_token(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateOnePasswordTokenResponse:
    """
    Get the current OnePassword service account token for the organization.
    """
    try:
        auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.onepassword_service_account.value,
        )
        if not auth_token:
            raise HTTPException(
                status_code=404,
                detail="No OnePassword service account token found for this organization",
            )

        return CreateOnePasswordTokenResponse(token=auth_token)

    except HTTPException:
        raise
    except Exception as e:
        LOG.error(
            "Failed to get OnePassword service account token",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get OnePassword service account token: {str(e)}",
        )


@base_router.post(
    "/credentials/onepassword/create",
    response_model=CreateOnePasswordTokenResponse,
    summary="Create or update OnePassword service account token",
    description="Creates or updates a OnePassword service account token for the current organization. Only one valid token is allowed per organization.",
    include_in_schema=False,
)
@base_router.post(
    "/credentials/onepassword/create/",
    response_model=CreateOnePasswordTokenResponse,
    include_in_schema=False,
)
async def update_onepassword_token(
    data: CreateOnePasswordTokenRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CreateOnePasswordTokenResponse:
    """
    Create or update a OnePassword service account token for the current organization.

    This endpoint ensures only one valid OnePassword token exists per organization.
    If a valid token already exists, it will be invalidated before creating the new one.
    """
    try:
        # Invalidate any existing valid OnePassword tokens for this organization
        await app.DATABASE.invalidate_org_auth_tokens(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.onepassword_service_account,
        )

        # Create the new token
        auth_token = await app.DATABASE.create_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.onepassword_service_account,
            token=data.token,
        )

        LOG.info(
            "Created or updated OnePassword service account token",
            organization_id=current_org.organization_id,
            token_id=auth_token.id,
        )

        return CreateOnePasswordTokenResponse(token=auth_token)

    except Exception as e:
        LOG.error(
            "Failed to create or update OnePassword service account token",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create or update OnePassword service account token: {str(e)}",
        )


@base_router.get(
    "/credentials/azure_credential/get",
    response_model=AzureClientSecretCredentialResponse,
    summary="Get Azure Client Secret Credential",
    description="Retrieves the current Azure Client Secret Credential for the organization.",
    include_in_schema=False,
)
@base_router.get(
    "/credentials/azure_credential/get/",
    response_model=AzureClientSecretCredentialResponse,
    include_in_schema=False,
)
async def get_azure_client_secret_credential(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> AzureClientSecretCredentialResponse:
    """
    Get the current Azure Client Secret Credential for the organization.
    """
    try:
        auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.azure_client_secret_credential.value,
        )
        if not auth_token:
            raise HTTPException(
                status_code=404,
                detail="No Azure Client Secret Credential found for this organization",
            )

        return AzureClientSecretCredentialResponse(token=auth_token)

    except HTTPException:
        raise
    except Exception as e:
        LOG.error(
            "Failed to get Azure Client Secret Credential",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get Azure Client Secret Credential: {str(e)}",
        )


@base_router.post(
    "/credentials/azure_credential/create",
    response_model=AzureClientSecretCredentialResponse,
    summary="Create or update Azure Client Secret Credential",
    description="Creates or updates a Azure Client Secret Credential for the current organization. Only one valid record is allowed per organization.",
    include_in_schema=False,
)
@base_router.post(
    "/credentials/azure_credential/create/",
    response_model=AzureClientSecretCredentialResponse,
    include_in_schema=False,
)
async def update_azure_client_secret_credential(
    request: CreateAzureClientSecretCredentialRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> AzureClientSecretCredentialResponse:
    """
    Create or update an Azure Client Secret Credential for the current organization.

    This endpoint ensures only one valid Azure Client Secret Credential exists per organization.
    If a valid token already exists, it will be invalidated before creating the new one.
    """
    try:
        # Invalidate any existing valid Azure Client Secret Credential for this organization
        await app.DATABASE.invalidate_org_auth_tokens(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.azure_client_secret_credential,
        )

        # Create the new Azure token
        auth_token = await app.DATABASE.create_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.azure_client_secret_credential,
            token=request.credential,
        )

        LOG.info(
            "Created or updated Azure Client Secret Credential",
            organization_id=current_org.organization_id,
            token_id=auth_token.id,
        )

        return AzureClientSecretCredentialResponse(token=auth_token)

    except Exception as e:
        LOG.error(
            "Failed to create or update Azure Client Secret Credential",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create or update Azure Client Secret Credential: {str(e)}",
        )


@base_router.get(
    "/credentials/custom_credential/get",
    response_model=CustomCredentialServiceConfigResponse,
    summary="Get Custom Credential Service Configuration",
    description="Retrieves the current custom credential service configuration for the organization.",
    include_in_schema=False,
)
@base_router.get(
    "/credentials/custom_credential/get/",
    response_model=CustomCredentialServiceConfigResponse,
    include_in_schema=False,
)
async def get_custom_credential_service_config(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CustomCredentialServiceConfigResponse:
    """
    Get the current custom credential service configuration for the organization.
    """
    try:
        auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.custom_credential_service.value,
        )
        if not auth_token:
            raise HTTPException(
                status_code=404,
                detail="No custom credential service configuration found for this organization",
            )

        return CustomCredentialServiceConfigResponse(token=auth_token)

    except HTTPException:
        raise
    except Exception as e:
        LOG.error(
            "Failed to get custom credential service configuration",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get custom credential service configuration: {e!s}",
        ) from e


@base_router.post(
    "/credentials/custom_credential/create",
    response_model=CustomCredentialServiceConfigResponse,
    summary="Create or update Custom Credential Service Configuration",
    description="Creates or updates a custom credential service configuration for the current organization. Only one valid configuration is allowed per organization.",
    include_in_schema=False,
)
@base_router.post(
    "/credentials/custom_credential/create/",
    response_model=CustomCredentialServiceConfigResponse,
    include_in_schema=False,
)
async def update_custom_credential_service_config(
    request: CreateCustomCredentialServiceConfigRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CustomCredentialServiceConfigResponse:
    """
    Create or update a custom credential service configuration for the current organization.

    This endpoint ensures only one valid custom credential service configuration exists per organization.
    If a valid configuration already exists, it will be invalidated before creating the new one.
    """
    try:
        # Invalidate any existing valid custom credential service configuration for this organization
        await app.DATABASE.invalidate_org_auth_tokens(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.custom_credential_service,
        )

        # Store the configuration as JSON in the token field
        config_json = json.dumps(request.config.model_dump())

        # Create the new configuration
        auth_token = await app.DATABASE.create_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.custom_credential_service,
            token=config_json,
        )

        LOG.info(
            "Created or updated custom credential service configuration",
            organization_id=current_org.organization_id,
            token_id=auth_token.id,
        )

        return CustomCredentialServiceConfigResponse(token=auth_token)

    except Exception as e:
        LOG.error(
            "Failed to create or update custom credential service configuration",
            organization_id=current_org.organization_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create or update custom credential service configuration: {e!s}",
        ) from e


DEFAULT_LOGIN_PROMPT = (
    "Navigate to the login page if needed and log in with the provided credentials. "
    "Fill in the username and password fields and submit the form. "
    "After submitting, verify whether the login was successful by checking the page content. "
    "IMPORTANT: Do NOT retry or re-submit the login form if the first attempt fails. "
    "If you see an error message such as 'invalid credentials', 'incorrect password', "
    "or 'account locked', report the failure immediately and stop. "
    "Only attempt to log in once."
)

BROWSER_PROFILE_LOGIN_PROMPT = (
    "A browser profile with saved session data has been loaded. "
    "FIRST, check whether you are already logged in by examining the page content. "
    "Look for signs of an authenticated session such as a dashboard, welcome message, "
    "user menu, profile icon, or any content that indicates a logged-in state. "
    "If you are already logged in, report success immediately — do NOT interact with "
    "any form fields or attempt to log in again. "
    "Only if the page clearly shows a login form and you are NOT logged in, "
    "then log in with the provided credentials. Fill in the username and password fields "
    "and submit the form. After submitting, verify whether the login was successful. "
    "IMPORTANT: Do NOT retry or re-submit the login form if the first attempt fails. "
    "If you see an error message such as 'invalid credentials', 'incorrect password', "
    "or 'account locked', report the failure immediately and stop. "
    "Only attempt to log in once."
)


@base_router.post(
    "/credentials/test-login",
    response_model=TestLoginResponse,
    summary="Test login with inline credentials",
    description=(
        "Test a login by providing credentials inline (no saved credential required). "
        "Creates a temporary credential, runs a login test, and returns a workflow run ID to poll."
    ),
    tags=["Credentials"],
)
@base_router.post(
    "/credentials/test-login/",
    response_model=TestLoginResponse,
    include_in_schema=False,
)
async def test_login(
    background_tasks: BackgroundTasks,
    data: TestLoginRequest = Body(
        ...,
        description="The login credentials and URL to test",
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TestLoginResponse:
    """Test a login with inline credentials without requiring a saved credential."""
    organization_id = current_org.organization_id

    # Create a temporary credential
    create_request = CreateCredentialRequest(
        name=f"_test_login_{data.username}",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(
            username=data.username,
            password=data.password,
            totp=data.totp,
            totp_type=data.totp_type,
            totp_identifier=data.totp_identifier,
        ),
    )

    credential_service = await _get_credential_vault_service()
    credential = await credential_service.create_credential(
        organization_id=organization_id,
        data=create_request,
    )

    if credential.vault_type == CredentialVaultType.BITWARDEN:
        background_tasks.add_task(fetch_credential_item_background, credential.item_id)

    credential_id = credential.credential_id

    LOG.info(
        "Testing login with inline credentials",
        credential_id=credential_id,
        organization_id=organization_id,
        url=data.url,
    )

    # Build a login workflow (no browser profile saving for test-only)
    parameter_key = "credential"
    label = "login"

    yaml_parameters = [
        WorkflowParameterYAML(
            key=parameter_key,
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            description="The credential to test",
            default_value=credential_id,
        )
    ]

    login_block_yaml = LoginBlockYAML(
        label=label,
        title=label,
        url=data.url,
        navigation_goal=DEFAULT_LOGIN_PROMPT,
        max_steps_per_run=10,
        parameter_keys=[parameter_key],
        totp_verification_url=None,
        totp_identifier=data.totp_identifier,
    )

    workflow_definition_yaml = WorkflowDefinitionYAML(
        parameters=yaml_parameters,
        blocks=[login_block_yaml],
    )

    workflow_create_request = WorkflowCreateYAMLRequest(
        title=f"Login Test - {data.username}",
        description="Auto-generated workflow to test login credentials",
        persist_browser_session=True,
        workflow_definition=workflow_definition_yaml,
        status=WorkflowStatus.auto_generated,
    )

    workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=current_org,
        request=workflow_create_request,
    )

    run_request = WorkflowRequestBody()

    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=None,
        workflow_request=run_request,
        workflow_permanent_id=workflow.workflow_permanent_id,
        organization=current_org,
        max_steps_override=None,
    )

    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=None,
        background_tasks=background_tasks,
        organization=current_org,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )

    # Schedule background task to create browser profile after the workflow completes
    asyncio.create_task(
        _create_browser_profile_after_workflow(
            credential_id=credential_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow_run.workflow_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=organization_id,
            credential_name=f"_test_login_{data.username}",
            test_url=data.url,
        )
    )

    LOG.info(
        "Login test started",
        credential_id=credential_id,
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )

    return TestLoginResponse(
        credential_id=credential_id,
        workflow_run_id=workflow_run.workflow_run_id,
        status="running",
    )


@base_router.post(
    "/credentials/{credential_id}/test",
    response_model=TestCredentialResponse,
    summary="Test a credential",
    description=(
        "Test a credential by running a login task against the specified URL. "
        "Optionally saves the browser profile after a successful login for reuse in workflows."
    ),
    tags=["Credentials"],
)
@base_router.post(
    "/credentials/{credential_id}/test/",
    response_model=TestCredentialResponse,
    include_in_schema=False,
)
async def test_credential(
    background_tasks: BackgroundTasks,
    credential_id: str = Path(
        ...,
        description="The credential ID to test",
        examples=["cred_1234567890"],
    ),
    data: TestCredentialRequest = Body(
        ...,
        description="Test configuration including the login URL",
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TestCredentialResponse:
    organization_id = current_org.organization_id

    # Validate credential exists and is a password type
    credential = await app.DATABASE.get_credential(credential_id=credential_id, organization_id=organization_id)
    if not credential:
        raise HTTPException(status_code=404, detail=f"Credential {credential_id} not found")
    if credential.credential_type != CredentialType.PASSWORD:
        raise HTTPException(
            status_code=400,
            detail="Only password credentials can be tested with login",
        )

    # Check if the credential already has a browser profile — if so, the agent should
    # first check whether the saved session is still valid before attempting to log in.
    existing_browser_profile_id = credential.browser_profile_id
    if existing_browser_profile_id:
        # Verify the browser profile still exists in storage
        profile = await app.DATABASE.get_browser_profile(
            profile_id=existing_browser_profile_id,
            organization_id=organization_id,
        )
        if not profile:
            LOG.warning(
                "Credential has browser_profile_id but profile not found, ignoring",
                credential_id=credential_id,
                browser_profile_id=existing_browser_profile_id,
            )
            existing_browser_profile_id = None

    LOG.info(
        "Testing credential",
        credential_id=credential_id,
        organization_id=organization_id,
        url=data.url,
        save_browser_profile=data.save_browser_profile,
        existing_browser_profile_id=existing_browser_profile_id,
    )

    # Choose the appropriate prompt: if a browser profile exists, instruct the agent
    # to check for an existing session before attempting login.
    navigation_goal = BROWSER_PROFILE_LOGIN_PROMPT if existing_browser_profile_id else DEFAULT_LOGIN_PROMPT

    # Build a login workflow
    parameter_key = "credential"
    label = "login"

    yaml_parameters = [
        WorkflowParameterYAML(
            key=parameter_key,
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            description="The credential to test",
            default_value=credential_id,
        )
    ]

    login_block_yaml = LoginBlockYAML(
        label=label,
        title=label,
        url=data.url,
        navigation_goal=navigation_goal,
        max_steps_per_run=10,
        parameter_keys=[parameter_key],
        totp_verification_url=None,
        totp_identifier=credential.totp_identifier,
    )

    workflow_definition_yaml = WorkflowDefinitionYAML(
        parameters=yaml_parameters,
        blocks=[login_block_yaml],
    )

    workflow_create_request = WorkflowCreateYAMLRequest(
        title=f"Credential Test - {credential.name}",
        description=f"Auto-generated workflow to test credential {credential_id}",
        persist_browser_session=data.save_browser_profile,
        workflow_definition=workflow_definition_yaml,
        status=WorkflowStatus.auto_generated,
    )

    # Create the workflow
    workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=current_org,
        request=workflow_create_request,
    )

    # Run the workflow — pass existing browser_profile_id so the browser launches
    # with saved session data (cookies, storage) before the agent starts acting.
    run_request = WorkflowRequestBody(
        browser_profile_id=existing_browser_profile_id,
    )

    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=None,
        workflow_request=run_request,
        workflow_permanent_id=workflow.workflow_permanent_id,
        organization=current_org,
        max_steps_override=None,
    )

    # Execute the workflow via the async executor (cloud version enqueues to a worker)
    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=None,
        background_tasks=background_tasks,
        organization=current_org,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )

    # If save_browser_profile is requested, schedule a background task to create
    # the browser profile after the workflow completes. This avoids the race
    # condition of doing it in the GET status endpoint (which could create
    # duplicate profiles when polled rapidly).
    if data.save_browser_profile:
        asyncio.create_task(
            _create_browser_profile_after_workflow(
                credential_id=credential_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_id=workflow_run.workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=organization_id,
                credential_name=credential.name,
                test_url=data.url,
            )
        )

    LOG.info(
        "Credential test started",
        credential_id=credential_id,
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )

    return TestCredentialResponse(
        credential_id=credential_id,
        workflow_run_id=workflow_run.workflow_run_id,
        status="running",
    )


@base_router.get(
    "/credentials/{credential_id}/test/{workflow_run_id}",
    response_model=TestCredentialStatusResponse,
    summary="Get credential test status",
    description=(
        "Poll the status of a credential test. When the test completes successfully "
        "and save_browser_profile was enabled, a browser profile will be automatically "
        "created and linked to the credential."
    ),
    tags=["Credentials"],
)
@base_router.get(
    "/credentials/{credential_id}/test/{workflow_run_id}/",
    response_model=TestCredentialStatusResponse,
    include_in_schema=False,
)
async def get_test_credential_status(
    credential_id: str = Path(
        ...,
        description="The credential ID being tested",
        examples=["cred_1234567890"],
    ),
    workflow_run_id: str = Path(
        ...,
        description="The workflow run ID from the test initiation",
        examples=["wr_1234567890"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TestCredentialStatusResponse:
    organization_id = current_org.organization_id

    # Validate credential exists
    credential = await app.DATABASE.get_credential(credential_id=credential_id, organization_id=organization_id)
    if not credential:
        raise HTTPException(status_code=404, detail=f"Credential {credential_id} not found")

    # Get workflow run status
    workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        raise HTTPException(status_code=404, detail=f"Workflow run {workflow_run_id} not found")

    status = str(workflow_run.status)
    browser_profile_id = credential.browser_profile_id
    tested_url = credential.tested_url
    browser_profile_failure_reason: str | None = None

    # Check workflow_run for browser_profile_failure_reason if the test completed
    # but the background profile creation failed (stored in workflow_run metadata).
    if status == "completed" and not browser_profile_id:
        # The background task may still be running. If it has already failed,
        # the failure reason will have been stored on the credential.
        # For now we just report the current state — no side effects.
        pass

    # Build a detailed failure reason for login failures
    failure_reason = workflow_run.failure_reason
    if status == "failed" and not failure_reason:
        failure_reason = "The login test failed. The credentials may be incorrect or the login page may have changed."
    elif status == "timed_out":
        failure_reason = failure_reason or (
            "The login test timed out. The page may be slow to load or the login flow may require additional steps."
        )
    elif status == "terminated":
        failure_reason = failure_reason or "The login test was terminated before it could complete."
    elif status == "canceled":
        failure_reason = failure_reason or "The login test was canceled."

    return TestCredentialStatusResponse(
        credential_id=credential_id,
        workflow_run_id=workflow_run_id,
        status=status,
        failure_reason=failure_reason,
        browser_profile_id=browser_profile_id,
        tested_url=tested_url,
        browser_profile_failure_reason=browser_profile_failure_reason,
    )


async def _create_browser_profile_after_workflow(
    credential_id: str,
    workflow_run_id: str,
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    credential_name: str,
    test_url: str,
) -> None:
    """Background task that polls the workflow run status and creates a browser
    profile from the persisted session when the run completes successfully.

    This runs as an ``asyncio.Task`` launched from the POST endpoint so that
    the GET status endpoint stays read-only and race-free.
    """
    max_polls = 120  # ~10 minutes at 5s intervals
    poll_interval = 5

    try:
        for _ in range(max_polls):
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id, organization_id=organization_id
            )
            if not workflow_run:
                LOG.warning(
                    "Workflow run not found during browser profile creation poll",
                    credential_id=credential_id,
                    workflow_run_id=workflow_run_id,
                )
                return

            status = str(workflow_run.status)
            if status not in ("completed", "failed", "terminated", "timed_out", "canceled"):
                await asyncio.sleep(poll_interval)
                continue

            if status != "completed":
                LOG.info(
                    "Workflow run did not complete successfully, skipping browser profile creation",
                    credential_id=credential_id,
                    workflow_run_id=workflow_run_id,
                    status=status,
                )
                return

            # Workflow completed — wait for session data to be persisted
            session_dir = None
            max_retries = 5
            for attempt in range(max_retries):
                session_dir = await app.STORAGE.retrieve_browser_session(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                )
                if session_dir:
                    break
                if attempt < max_retries - 1:
                    LOG.info(
                        "Browser session not yet persisted, retrying",
                        credential_id=credential_id,
                        workflow_run_id=workflow_run_id,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    await asyncio.sleep(2)

            if not session_dir:
                LOG.warning(
                    "No persisted session found after retries for credential test workflow",
                    credential_id=credential_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_permanent_id,
                    max_retries=max_retries,
                )
                return

            # Create the browser profile in DB
            profile_name = f"Profile - {credential_name} ({credential_id})"
            profile = await app.DATABASE.create_browser_profile(
                organization_id=organization_id,
                name=profile_name,
                description=f"Browser profile from credential test for {credential_name}",
            )

            # Copy session data to the browser profile storage location
            await app.STORAGE.store_browser_profile(
                organization_id=organization_id,
                profile_id=profile.browser_profile_id,
                directory=session_dir,
            )

            # Link browser profile to credential
            await app.DATABASE.update_credential(
                credential_id=credential_id,
                organization_id=organization_id,
                browser_profile_id=profile.browser_profile_id,
                tested_url=test_url,
            )

            LOG.info(
                "Browser profile created from credential test",
                credential_id=credential_id,
                browser_profile_id=profile.browser_profile_id,
                workflow_run_id=workflow_run_id,
            )
            return

        LOG.warning(
            "Timed out waiting for workflow run to complete for browser profile creation",
            credential_id=credential_id,
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        LOG.exception(
            "Failed to create browser profile from credential test",
            credential_id=credential_id,
            workflow_run_id=workflow_run_id,
        )


async def _get_credential_vault_service() -> CredentialVaultService:
    if settings.CREDENTIAL_VAULT_TYPE == CredentialVaultType.BITWARDEN:
        return app.BITWARDEN_CREDENTIAL_VAULT_SERVICE
    elif settings.CREDENTIAL_VAULT_TYPE == CredentialVaultType.AZURE_VAULT:
        if not app.AZURE_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Azure Vault credential is not supported")
        return app.AZURE_CREDENTIAL_VAULT_SERVICE
    elif settings.CREDENTIAL_VAULT_TYPE == CredentialVaultType.CUSTOM:
        if not app.CUSTOM_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Custom credential vault is not supported")
        return app.CUSTOM_CREDENTIAL_VAULT_SERVICE
    else:
        raise HTTPException(status_code=400, detail="Credential storage not supported")


def _convert_to_response(credential: Credential) -> CredentialResponse:
    """Convert an internal ``Credential`` to a safe API response.

    SECURITY: This function must ONLY copy non-sensitive metadata into the
    response. Never include passwords, TOTP secrets, full card numbers, CVVs,
    expiration dates, card holder names, or secret values. See the module
    docstring for the full security invariant.
    """
    if credential.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=credential.username or credential.credential_id,
            totp_type=credential.totp_type,
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            browser_profile_id=credential.browser_profile_id,
            tested_url=credential.tested_url,
        )
    elif credential.credential_type == CredentialType.CREDIT_CARD:
        credential_response = CreditCardCredentialResponse(
            last_four=credential.card_last4 or "****",
            brand=credential.card_brand or "Card Brand",
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            browser_profile_id=credential.browser_profile_id,
            tested_url=credential.tested_url,
        )
    elif credential.credential_type == CredentialType.SECRET:
        credential_response = SecretCredentialResponse(secret_label=credential.secret_label)
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            browser_profile_id=credential.browser_profile_id,
            tested_url=credential.tested_url,
        )
    else:
        raise HTTPException(status_code=400, detail="Credential type not supported")
