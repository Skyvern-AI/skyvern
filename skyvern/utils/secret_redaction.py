import base64
import functools
import html
import json
import re
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from typing import Any

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
MIN_SECRET_LENGTH = 4
MIN_NUMERIC_SECRET_LENGTH = 6

SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
SENSITIVE_QUERY_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "otp",
        "totp",
        "mfa_code",
        "session",
        "session_id",
        "sessionid",
    }
)
SENSITIVE_FORM_FIELD_NAMES: frozenset[str] = SENSITIVE_QUERY_PARAM_NAMES | frozenset(
    {
        "cvv",
        "cvc",
        "cvv2",
        "cvc2",
        "security_code",
        "card_cvv",
        "cardcode",
        "card_number",
        "cardnumber",
        "card_no",
        "account_number",
        "ssn",
        "pin",
    }
)

_PLACEHOLDER_TOKEN_RE = re.compile(r"placeholder_\w+")
# Source constants: BitwardenConstants.TOTP, OnePasswordConstants.TOTP, AzureVaultConstants.TOTP.
_TOTP_SENTINEL_VALUES = frozenset({"BW_TOTP", "OP_TOTP", "AZ_TOTP"})


def clears_numeric_secret_floor(value: str) -> bool:
    """Whether an all-digit value is long enough to scrub without colliding with ordinary content.

    `_is_redactable_otp_value` deliberately omits this floor, so short runtime codes still redact in
    diagnostics. Callers scrubbing data the customer asked for apply it, so a 4-digit code cannot
    rewrite the year out of a date.
    """
    return not (value.isdigit() and len(value) < MIN_NUMERIC_SECRET_LENGTH)


def is_redactable_secret_value(value: Any, secrets: Mapping[str, Any]) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) < MIN_SECRET_LENGTH:
        return False
    if not clears_numeric_secret_floor(value):
        return False
    if value in secrets:
        return False
    return value not in _TOTP_SENTINEL_VALUES


def _is_redactable_otp_value(value: Any, secrets: Mapping[str, Any]) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) < MIN_SECRET_LENGTH:
        return False
    if value in secrets:
        return False
    return value not in _TOTP_SENTINEL_VALUES


def collect_redactable_secret_values(
    secrets: Mapping[str, Any], extra_values: Iterable[Any] = (), otp_values: Iterable[Any] = ()
) -> set[str]:
    values: set[str] = set()
    for value in [*secrets.values(), *extra_values]:
        if is_redactable_secret_value(value, secrets):
            values.add(value)
    for value in otp_values:
        if _is_redactable_otp_value(value, secrets):
            values.add(value)
    return values


def expand_secret_encodings(value: str) -> set[str]:
    return {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        json.dumps(value)[1:-1],
        html.escape(value, quote=True),
        base64.b64encode(value.encode()).decode(),
    }


@functools.lru_cache(maxsize=64)
def _compiled_secret_pattern(variants: frozenset[str], boundary_all_lengths: bool = False) -> re.Pattern[str]:
    sorted_variants = sorted(variants, key=len, reverse=True)
    pattern_parts = [
        rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])"
        if boundary_all_lengths or len(variant) < 8
        else re.escape(variant)
        for variant in sorted_variants
    ]
    return re.compile("|".join(pattern_parts))


def redact_secrets_from_text(text: str, secret_values: Collection[str], *, boundary_all_lengths: bool = False) -> str:
    """Replace every registered secret variant found in ``text`` with a placeholder.

    By default, secrets of 8+ characters match as a plain substring (matches inside a longer
    alphanumeric run too), which suits free-form prose logs. ``boundary_all_lengths=True`` anchors
    every secret length to token boundaries instead, for structured data where a short secret can
    legitimately be a substring of an unrelated longer value (e.g. "Sunshine1" inside "MySunshine1Co").
    """
    if not text or not secret_values:
        return text

    variants = {
        variant for secret_value in secret_values for variant in expand_secret_encodings(secret_value) if variant
    }
    if not variants:
        return text
    pattern = _compiled_secret_pattern(frozenset(variants), boundary_all_lengths)
    segments = re.split(f"({_PLACEHOLDER_TOKEN_RE.pattern})", text)
    for index, segment in enumerate(segments):
        if _PLACEHOLDER_TOKEN_RE.fullmatch(segment):
            continue
        segments[index] = pattern.sub(REDACTED_SECRET_PLACEHOLDER, segment)
    return "".join(segments)


def redact_secrets_from_bytes(data: bytes, secret_values: Collection[str]) -> bytes:
    text = data.decode("utf-8", errors="replace")
    return redact_secrets_from_text(text, secret_values).encode()


def redact_har_bytes(har_data: bytes, secret_values: Collection[str]) -> bytes:
    try:
        har = json.loads(har_data)
    except Exception:
        return redact_secrets_from_bytes(har_data, secret_values)

    original_serialized_har = json.dumps(har)
    log = har.get("log", {}) if isinstance(har, dict) else {}
    entries = log.get("entries", []) if isinstance(log, dict) else []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            request = entry.get("request", {})
            response = entry.get("response", {})
            if isinstance(request, dict):
                _redact_headers(request.get("headers", []))
                _redact_query_string(request.get("queryString", []))
                _redact_cookies(request.get("cookies", []))
                _redact_url_query_params(request)
                post_data = request.get("postData", {})
                if isinstance(post_data, dict):
                    _redact_form_fields(post_data.get("params", []))
                    _redact_urlencoded_post_data_text(post_data)
                _redact_base64_text(post_data, secret_values)
            if isinstance(response, dict):
                _redact_headers(response.get("headers", []))
                _redact_cookies(response.get("cookies", []))
                content = response.get("content", {})
                if isinstance(content, dict):
                    _redact_base64_text(content, secret_values)

    redacted_serialized_har = redact_secrets_from_text(json.dumps(har), secret_values)
    if redacted_serialized_har == original_serialized_har:
        return har_data
    return redacted_serialized_har.encode()


def redact_console_log_bytes(log_data: bytes, secret_values: Collection[str]) -> bytes:
    return redact_secrets_from_bytes(log_data, secret_values)


def _redact_headers(headers: Any) -> None:
    if not isinstance(headers, list):
        return
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        if isinstance(name, str) and name.lower() in SENSITIVE_HEADER_NAMES:
            header["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_query_string(query_string: Any) -> None:
    if not isinstance(query_string, list):
        return
    for query_param in query_string:
        if not isinstance(query_param, dict):
            continue
        name = query_param.get("name")
        if isinstance(name, str) and name.lower() in SENSITIVE_QUERY_PARAM_NAMES:
            query_param["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_form_fields(params: Any) -> None:
    if not isinstance(params, list):
        return
    for field in params:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if isinstance(name, str) and name.lower() in SENSITIVE_FORM_FIELD_NAMES:
            field["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_named_urlencoded_values(text: str, sensitive_names: frozenset[str]) -> str:
    try:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    except Exception:
        return text

    changed = False
    redacted_pairs: list[tuple[str, str]] = []
    for name, value in pairs:
        if name.lower() in sensitive_names:
            redacted_pairs.append((name, REDACTED_SECRET_PLACEHOLDER))
            changed = True
        else:
            redacted_pairs.append((name, value))
    if not changed:
        return text
    try:
        return urllib.parse.urlencode(redacted_pairs, safe="[]")
    except Exception:
        return text


def _redact_url_query_params(request: dict[str, Any]) -> None:
    url = request.get("url")
    if not isinstance(url, str):
        return
    try:
        split_url = urllib.parse.urlsplit(url)
    except Exception:
        return
    if not split_url.query:
        return
    redacted_query = _redact_named_urlencoded_values(split_url.query, SENSITIVE_QUERY_PARAM_NAMES)
    if redacted_query == split_url.query:
        return
    try:
        request["url"] = urllib.parse.urlunsplit(
            (split_url.scheme, split_url.netloc, split_url.path, redacted_query, split_url.fragment)
        )
    except Exception:
        return


def _redact_urlencoded_post_data_text(post_data: dict[str, Any]) -> None:
    mime_type = post_data.get("mimeType")
    text = post_data.get("text")
    if not isinstance(mime_type, str) or "application/x-www-form-urlencoded" not in mime_type.lower():
        return
    if not isinstance(text, str):
        return
    post_data["text"] = _redact_named_urlencoded_values(text, SENSITIVE_FORM_FIELD_NAMES)


def _redact_cookies(cookies: Any) -> None:
    if not isinstance(cookies, list):
        return
    for cookie in cookies:
        if isinstance(cookie, dict):
            cookie["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_base64_text(container: Any, secret_values: Collection[str]) -> None:
    if not isinstance(container, dict):
        return
    if container.get("encoding") != "base64":
        return
    encoded_text = container.get("text")
    if not isinstance(encoded_text, str):
        return
    try:
        decoded_bytes = base64.b64decode(encoded_text, validate=False)
    except Exception:
        return
    try:
        decoded_text = decoded_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return
    redacted_text = redact_secrets_from_text(decoded_text, secret_values)
    container["text"] = base64.b64encode(redacted_text.encode()).decode()
