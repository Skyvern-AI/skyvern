"""Raw Ember HTTP calls with plain dict bodies, because the Ember routes are not in the public OpenAPI schema.
Ember itself is only reached through /v1/ember/*; there is no Copilot-route fallback."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx

from skyvern.cli.core.client import get_active_api_key
from skyvern.client.core.http_client import AsyncHttpClient
from skyvern.client.core.request_options import RequestOptions

from ._common import ErrorCode
from ._session import get_skyvern
from ._workflow_http import extract_error_detail

EMBER_ROUTE = "v1/ember"
RECOVERY_HEADER = "X-Ember-Credential-Recovery-Token"
_RECOVERY_CONTEXT = b"skyvern-ember-credential-recovery"
CHATS_PAGE_SIZE = 100
_MAX_CHAT_PAGES = 20
_HISTORY_CHANGED_DETAIL = "Copilot history changed while loading"
_NOT_SUPPORTED_SUFFIX = "not supported in this environment"
_STEER_TURN_ENDED = "turn has ended"
_RUN_NOT_FOUND_PREFIX = "workflow_run_id does not name a run"
_WORKFLOW_NOT_FOUND_DETAIL = "Workflow not found"
_TURN_RUNNING_REFUSED = "Another Copilot turn is still running on this workflow"
_TURN_CONTROL_DETAILS = (
    "Only the person who started this Copilot turn",
    "Another member's Copilot turn is still running",
)
_SITES_CHANGED_PREFIX = "This login's sites changed"
_ITEM_GONE_DETAILS = ("Unknown Copilot question", "No active credential pause", "Credential pause already resolved")
_QUESTION_GONE_MARKERS = ("no longer active", "no longer pending", "execution was interrupted", "already answered")
_INVALID_RESUME_TOKEN_DETAIL = "Invalid credential resume token"
_NO_PROPOSAL_DETAIL = "No proposed workflow to apply"
_UNKNOWN_MESSAGE_DETAIL = "Unknown Copilot message"

Endpoint = Literal[
    "chat-post",
    "chat-history",
    "chats",
    "steer",
    "cancel",
    "question-response",
    "credential-deletion",
    "message-feedback",
    "credential-response",
    "credential-generate",
    "clear-proposed-workflow",
    "disable-auto-accept",
    "apply-proposed-workflow",
]
_QUESTION_ENDPOINTS = ("question-response", "credential-deletion")
_CREDENTIAL_PAUSE_ENDPOINTS = ("credential-response", "credential-generate")
_PROPOSAL_ENDPOINTS = ("apply-proposed-workflow", "clear-proposed-workflow")


class EmberError(Exception):
    def __init__(self, code: str, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class ChatScanCapped(EmberError):
    pass


class HistoryReadTimeout(EmberError):
    pass


class WriteUnconfirmed(EmberError):
    pass


class SitesChanged(EmberError):
    pass


def chat_id_of(payload: dict[str, Any]) -> str | None:
    return payload.get("ember_chat_id") or payload.get("workflow_copilot_chat_id")


def recovery_token() -> str:
    api_key = get_active_api_key()
    if not api_key:
        raise EmberError(ErrorCode.API_ERROR, "No Skyvern API key is configured for this MCP server.")
    return hmac.new(api_key.encode(), _RECOVERY_CONTEXT, hashlib.sha256).hexdigest()


def _is_turn_control_refusal(detail: str) -> bool:
    return detail.startswith(_TURN_CONTROL_DETAILS)


def _is_missing_credential(detail: str) -> bool:
    return detail.startswith("Credential ") and detail.endswith(" not found")


def classify_error(response: httpx.Response, endpoint: Endpoint) -> EmberError:
    status = response.status_code
    detail = extract_error_detail(response)
    if status == 404:
        try:
            body = response.json()
        except ValueError:
            body = None
        if body == {"detail": "Not Found"}:
            return EmberError(
                ErrorCode.EMBER_UNAVAILABLE,
                "This Skyvern server is older than Ember's API; upgrade the Skyvern server.",
            )
    if status == 409 and detail.startswith(_SITES_CHANGED_PREFIX):
        return SitesChanged(ErrorCode.ITEM_EXPIRED, detail)
    if status == 410 or (status in (404, 409) and detail.startswith(_ITEM_GONE_DETAILS)):
        return EmberError(ErrorCode.ITEM_EXPIRED, detail)
    if status == 403 and detail == _INVALID_RESUME_TOKEN_DETAIL:
        return EmberError(ErrorCode.ITEM_EXPIRED, detail)
    if status == 404 and (detail == _UNKNOWN_MESSAGE_DETAIL or _is_missing_credential(detail)):
        return EmberError(ErrorCode.INVALID_INPUT, detail)
    if status == 409 and endpoint in _QUESTION_ENDPOINTS:
        gone = any(marker in detail for marker in _QUESTION_GONE_MARKERS)
        return EmberError(ErrorCode.ITEM_EXPIRED if gone else ErrorCode.INVALID_INPUT, detail)
    if status == 409 and endpoint in _CREDENTIAL_PAUSE_ENDPOINTS:
        return EmberError(ErrorCode.INVALID_INPUT, detail)
    # Every 409 the accept and reject routes return means the proposal moved (proposal_accept.py), whatever its wording.
    if status == 409 and endpoint in _PROPOSAL_ENDPOINTS:
        return EmberError(ErrorCode.PROPOSAL_CHANGED, detail)
    if status == 400 and detail == _NO_PROPOSAL_DETAIL:
        return EmberError(ErrorCode.NO_PROPOSAL, detail)
    if status == 404:
        if endpoint == "chat-post" and detail.startswith(_RUN_NOT_FOUND_PREFIX):
            return EmberError(ErrorCode.RUN_NOT_FOUND, detail)
        if endpoint == "chat-post" and detail == _WORKFLOW_NOT_FOUND_DETAIL:
            return EmberError(ErrorCode.WORKFLOW_NOT_FOUND, detail)
        return EmberError(ErrorCode.CHAT_NOT_FOUND, f"Ember chat not found: {detail}")
    if status == 503 and detail.endswith(_NOT_SUPPORTED_SUFFIX):
        if endpoint == "cancel":
            return EmberError(
                ErrorCode.API_ERROR,
                f"Stop is not supported on this Skyvern server, which has no shared cache: {detail}",
            )
        return EmberError(ErrorCode.EMBER_UNAVAILABLE, f"Ember is not available on this Skyvern server: {detail}")
    if status == 503:
        return EmberError(ErrorCode.RETRY_LATER, detail, retry_after=response.headers.get("Retry-After"))
    if status == 403:
        if _is_turn_control_refusal(detail):
            return EmberError(ErrorCode.TURN_CONTROL_DENIED, detail)
        return EmberError(
            ErrorCode.PERMISSION_DENIED,
            f"{detail} Ember is governed by the workflow.update and workflow_copilot.* permissions.",
        )
    if status == 409:
        return EmberError(ErrorCode.TURN_IN_PROGRESS, detail)
    if status in (400, 422):
        return EmberError(ErrorCode.INVALID_INPUT, detail)
    return EmberError(ErrorCode.API_ERROR, f"HTTP {status}: {detail}")


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _client() -> AsyncHttpClient:
    return get_skyvern()._client_wrapper.httpx_client


def _options(timeout: float | None) -> RequestOptions:
    # Ember writes are not idempotent and callers report "nothing was sent" on a failed connect, so never retry.
    options = RequestOptions(max_retries=0)
    if timeout is not None:
        options["timeout_in_seconds"] = max(1, math.ceil(timeout))
    return options


async def get_history(
    *,
    chat_id: str | None = None,
    workflow_permanent_id: str | None = None,
    request_cancel_token: str | None = None,
    message_limit: int | None = 2,
    timeout: float | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"include_live_narrative": True}
    if chat_id is not None:
        params["ember_chat_id"] = chat_id
    else:
        params["workflow_permanent_id"] = workflow_permanent_id
    if request_cancel_token is not None:
        params["request_cancel_token"] = request_cancel_token
    if message_limit is not None:
        params["message_limit"] = message_limit
    started = time.monotonic()
    for attempt in range(2):
        # The retry shares the read's timeout, so a slow first answer leaves it only what is left.
        attempt_timeout = None if timeout is None else timeout - (time.monotonic() - started)
        if attempt and attempt_timeout is not None and attempt_timeout < 1:
            break
        try:
            response = await _client().request(
                f"{EMBER_ROUTE}/chat-history",
                method="GET",
                params=params,
                headers={RECOVERY_HEADER: recovery_token()},
                request_options=_options(attempt_timeout),
            )
        except httpx.TimeoutException as exc:
            raise HistoryReadTimeout(ErrorCode.RETRY_LATER, "Ember's chat history did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise EmberError(
                ErrorCode.API_ERROR, f"Could not read Ember's chat history ({type(exc).__name__})."
            ) from exc
        if response.status_code == 503 and extract_error_detail(response) == _HISTORY_CHANGED_DETAIL and attempt == 0:
            continue
        break
    if response.status_code >= 400:
        raise classify_error(response, "chat-history")
    payload = _json_or_none(response)
    if not isinstance(payload, dict) or "app_url" not in payload or "workflow_permanent_id" not in payload:
        raise EmberError(
            ErrorCode.EMBER_UNAVAILABLE,
            "This Skyvern server's chat history lacks fields Ember's MCP tools need; upgrade the Skyvern server.",
        )
    if chat_id is not None and chat_id_of(payload) is None:
        raise EmberError(ErrorCode.CHAT_NOT_FOUND, f"Ember chat {chat_id!r} not found.")
    remaining = None if timeout is None else timeout - (time.monotonic() - started)
    if message_limit is not None and _proposal_owner_row_missing(payload) and (remaining is None or remaining >= 1):
        # A waiting proposal's review lives on its owner turn's row, which a short read misses once the chat moves on.
        return await get_history(
            chat_id=chat_id,
            workflow_permanent_id=workflow_permanent_id,
            request_cancel_token=request_cancel_token,
            message_limit=None,
            timeout=remaining,
        )
    return payload


def _proposal_owner_row_missing(payload: dict[str, Any]) -> bool:
    owner_turn_id = (payload.get("proposed_workflow_metadata") or {}).get("owner_turn_id")
    if not payload.get("proposed_workflow") or not owner_turn_id:
        return False
    # A running turn that owns the draft has no AI row yet; a full read would not find one either.
    if (payload.get("in_flight_turn") or {}).get("turn_id") == owner_turn_id:
        return False
    return not any(
        isinstance(row, dict) and row.get("sender") == "ai" and row.get("turn_id") == owner_turn_id
        for row in payload.get("chat_history") or []
    )


async def list_chats(
    *,
    workflow_permanent_id: str | None,
    search: str | None = None,
    page: int = 1,
    page_size: int = CHATS_PAGE_SIZE,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if workflow_permanent_id is not None:
        params["workflow_permanent_id"] = workflow_permanent_id
    if search is not None:
        params["search"] = search
    try:
        response = await _client().request(
            f"{EMBER_ROUTE}/chats", method="GET", params=params, request_options=_options(timeout)
        )
    except httpx.TimeoutException as exc:
        raise EmberError(ErrorCode.RETRY_LATER, "Ember's chat list did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise EmberError(ErrorCode.API_ERROR, f"Could not read Ember's chat list ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        raise classify_error(response, "chats")
    payload = _json_or_none(response)
    if not isinstance(payload, list):
        raise EmberError(ErrorCode.API_ERROR, f"Unexpected Ember chats payload: {type(payload).__name__}")
    return [row for row in payload if isinstance(row, dict)]


async def list_all_chats(
    workflow_permanent_id: str, *, timeout: float | Callable[[], float] | None = None
) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    for page in range(1, _MAX_CHAT_PAGES + 1):
        # A callable lets the caller give each page only the time its call has left.
        page_timeout = timeout() if callable(timeout) else timeout
        rows = await list_chats(workflow_permanent_id=workflow_permanent_id, page=page, timeout=page_timeout)
        chats.extend(rows)
        if len(rows) < CHATS_PAGE_SIZE:
            return chats
    raise ChatScanCapped(
        ErrorCode.API_ERROR,
        f"This workflow has more than {len(chats)} Ember chats, too many to check for a running turn, so no new "
        "turn can start from here. To steer a running turn, pass its chat as ember_chat_id; otherwise use the Studio.",
    )


async def post_chat(body: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
    """POST chat-post and read frames until turn_start; None means the stream ended before turn_start."""
    try:
        async with _client().stream(
            f"{EMBER_ROUTE}/chat-post",
            method="POST",
            json=body,
            retries=0,
            request_options=_options(timeout),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise classify_error(response, "chat-post")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[len("data:") :].strip())
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "turn_start":
                    return frame
                if frame.get("type") == "error":
                    error = str(frame.get("error") or "Ember could not start the turn.")
                    if error.startswith(_TURN_RUNNING_REFUSED):
                        raise EmberError(
                            ErrorCode.TURN_IN_PROGRESS,
                            "Another Ember turn started in this chat after send checked, so nothing was sent and "
                            "that turn keeps running. Call skyvern_ember_check to see it.",
                        )
                    if _is_turn_control_refusal(error):
                        raise EmberError(ErrorCode.TURN_CONTROL_DENIED, error)
                    kind = frame.get("failure_kind") or "ember"
                    raise EmberError(ErrorCode.API_ERROR, f"Ember could not start the turn ({kind}): {error}")
    # A connection that never opened sent nothing; any later failure may have started the turn.
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise EmberError(ErrorCode.API_ERROR, f"Could not reach the Skyvern server: {exc}") from exc
    except httpx.HTTPError:
        return None
    return None


async def steer(
    *, chat_id: str, cancel_token: str, steer_id: str, message: str, timeout: float | None = None
) -> dict[str, Any] | None:
    """POST steer; None means the turn ended before the message could be delivered."""
    response = await _client().request(
        f"{EMBER_ROUTE}/steer",
        method="POST",
        json={"ember_chat_id": chat_id, "cancel_token": cancel_token, "steer_id": steer_id, "message": message},
        request_options=_options(timeout),
    )
    if response.status_code == 409 and _STEER_TURN_ENDED in extract_error_detail(response):
        return None
    if response.status_code >= 400:
        raise classify_error(response, "steer")
    # The steer was accepted, so an unreadable body still reports it as sent.
    payload = _json_or_none(response)
    return payload if isinstance(payload, dict) else {}


async def cancel(*, chat_id: str, cancel_token: str, timeout: float | None = None) -> None:
    try:
        response = await _client().request(
            f"{EMBER_ROUTE}/cancel",
            method="POST",
            json={"ember_chat_id": chat_id, "cancel_token": cancel_token, "source": "api"},
            request_options=_options(timeout),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise EmberError(ErrorCode.RETRY_LATER, "Could not reach Skyvern to stop the turn; nothing was sent.") from exc
    except httpx.HTTPError as exc:
        raise EmberError(
            ErrorCode.API_ERROR,
            f"The stop request got no answer ({type(exc).__name__}); it may have taken effect. Call "
            "skyvern_ember_check to see whether the turn stopped.",
        ) from exc
    if response.status_code >= 400:
        raise classify_error(response, "cancel")


async def post_action(
    endpoint: Endpoint, body: dict[str, Any], *, timeout: float | None = None
) -> dict[str, Any] | None:
    """POST one card route; None means the route answered 204 with no body."""
    try:
        response = await _client().request(
            f"{EMBER_ROUTE}/{endpoint}", method="POST", json=body, request_options=_options(timeout)
        )
    except httpx.ConnectError as exc:
        # No connection means no request; these calls set no retries, so no earlier attempt can have landed.
        raise EmberError(
            ErrorCode.RETRY_LATER,
            f"Could not reach Skyvern to send {endpoint} ({type(exc).__name__}); nothing was sent.",
        ) from exc
    except httpx.HTTPError as exc:
        # The server keeps working after the client gives up, so a lost answer does not mean nothing happened.
        raise WriteUnconfirmed(
            ErrorCode.API_ERROR,
            f"The {endpoint} request got no answer ({type(exc).__name__}); it may have taken effect.",
        ) from exc
    if response.status_code >= 400:
        raise classify_error(response, endpoint)
    if response.status_code == 204 or not response.content:
        return None
    # The write was accepted, so an unreadable body still reports it as done.
    payload = _json_or_none(response)
    return payload if isinstance(payload, dict) else {}


async def _get_json(path: str, *, timeout: float | None) -> dict[str, Any] | None:
    try:
        response = await _client().request(path, method="GET", request_options=_options(timeout))
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def list_saved_logins(limit: int, *, timeout: float | None = None) -> list[dict[str, str | None]] | None:
    """The org's newest `limit` password credentials (id, name, username), with no search like the Studio picker's."""
    params = {"page": 1, "page_size": limit, "credential_type": "password"}
    try:
        response = await _client().request(
            "v1/credentials", method="GET", params=params, request_options=_options(timeout)
        )
    except httpx.HTTPError:
        return None
    payload = _json_or_none(response) if response.status_code == 200 else None
    if not isinstance(payload, list):
        return None
    return [
        {
            "credential_id": row["credential_id"],
            "name": row["name"],
            "username": row["credential"].get("username") if isinstance(row.get("credential"), dict) else None,
        }
        for row in payload
        if isinstance(row, dict) and isinstance(row.get("credential_id"), str) and isinstance(row.get("name"), str)
    ]


async def get_debug_session_browser_id(workflow_permanent_id: str, *, timeout: float) -> str | None:
    payload = await _get_json(f"v1/debug-session/{workflow_permanent_id}", timeout=timeout)
    browser_session_id = payload.get("browser_session_id") if payload else None
    return browser_session_id if isinstance(browser_session_id, str) else None


async def get_browser_session(browser_session_id: str, *, timeout: float | None = None) -> dict[str, Any] | None:
    return await _get_json(f"v1/browser_sessions/{browser_session_id}", timeout=timeout)


async def get_artifact_url(artifact_id: str, *, timeout: float | None = None) -> str | None:
    payload = await _get_json(f"v1/artifacts/{artifact_id}", timeout=timeout)
    url = payload.get("signed_url") if payload else None
    return url if isinstance(url, str) else None


async def upload_file(name: str, content: bytes, *, timeout: float | None = None) -> str:
    try:
        response = await _client().request(
            "v1/upload_file",
            method="POST",
            files={"file": (name, content)},
            request_options=_options(timeout),
        )
    except httpx.HTTPError as exc:
        raise EmberError(
            ErrorCode.RETRY_LATER,
            f"Upload of {name} did not complete ({type(exc).__name__}); no turn was started, so sending again is safe.",
        ) from exc
    if response.status_code >= 400:
        raise EmberError(
            ErrorCode.INVALID_INPUT,
            f"Upload of {name} failed: HTTP {response.status_code}: {extract_error_detail(response)}",
        )
    payload = _json_or_none(response)
    file_id = payload.get("file_id") if isinstance(payload, dict) else None
    if not isinstance(file_id, str):
        raise EmberError(ErrorCode.API_ERROR, f"Upload of {name} returned no file id.")
    return file_id
