import structlog
from fastapi import Body, Depends, HTTPException, Path, Query

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
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
    CreditCardCredentialResponse,
    PasswordCredentialResponse,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.totp_codes import TOTPCode, TOTPCodeCreate
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.bitwarden import BitwardenService

LOG = structlog.get_logger()


async def parse_totp_code(content: str) -> str | None:
    prompt = prompt_engine.load_prompt("parse-verification-code", content=content)
    code_resp = await app.SECONDARY_LLM_API_HANDLER(prompt=prompt, prompt_name="parse-verification-code")
    LOG.info("TOTP Code Parser Response", code_resp=code_resp)
    return code_resp.get("code", None)


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
    data: TOTPCodeCreate, curr_org: Organization = Depends(org_auth_service.get_current_org)
) -> TOTPCode:
    LOG.info(
        "Saving TOTP code",
        organization_id=curr_org.organization_id,
        totp_identifier=data.totp_identifier,
        task_id=data.task_id,
        workflow_id=data.workflow_id,
        workflow_run_id=data.workflow_run_id,
    )
    content = data.content.strip()
    code: str | None = content
    # We assume the user is sending the code directly when the length of code is less than or equal to 10
    if len(content) > 10:
        code = await parse_totp_code(content)
    if not code:
        LOG.error(
            "Failed to parse totp code",
            totp_identifier=data.totp_identifier,
            task_id=data.task_id,
            workflow_id=data.workflow_id,
            workflow_run_id=data.workflow_run_id,
            content=data.content,
        )
        raise HTTPException(status_code=400, detail="Failed to parse totp code")
    return await app.DATABASE.create_totp_code(
        organization_id=curr_org.organization_id,
        totp_identifier=data.totp_identifier,
        content=data.content,
        code=code,
        task_id=data.task_id,
        workflow_id=data.workflow_id,
        workflow_run_id=data.workflow_run_id,
        source=data.source,
        expired_at=data.expired_at,
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
    org_collection = await app.DATABASE.get_organization_bitwarden_collection(current_org.organization_id)

    if not org_collection:
        LOG.info(
            "There is no collection for the organization. Creating new collection.",
            organization_id=current_org.organization_id,
        )
        collection_id = await BitwardenService.create_collection(
            name=current_org.organization_id,
        )
        org_collection = await app.DATABASE.create_organization_bitwarden_collection(
            current_org.organization_id,
            collection_id,
        )

    item_id = await BitwardenService.create_credential_item(
        collection_id=org_collection.collection_id,
        name=data.name,
        credential=data.credential,
    )

    credential = await app.DATABASE.create_credential(
        organization_id=current_org.organization_id,
        item_id=item_id,
        name=data.name,
        credential_type=data.credential_type,
    )

    if data.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=data.credential.username,
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
    credential_id: str = Path(
        ...,
        description="The unique identifier of the credential to delete",
        examples=["cred_1234567890"],
        openapi_extra={"x-fern-sdk-parameter-name": "credential_id"},
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(
        current_org.organization_id
    )
    if not organization_bitwarden_collection:
        raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

    credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not credential:
        raise HTTPException(status_code=404, detail=f"Credential not found, credential_id={credential_id}")

    await app.DATABASE.delete_credential(credential.credential_id, current_org.organization_id)
    await BitwardenService.delete_credential_item(credential.item_id)

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
    organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(
        current_org.organization_id
    )
    if not organization_bitwarden_collection:
        raise HTTPException(status_code=404, detail="Credential account not found. It might have been deleted.")

    credential = await app.DATABASE.get_credential(
        credential_id=credential_id, organization_id=current_org.organization_id
    )
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    credential_item = await BitwardenService.get_credential_item(credential.item_id)
    if not credential_item:
        raise HTTPException(status_code=404, detail="Credential not found")

    if credential_item.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=credential_item.credential.username,
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential_item.credential_type,
            name=credential_item.name,
        )
    if credential_item.credential_type == CredentialType.CREDIT_CARD:
        credential_response = CreditCardCredentialResponse(
            last_four=credential_item.credential.card_number[-4:],
            brand=credential_item.credential.card_brand,
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential_item.credential_type,
            name=credential_item.name,
        )
    raise HTTPException(status_code=400, detail="Invalid credential type")


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
    organization_bitwarden_collection = await app.DATABASE.get_organization_bitwarden_collection(
        current_org.organization_id
    )
    if not organization_bitwarden_collection:
        return []

    credentials = await app.DATABASE.get_credentials(current_org.organization_id, page=page, page_size=page_size)
    items = await BitwardenService.get_collection_items(organization_bitwarden_collection.collection_id)

    response_items = []
    for credential in credentials:
        item = next((item for item in items if item.item_id == credential.item_id), None)
        if not item:
            continue
        if item.credential_type == CredentialType.PASSWORD:
            credential_response = PasswordCredentialResponse(username=item.credential.username)
            response_items.append(
                CredentialResponse(
                    credential=credential_response,
                    credential_id=credential.credential_id,
                    credential_type=item.credential_type,
                    name=item.name,
                )
            )
        elif item.credential_type == CredentialType.CREDIT_CARD:
            credential_response = CreditCardCredentialResponse(
                last_four=item.credential.card_number[-4:],
                brand=item.credential.card_brand,
            )
            response_items.append(
                CredentialResponse(
                    credential=credential_response,
                    credential_id=credential.credential_id,
                    credential_type=item.credential_type,
                    name=item.name,
                )
            )
    return response_items
