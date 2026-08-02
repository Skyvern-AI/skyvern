"""Field-name redaction shared by the request-logging middleware and the log processors.

Deliberately free of web-framework imports: ``forge_log`` runs this on every log
record, and starlette ships only in the ``local``/``server`` extras, so importing
it from here would break logging on a core ``pip install skyvern``.

Public surface (imported by ``forge_log``, ``request_logging`` and the copilot
tracing modules): ``REDACTED``, ``SENSITIVE_HEADERS``, ``SENSITIVE_FIELDS``,
``is_sensitive_key``, ``strip_artifact_url_query``, ``redact_bearer_tokens_in_text``
and ``redact_sensitive_fields``.
"""

from __future__ import annotations

import json
import re
import typing
from collections.abc import Mapping

from pydantic import BaseModel

from skyvern.utils.action_redaction import redact_action_payload_for_log

REDACTED = "****"

# Request headers dropped wholesale from request logs.  proxy-authorization and
# set-cookie carry credentials too and are classified as such by other modules.
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}

# Exact field names that are always redacted.  Use a set for O(1) lookup
# instead of regex substring matching to avoid false positives like
# credential_id, author, page_token, etc.  SENSITIVE_HEADERS is folded in so a
# name classified as a credential header cannot be missed when the same header
# dict arrives as a log kwarg instead of through the middleware.
SENSITIVE_FIELDS: set[str] = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "credential",
    "access_key",
    "private_key",
    "auth",
    "authorization",
    "extra_http_headers",
    "cdp_connect_headers",
    "secret_key",
    "totp",
    "cached_totp",
    "otp",
    "one_time_code",
    "one_time_password",
    "mfa_code",
    "verification_code",
    "totp_identifier",
    "totp_url",
    "totp_secret",
} | SENSITIVE_HEADERS

# Matches a signed artifact-content URL (absolute or path-relative) anywhere in a
# string and captures everything up to — but excluding — its query, so re.sub can
# drop the capability params (expiry/kid/sig) while leaving surrounding text and
# non-artifact URLs untouched. Query runs to the next whitespace/quote/angle bracket.
# Anchored on the literal path segment: an optional leading `scheme://host` group
# re-scanned and backtracked the alphanumeric run at every start position, which is
# quadratic in run length on strings that never match, and `re.sub` re-emitted that
# prefix verbatim anyway.
_ARTIFACT_CONTENT_URL_QUERY_RE = re.compile(r"(/v1/artifacts/[^/\s\"'<>]+/content/?)\?[^\s\"'<>]*")

# Bearer JWTs occasionally leak into log messages via WebSocket connection URLs that
# pass `?token=Bearer%20<jwt>` as a query string. The token class spans base64url,
# standard base64 (`+ / =`) and opaque values (`~`), so a credential is consumed whole.
_BEARER_TOKEN_RE = re.compile(r"(?i)(token=)(?:Bearer(?:%20|\s+))?[A-Za-z0-9._%~+/=-]+")
# `Authorization: Bearer <token>` / `Proxy-Authorization: Bearer <token>` header
# values (including dict-repr forms like `'authorization': 'Bearer ...'`). The whole
# credential after the scheme is dropped regardless of its shape: in a credential
# header there is no prose to protect, so all-alpha and short tokens are redacted too.
_AUTH_HEADER_BEARER_RE = re.compile(
    r"(?i)((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)Bearer(?:%20|\s+)[A-Za-z0-9._~+/=%-]+"
)
# Bare `Bearer <token>` inside exception strings / free text with no header context.
# Requires at least one non-alpha char and an 8-char minimum so prose like
# "Bearer authentication required" is preserved; header shapes are handled above.
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?i)\bBearer(?:%20|\s+)(?=[A-Za-z0-9._~+/=%-]*[0-9._~+/=%-])[A-Za-z0-9._~+/=%-]{8,}"
)

# Placeholder emitted when a container is revisited during the walk. Breaks cycles
# and de-duplicates shared sub-graphs so the walk stays O(nodes) rather than
# fanning out exponentially under the depth cap.
_CIRCULAR = "<circular>"


def is_sensitive_key(key: typing.Any, *, redact_metadata: bool = False) -> bool:
    # Reached with arbitrary structlog kwargs and parsed JSON, so a key is not
    # guaranteed to be a string; a non-str key can never match an exact name.
    if not isinstance(key, str):
        return False
    normalized = key.lower()
    return normalized in SENSITIVE_FIELDS or (redact_metadata and normalized == "metadata")


def strip_artifact_url_query(value: str) -> str:
    return _ARTIFACT_CONTENT_URL_QUERY_RE.sub(r"\1", value)


def redact_bearer_tokens_in_text(value: str) -> str:
    """Redact Bearer credentials from a single string.

    Covers `?token=Bearer%20<jwt>` query strings, `Authorization: Bearer <token>`
    (and `Proxy-Authorization`) header values of any token shape, and bare
    `Bearer <token>` runs in exception / free text (guarded against prose).
    """
    lowered = value.lower()
    if "token=" in lowered:
        value = _BEARER_TOKEN_RE.sub(r"\1<redacted>", value)
    if "authorization" in lowered:
        value = _AUTH_HEADER_BEARER_RE.sub(r"\1Bearer <redacted>", value)
    if "bearer" in lowered:
        value = _BEARER_CREDENTIAL_RE.sub("Bearer <redacted>", value)
    return value


def _redact_string(value: str) -> str:
    return redact_bearer_tokens_in_text(strip_artifact_url_query(value))


def redact_sensitive_fields(
    obj: typing.Any,
    _depth: int = 0,
    *,
    redact_metadata: bool = False,
    _seen: set[int] | None = None,
) -> typing.Any:
    """Redact sensitive fields, bearer credentials, and artifact-URL capability queries.

    Field names use exact-match (case-insensitive) rather than substring/regex
    to avoid false positives on fields like ``credential_id``, ``author``, or
    ``page_token`` which contain sensitive substrings but are not secrets. Bearer
    credentials embedded in nested string values are stripped too, so a header dict
    like ``{"Proxy-Authorization": "Bearer sk-..."}`` cannot slip through under a
    key name the middleware does not classify.
    """
    if isinstance(obj, str):
        sanitized_value = _redact_string(obj)
        if _depth > 20:
            return sanitized_value
        try:
            parsed_value = json.loads(sanitized_value)
        except (json.JSONDecodeError, TypeError):
            return sanitized_value
        if isinstance(parsed_value, (dict, list)):
            redacted_value = redact_sensitive_fields(
                parsed_value, _depth + 1, redact_metadata=redact_metadata, _seen=_seen
            )
            return json.dumps(redacted_value) if redacted_value != parsed_value else sanitized_value
        return sanitized_value

    # Bound total node count, not just depth: a shared or cyclic container fans out
    # exponentially under the depth cap on this synchronous per-record walk.
    if isinstance(obj, (BaseModel, Mapping, list, tuple, set, frozenset)):
        if _seen is None:
            _seen = set()
        marker = id(obj)
        if marker in _seen:
            return _CIRCULAR
        _seen.add(marker)

    if _depth > 20:
        # Stop recursing but still redact sensitive keys at this level. Any Mapping
        # (not just dict) so header containers such as httpx / starlette Headers,
        # MappingProxyType and CIMultiDict are covered.
        if isinstance(obj, Mapping):
            return {k: REDACTED if is_sensitive_key(k, redact_metadata=redact_metadata) else v for k, v in obj.items()}
        return obj
    if isinstance(obj, BaseModel):
        # A model kwarg is otherwise rendered in full by the log formatter, carrying
        # whatever sensitive fields it declares. model_dump bypasses the model's own
        # __repr__ OTP masking, so re-apply the action-payload redaction (masks
        # input_text `text`/`totp_*` in OTP context) before the generic field pass.
        try:
            dumped = obj.model_dump()
        except Exception:
            return obj
        dumped = redact_action_payload_for_log(dumped)
        return redact_sensitive_fields(dumped, _depth + 1, redact_metadata=redact_metadata, _seen=_seen)
    if isinstance(obj, Mapping):
        return {
            k: (
                REDACTED
                if is_sensitive_key(k, redact_metadata=redact_metadata)
                else redact_sensitive_fields(v, _depth + 1, redact_metadata=redact_metadata, _seen=_seen)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive_fields(item, _depth + 1, redact_metadata=redact_metadata, _seen=_seen) for item in obj]
    if isinstance(obj, tuple):
        return tuple(
            redact_sensitive_fields(item, _depth + 1, redact_metadata=redact_metadata, _seen=_seen) for item in obj
        )
    if isinstance(obj, (set, frozenset)):
        try:
            return type(obj)(
                redact_sensitive_fields(item, _depth + 1, redact_metadata=redact_metadata, _seen=_seen) for item in obj
            )
        except TypeError:
            # Redaction maps hashables to hashables except a frozen model, which
            # dumps to an unhashable dict.
            return obj
    return obj
