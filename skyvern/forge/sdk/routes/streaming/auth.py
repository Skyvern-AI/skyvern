"""
Streaming auth.
"""

import typing as t

import structlog
from fastapi import HTTPException, WebSocket, status
from websockets.exceptions import ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.services.org_auth_service import get_current_org
from skyvern.forge.sdk.services.request_principal import get_request_principal

LOG = structlog.get_logger()

EXPECTED_AUTH_FAILURE_STATUS_CODES = frozenset({status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN})

STREAM_SUBPROTOCOL = "skyvern.v1"
_BEARER_SUBPROTOCOL_PREFIX = "bearer."
_APIKEY_SUBPROTOCOL_PREFIX = "apikey."


class StreamCredentials(t.NamedTuple):
    apikey: str | None
    # Full `Authorization` header value (`Bearer <jwt>`), as `get_current_org` expects.
    authorization: str | None
    subprotocol: str | None


def credentials_from_websocket(websocket: WebSocket, apikey: str | None, token: str | None) -> StreamCredentials:
    """Read the stream credential from `Sec-WebSocket-Protocol`, falling back to the deprecated query params.

    Browsers cannot set headers on a WebSocket handshake, so the web app offers
    `skyvern.v1, bearer.<jwt>` / `apikey.<key>` as subprotocols instead of putting the credential in the URL.
    """
    header_apikey: str | None = None
    header_authorization: str | None = None
    subprotocol: str | None = None
    for item in websocket.headers.get("sec-websocket-protocol", "").split(","):
        item = item.strip()
        if item == STREAM_SUBPROTOCOL:
            subprotocol = STREAM_SUBPROTOCOL
        elif item.startswith(_BEARER_SUBPROTOCOL_PREFIX) and len(item) > len(_BEARER_SUBPROTOCOL_PREFIX):
            header_authorization = f"Bearer {item[len(_BEARER_SUBPROTOCOL_PREFIX) :]}"
        elif item.startswith(_APIKEY_SUBPROTOCOL_PREFIX) and len(item) > len(_APIKEY_SUBPROTOCOL_PREFIX):
            header_apikey = item[len(_APIKEY_SUBPROTOCOL_PREFIX) :]

    # Any credential in the header wins outright; query values are not merged in.
    if header_apikey or header_authorization:
        return StreamCredentials(header_apikey, header_authorization, subprotocol)
    if apikey or token:
        LOG.info("Streaming credential supplied in query string (deprecated)", path=websocket.url.path)
    return StreamCredentials(apikey, token, subprotocol)


def require_client_id(client_id: str | None, **log_kwargs: t.Any) -> bool:
    if client_id:
        return True
    LOG.error("No client_id provided", **log_kwargs)
    return False


class Constants:
    MISSING_API_KEY = "<missing-x-api-key>"


async def get_x_api_key(organization_id: str) -> str:
    token = await app.DATABASE.organizations.get_valid_org_auth_token(
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


async def _close_unauthenticated(websocket: WebSocket, **log_kwargs: t.Any) -> None:
    try:
        await websocket.close(code=1002)
    except ConnectionClosedOK:
        LOG.info("WebSocket connection closed due to invalid credentials.", **log_kwargs)


async def folder_access_denied(websocket: WebSocket, action: str, resource: t.Mapping[str, str | None]) -> bool:
    """Close the stream when the caller's workflow folders do not cover the run behind it."""
    principal = get_request_principal()
    try:
        # The evaluator judges one identifier per call, so each id behind the stream is checked on its own.
        for key, value in resource.items():
            if value:
                await app.AGENT_FUNCTION.enforce_folder_access(principal, action, {key: value})
                if key == "browser_session_id":
                    await app.AGENT_FUNCTION.enforce_browser_session_credential_use(principal, value)
                elif key == "workflow_run_id":
                    await app.AGENT_FUNCTION.enforce_workflow_run_credential_use(principal, value)
                elif key == "task_id":
                    try:
                        organization_id = principal.organization_id if principal else None
                        task = await app.DATABASE.tasks.get_task(value, organization_id=organization_id)
                        if task is None:
                            continue
                        if task.workflow_run_id:
                            await app.AGENT_FUNCTION.enforce_workflow_run_credential_use(
                                principal, task.workflow_run_id
                            )
                        session = await app.DATABASE.browser_sessions.get_persistent_browser_session_by_runnable_id(
                            value, organization_id=task.organization_id
                        )
                        if session:
                            await app.AGENT_FUNCTION.enforce_browser_session_credential_use(
                                principal, session.persistent_browser_session_id
                            )
                    except (HTTPException, PermissionError):
                        raise
                    except Exception as exc:
                        LOG.warning(
                            "Task stream credential authorization failed; denying",
                            task_id=value,
                            exc_info=True,
                        )
                        raise PermissionError("credential not accessible") from exc
    except (HTTPException, PermissionError):
        LOG.info("Rejecting streaming connection outside the caller's workflow folders.", action=action)
        try:
            await websocket.close(code=4403)
        except ConnectionClosedOK:
            pass
        return True
    return False


async def auth(
    apikey: str | None,
    token: str | None,
    websocket: WebSocket,
    *,
    folder_action: str,
    folder_resource: t.Mapping[str, str | None],
    **log_kwargs: t.Any,
) -> str | None:
    """
    Accepts the websocket connection.

    Authenticates the user; cannot proceed with WS connection if an organization_id cannot be
    determined.
    """

    credentials = credentials_from_websocket(websocket, apikey, token)
    try:
        await websocket.accept(subprotocol=credentials.subprotocol)
        if not credentials.authorization and not credentials.apikey:
            await websocket.close(code=1002)
            return None
    except ConnectionClosedOK:
        LOG.info("WebSocket connection closed cleanly.", **log_kwargs)
        return None

    try:
        organization = await get_current_org(x_api_key=credentials.apikey, authorization=credentials.authorization)
        organization_id = organization.organization_id

        if not organization_id:
            await websocket.close(code=1002)
            return None
    except HTTPException as exc:
        if exc.status_code in EXPECTED_AUTH_FAILURE_STATUS_CODES:
            LOG.warning(
                "Rejecting streaming connection with invalid credentials.",
                status_code=exc.status_code,
                detail=exc.detail,
                **log_kwargs,
            )
        else:
            LOG.exception("Error occurred while retrieving organization information.", **log_kwargs)
        await _close_unauthenticated(websocket, **log_kwargs)
        return None
    except Exception:
        LOG.exception("Error occurred while retrieving organization information.", **log_kwargs)
        await _close_unauthenticated(websocket, **log_kwargs)
        return None

    if await folder_access_denied(websocket, folder_action, folder_resource):
        return None
    return organization_id
