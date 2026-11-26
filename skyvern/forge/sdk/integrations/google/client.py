import json
from typing import Any, TypeVar

import httpx

from skyvern.forge.sdk.integrations.http import ProviderAPIError

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
RECONNECT_REQUIRED_CODE = "reconnect_required"

_RATE_LIMIT_REASONS_CASEFOLDED = frozenset(reason.lower() for reason in RATE_LIMIT_REASONS)
_MISSING_SCOPE_REASONS = frozenset({"insufficientPermissions", "insufficientScopes"})


class GoogleAPIError(ProviderAPIError):
    pass


_GoogleAPIErrorT = TypeVar("_GoogleAPIErrorT", bound=GoogleAPIError)


def is_rate_limit_reason(reason: str) -> bool:
    return reason.lower() in _RATE_LIMIT_REASONS_CASEFOLDED


def is_rate_limit_refusal(status_code: int, body_text: str | None) -> bool:
    if status_code != 403 or not body_text:
        return False
    try:
        payload = json.loads(body_text)
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    errors = error.get("errors") if isinstance(error, dict) else None
    if not isinstance(errors, list):
        return False
    return any(isinstance(item, dict) and is_rate_limit_reason(str(item.get("reason", ""))) for item in errors)


def error_from_response(
    response: httpx.Response,
    error_class: type[_GoogleAPIErrorT],
    *,
    default_message: str,
    body_text_when_message_missing: bool,
) -> _GoogleAPIErrorT:
    """A missing-scope 403 becomes reconnect_required; a body that is not JSON always falls back to its text."""
    status = response.status_code
    try:
        payload: Any = response.json() or {}
    except ValueError:
        return error_class(status=status, code=None, message=response.text[:500] or default_message)
    fallback = (response.text[:500] or default_message) if body_text_when_message_missing else default_message
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return error_class(status=status, code=None, message=fallback)
    message = error.get("message") or fallback
    details = error.get("errors")
    code = details[0].get("reason") if isinstance(details, list) and details and isinstance(details[0], dict) else None
    if status == 403 and code in _MISSING_SCOPE_REASONS:
        code = RECONNECT_REQUIRED_CODE
    return error_class(status=status, code=code, message=message)
