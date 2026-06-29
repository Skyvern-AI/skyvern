import structlog
from fastapi import Depends, HTTPException, Path

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.custom_llm_registry import (
    deregister_custom_llm_config,
    register_custom_llm_config,
)
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.custom_llms import (
    CUSTOM_LLM_API_KEY_MASK,
    CustomLLM,
    CustomLLMConfig,
    CustomLLMCreateRequest,
    CustomLLMListResponse,
    CustomLLMResponse,
    CustomLLMUpdateRequest,
    custom_llm_from_org_auth_token,
    custom_llm_response_from_org_auth_token,
)
from skyvern.forge.sdk.schemas.organizations import ClearOrganizationAuthTokenResponse, Organization
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()


def _require_local_custom_llms() -> None:
    if settings.ENV != "local":
        raise HTTPException(status_code=404, detail="Custom LLM configuration is only available in local OSS mode")


async def _get_custom_llm(
    organization_id: str,
    custom_llm_id: str,
) -> CustomLLM:
    tokens = await app.DATABASE.organizations.get_valid_org_auth_tokens(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
    )
    for token in tokens:
        if token.id == custom_llm_id:
            return custom_llm_from_org_auth_token(token)
    raise NotFoundError("Custom LLM not found")


def _config_with_preserved_api_key(config: CustomLLMConfig, existing_custom_llm: CustomLLM) -> CustomLLMConfig:
    if config.api_key != CUSTOM_LLM_API_KEY_MASK:
        return config

    return CustomLLMConfig.model_validate(
        {
            **config.model_dump(),
            "api_key": existing_custom_llm.config.api_key,
        }
    )


@base_router.get(
    "/custom-llms",
    response_model=CustomLLMListResponse,
    summary="List Custom LLMs",
    include_in_schema=False,
)
@base_router.get(
    "/custom-llms/",
    response_model=CustomLLMListResponse,
    include_in_schema=False,
)
async def list_custom_llms(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CustomLLMListResponse:
    _require_local_custom_llms()
    tokens = await app.DATABASE.organizations.get_valid_org_auth_tokens(
        organization_id=current_org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
    )
    custom_llms = []
    for token in tokens:
        try:
            custom_llm = custom_llm_response_from_org_auth_token(token)
        except Exception:
            LOG.warning("Skipping invalid custom LLM token", token_id=token.id, exc_info=True)
            continue
        custom_llms.append(custom_llm)

    return CustomLLMListResponse(custom_llms=custom_llms)


@base_router.post(
    "/custom-llms",
    response_model=CustomLLMResponse,
    summary="Create Custom LLM",
    include_in_schema=False,
)
@base_router.post(
    "/custom-llms/",
    response_model=CustomLLMResponse,
    include_in_schema=False,
)
async def create_custom_llm(
    request: CustomLLMCreateRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CustomLLMResponse:
    _require_local_custom_llms()
    token = await app.DATABASE.organizations.create_org_auth_token(
        organization_id=current_org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=request.config.model_dump_json(),
    )
    custom_llm = custom_llm_from_org_auth_token(token)
    register_custom_llm_config(custom_llm.id, custom_llm.config)
    return CustomLLMResponse(custom_llm=custom_llm_response_from_org_auth_token(token))


@base_router.put(
    "/custom-llms/{custom_llm_id}",
    response_model=CustomLLMResponse,
    summary="Update Custom LLM",
    include_in_schema=False,
)
@base_router.put(
    "/custom-llms/{custom_llm_id}/",
    response_model=CustomLLMResponse,
    include_in_schema=False,
)
async def update_custom_llm(
    request: CustomLLMUpdateRequest,
    custom_llm_id: str = Path(..., description="The custom LLM id."),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> CustomLLMResponse:
    _require_local_custom_llms()
    try:
        existing_custom_llm = await _get_custom_llm(current_org.organization_id, custom_llm_id)
        config = _config_with_preserved_api_key(request.config, existing_custom_llm)
        token = await app.DATABASE.organizations.update_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.custom_llm,
            token_id=custom_llm_id,
            token=config.model_dump_json(),
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail="Custom LLM not found") from e

    custom_llm = custom_llm_from_org_auth_token(token)
    register_custom_llm_config(custom_llm.id, custom_llm.config)
    return CustomLLMResponse(custom_llm=custom_llm_response_from_org_auth_token(token))


@base_router.delete(
    "/custom-llms/{custom_llm_id}",
    response_model=ClearOrganizationAuthTokenResponse,
    summary="Delete Custom LLM",
    include_in_schema=False,
)
@base_router.delete(
    "/custom-llms/{custom_llm_id}/",
    response_model=ClearOrganizationAuthTokenResponse,
    include_in_schema=False,
)
async def delete_custom_llm(
    custom_llm_id: str = Path(..., description="The custom LLM id."),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ClearOrganizationAuthTokenResponse:
    _require_local_custom_llms()
    try:
        await app.DATABASE.organizations.invalidate_org_auth_token(
            organization_id=current_org.organization_id,
            token_type=OrganizationAuthTokenType.custom_llm,
            token_id=custom_llm_id,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail="Custom LLM not found") from e

    deregister_custom_llm_config(custom_llm_id)
    return ClearOrganizationAuthTokenResponse(success=True)
