"""
Streaming auth.
"""

import structlog
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.services.org_auth_service import get_current_org

LOG = structlog.get_logger()


class Constants:
    MISSING_API_KEY = "<missing-x-api-key>"


async def get_x_api_key(organization_id: str) -> str:
    token = await app.DATABASE.get_valid_org_auth_token(
        organization_id,
        OrganizationAuthTokenType.api.value,
    )

    if not token:
        LOG.warning(
            "No valid API key found for organization when streaming.",
            organization_id=organization_id,
        )
        x_api_key = Constants.MISSING_API_KEY
    else:
        x_api_key = token.token

    return x_api_key


async def auth(apikey: str | None, token: str | None, websocket: WebSocket) -> str | None:
    """
    Accepts the websocket connection.

    Authenticates the user; cannot proceed with WS connection if an organization_id cannot be
    determined.
    """

    try:
        await websocket.accept()
        if not token and not apikey:
            await websocket.close(code=1002)
            return None
    except ConnectionClosedOK:
        LOG.info("WebSocket connection closed cleanly.")
        return None

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        organization_id = organization.organization_id

        if not organization_id:
            await websocket.close(code=1002)
            return None
    except Exception:
        LOG.exception("Error occurred while retrieving organization information.")
        try:
            await websocket.close(code=1002)
        except ConnectionClosedOK:
            LOG.info("WebSocket connection closed due to invalid credentials.")
        return None

    return organization_id


# NOTE(jdo:streaming-local-dev): use this instead of the above `auth`
async def _auth(apikey: str | None, token: str | None, websocket: WebSocket) -> str | None:
    """
    Dummy auth for local testing.
    """

    try:
        await websocket.accept()
    except ConnectionClosedOK:
        LOG.info("WebSocket connection closed cleanly.")
        return None

    return "o_temp123"
