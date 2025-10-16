import structlog
from fastapi import BackgroundTasks, Body, Depends, HTTPException, Path, Query

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.code_samples import (
    CREATE_CREDENTIAL_CODE_SAMPLE,
    CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD,
    DELETE_CREDENTIAL_CODE_SAMPLE,
    GET_CREDENTIAL_CODE_SAMPLE,
    GET_CREDENTIALS_CODE_SAMPLE,
    SEND_TOTP_CODE_CODE_SAMPLE,
)
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    CreditCardCredentialResponse,
    PasswordCredentialResponse,
)
from skyvern.forge.sdk.schemas.organizations import (
    AzureClientSecretCredentialResponse,
    CreateAzureClientSecretCredentialRequest,
    CreateOnePasswordTokenRequest,
    CreateOnePasswordTokenResponse,
    Organization,
)
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCode, TOTPCodeCreate
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.bitwarden import BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
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
        "x-fern-examples": [{"code-samples": [{"sdk": "python", "code": SEND_TOTP_CODE_CODE_SAMPLE}]}],
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
    content = data.content.strip()
    otp_value: OTPValue | None = OTPValue(value=content, type=OTPType.TOTP)
    # We assume the user is sending the code directly when the length of code is less than or equal to 10
    if len(content) > 10:
        otp_value = await parse_otp_login(content, curr_org.organization_id)

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
                    {"sdk": "python", "code": CREATE_CREDENTIAL_CODE_SAMPLE},
                    {"sdk": "python", "code": CREATE_CREDENTIAL_CODE_SAMPLE_CREDIT_CARD},
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
    credential_service = await _get_credential_vault_service(current_org.organization_id)

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
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported credential type: {data.credential_type}")


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
        "x-fern-examples": [{"code-samples": [{"sdk": "python", "code": DELETE_CREDENTIAL_CODE_SAMPLE}]}],
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
    background_tasks.add_task(credential_service.post_delete_credential_item, credential.item_id)

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
        "x-fern-examples": [{"code-samples": [{"sdk": "python", "code": GET_CREDENTIAL_CODE_SAMPLE}]}],
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
    credential_service = await _get_credential_vault_service(current_org.organization_id)

    return await credential_service.get_credential(current_org.organization_id, credential_id)


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
        "x-fern-examples": [{"code-samples": [{"sdk": "python", "code": GET_CREDENTIALS_CODE_SAMPLE}]}],
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
    credential_service = await _get_credential_vault_service(current_org.organization_id)

    return await credential_service.get_credentials(current_org.organization_id, page, page_size)


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


async def _get_credential_vault_service(organization_id: str) -> CredentialVaultService:
    org_collection = await app.DATABASE.get_organization_bitwarden_collection(organization_id)

    if settings.CREDENTIAL_VAULT_TYPE == CredentialVaultType.BITWARDEN or org_collection:
        return app.BITWARDEN_CREDENTIAL_VAULT_SERVICE
    elif settings.CREDENTIAL_VAULT_TYPE == CredentialVaultType.AZURE_VAULT:
        if not app.AZURE_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Azure Vault credential is not supported")
        return app.AZURE_CREDENTIAL_VAULT_SERVICE
    else:
        raise HTTPException(status_code=400, detail="Credential storage not supported")
