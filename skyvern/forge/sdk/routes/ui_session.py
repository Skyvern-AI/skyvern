from typing import Annotated

from fastapi import Header, Response
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key
from skyvern.forge.sdk.services.org_auth_token_service import create_org_ui_session_token


class UISessionTokenResponse(BaseModel):
    token: str
    expires_at: int


@legacy_base_router.post(
    "/ui-session",
    response_model=UISessionTokenResponse,
    include_in_schema=False,
)
@base_router.post(
    "/ui-session",
    response_model=UISessionTokenResponse,
    include_in_schema=False,
)
async def create_ui_session_token(
    response: Response,
    x_api_key: Annotated[str, Header()],
) -> UISessionTokenResponse:
    validation = await resolve_org_from_api_key(
        x_api_key,
        app.DATABASE,
        token_types=(OrganizationAuthTokenType.api,),
    )
    auth_token, expires_at = await create_org_ui_session_token(validation.organization.organization_id)
    response.headers["Cache-Control"] = "no-store"
    return UISessionTokenResponse(token=auth_token.token, expires_at=expires_at)
