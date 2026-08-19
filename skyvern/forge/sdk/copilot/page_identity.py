from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from skyvern.config import settings
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt

_LOCATION_FINGERPRINT_DOMAIN = b"skyvern.copilot.page_location.v1"
_LOCATION_FALLBACK_SECRET = secrets.token_bytes(32)


def safe_page_origin(value: str | None) -> str | None:
    """Return only a model-safe HTTP origin, never URL-carried credentials or location state."""
    if not value:
        return None
    try:
        parsed = urlsplit(redact_raw_secrets_for_prompt(value))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme.lower(), netloc, "/", "", ""))


def page_location_fingerprint(value: str | None) -> str | None:
    """Return a keyed location identity without exposing path, query, userinfo, or fragment."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    location = urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    configured_key = settings.SECRET_KEY
    key = (
        configured_key.encode("utf-8")
        if configured_key and configured_key != type(settings).model_fields["SECRET_KEY"].default
        else _LOCATION_FALLBACK_SECRET
    )
    digest = hmac.new(
        key,
        _LOCATION_FINGERPRINT_DOMAIN + b"\0" + location.encode("utf-8", "surrogatepass"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


def page_record_matches_url(record: dict[str, Any], url: str) -> bool:
    record_url = str(record.get("current_url") or record.get("inspected_url") or record.get("url") or "").strip()
    record_origin = safe_page_origin(record_url)
    target_origin = safe_page_origin(url)
    record_fingerprint = record.get("current_url_location_fingerprint") or record.get("location_fingerprint")
    if not isinstance(record_fingerprint, str):
        record_fingerprint = page_location_fingerprint(record_url)
    target_fingerprint = page_location_fingerprint(url)
    return bool(
        record_origin
        and target_origin
        and record_origin == target_origin
        and record_fingerprint
        and target_fingerprint
        and record_fingerprint == target_fingerprint
    )


def page_records_share_location(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_url = str(first.get("current_url") or first.get("inspected_url") or first.get("url") or "").strip()
    second_url = str(second.get("current_url") or second.get("inspected_url") or second.get("url") or "").strip()
    first_origin = safe_page_origin(first_url)
    second_origin = safe_page_origin(second_url)
    first_fingerprint = first.get("current_url_location_fingerprint") or first.get("location_fingerprint")
    second_fingerprint = second.get("current_url_location_fingerprint") or second.get("location_fingerprint")
    if not isinstance(first_fingerprint, str):
        first_fingerprint = page_location_fingerprint(first_url)
    if not isinstance(second_fingerprint, str):
        second_fingerprint = page_location_fingerprint(second_url)
    return bool(
        first_origin
        and second_origin
        and first_origin == second_origin
        and first_fingerprint
        and second_fingerprint
        and first_fingerprint == second_fingerprint
    )
