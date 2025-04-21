import structlog
from fastapi import Depends, HTTPException

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.routes.routers import legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.totp_codes import TOTPCode, TOTPCodeCreate
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()


@legacy_base_router.post(
    "/totp",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-group-name": "agent",
        "x-fern-sdk-method-name": "send_totp_code",
    },
)
@legacy_base_router.post("/totp/", include_in_schema=False)
async def send_totp_code(
    data: TOTPCodeCreate, curr_org: Organization = Depends(org_auth_service.get_current_org)
) -> TOTPCode:
    LOG.info(
        "Saving TOTP code",
        data=data,
        organization_id=curr_org.organization_id,
        totp_identifier=data.totp_identifier,
        task_id=data.task_id,
        workflow_id=data.workflow_id,
    )
    code = await parse_totp_code(data.content)
    if not code:
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


async def parse_totp_code(content: str) -> str | None:
    prompt = prompt_engine.load_prompt("parse-verification-code", content=content)
    code_resp = await app.SECONDARY_LLM_API_HANDLER(prompt=prompt, prompt_name="parse-verification-code")
    return code_resp.get("code", None)
