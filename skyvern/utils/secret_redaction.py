import base64
import contextvars
import dataclasses
import functools
import html
import json
import re
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from typing import Any

import structlog

from skyvern.forge.sdk.core import skyvern_context
from skyvern.utils.raw_secret_redaction import (
    has_credential_shape,
    is_hex_credential,
    iter_auth_scheme_credentials,
    iter_jwt_runs,
)

LOG = structlog.get_logger()

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
# Brackets are not valid in a URL path, and path matchers built on `[A-Za-z0-9_.-]+` must still match a scrubbed one.
_PATH_SEGMENT_PLACEHOLDER = REDACTED_SECRET_PLACEHOLDER.strip("[]")
_PATH_PLACEHOLDER_RE = re.compile(re.escape(_PATH_SEGMENT_PLACEHOLDER) + r"(?:_[0-9]+)?")
# Stored in place of an artifact whose redaction failed. A JSON string, so JSON readers still parse it.
REDACTION_FAILED_STUB = b'"[REDACTED_SECRET] artifact withheld: secret redaction failed"'
MIN_SECRET_LENGTH = 4
MIN_NUMERIC_SECRET_LENGTH = 6
# Shorter variants only match on token boundaries, so they cannot redact inside an unrelated word.
MIN_UNANCHORED_SECRET_LENGTH = 8

# Names whose value is scrubbed out of a stored artifact, irreversibly. Distinct from the `***`
# mask in utils/secret_headers.py, which is a reversible API round-trip placeholder for every
# header a customer authored, so the two cannot be merged.
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
        "client_secret",
        "auth_token",
        "oauth_token",
        "code_verifier",
        "samlresponse",
    }
)
SENSITIVE_FORM_FIELD_NAMES: frozenset[str] = SENSITIVE_QUERY_PARAM_NAMES | frozenset(
    {
        "pass",
        "j_password",
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


def _normalized_name(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


# Names compare without case, `_` or `-`, so `accessToken` and `access-token` match `access_token`.
_NORMALIZED_HEADER_NAMES = frozenset(_normalized_name(name) for name in SENSITIVE_HEADER_NAMES)
_NORMALIZED_QUERY_PARAM_NAMES = frozenset(_normalized_name(name) for name in SENSITIVE_QUERY_PARAM_NAMES)
_NORMALIZED_FORM_FIELD_NAMES = frozenset(_normalized_name(name) for name in SENSITIVE_FORM_FIELD_NAMES)
_URL_HEADER_NAMES = frozenset({"referer", "location", "contentlocation", ":path"})
_BRACKETED_PART_RE = re.compile(r"\[([^\[\]]*)\]")


def _leaf_name(name: str) -> str:
    """The field a structured form name ends in: `user[password]` and `a[b][token]` by the last non-empty bracket,
    `ctl00$Password` after the last `$`."""
    if name.endswith("]"):
        leaves = [part for part in _BRACKETED_PART_RE.findall(name) if part]
        name = leaves[-1] if leaves else name.split("[", 1)[0]
    return name.rpartition("$")[2]


def _is_sensitive_name(name: str, normalized_names: frozenset[str]) -> bool:
    return _normalized_name(name) in normalized_names or _normalized_name(_leaf_name(name)) in normalized_names


def _is_sensitive_header_name(name: str) -> bool:
    # A header also carries a field name such as `token` or `x-access-token`, as the truncated-HAR scrub already reads.
    unprefixed = name[2:] if name.lower().startswith("x-") else name
    return _normalized_name(name) in _NORMALIZED_HEADER_NAMES or _is_sensitive_name(
        unprefixed, _NORMALIZED_FORM_FIELD_NAMES
    )


_JSON_STRING_BODY = r'(?:[^"\\]|\\.)*'
# Whole string tokens or runs between them, so a scan never restarts inside a string: linear on any input.
_JSON_TEXT_TOKEN_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?|[^"]+')
_JSON_KEY_SEPARATOR_RE = re.compile(r"(\s*:\s*)(-?[0-9][0-9.eE+-]*)?")
_JSON_NUMBER_RE = re.compile(r"-?[0-9][0-9.eE+-]*")
_JSON_STRUCTURE_PIECE_RE = re.compile(r"[\[\]{}]|-?[0-9][0-9.eE+-]*|[^\[\]{}0-9-]+|-")
_JSON_OPENERS = frozenset("{[")
_JSON_CLOSERS = frozenset("}]")
_DIGEST_RESPONSE_VALUE_RE = re.compile(r'(?:\\?")?[^"\\,\r\n]{0,256}')
_JSON_TEXT_LEADING_CHARS = "\ufeff \t\r\n"
_MAX_PERCENT_DECODE_ROUNDS = 3
_TRAILING_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,5}\Z")
# A quoted or `=`-delimited value, so the text scan applies the same whole-value hex rule as the parsed walk.
_DELIMITED_HEX_VALUE_RE = re.compile(r'(?<=")[0-9A-Fa-f]{32,}(?=\\?")|(?<==)[0-9A-Fa-f]{32,}(?![A-Za-z0-9_.~%+-])')
_MAX_BASE64_POST_DATA_CHARS = 16 * 1024 * 1024
_BASE64_HEAD_CHARS = 4096


def _name_alternatives(names: frozenset[str]) -> str:
    """Pattern text for the sensitive names under the same normalization: any case, an optional `_` or `-` anywhere."""
    normalized = sorted({_normalized_name(name) for name in names}, key=len, reverse=True)
    return "|".join("[_-]?".join(re.escape(char) for char in name) for name in normalized)


_SENSITIVE_NAME_VALUE_PAIR_RE = re.compile(
    r'("name"\s*:\s*"(?:x[_-]?)?(?:'
    + _name_alternatives(SENSITIVE_HEADER_NAMES | SENSITIVE_FORM_FIELD_NAMES)
    + r')"\s*,\s*"value"\s*:\s*")'
    + _JSON_STRING_BODY,
    re.IGNORECASE,
)
_JSON_VALUE_FIELD_RE = re.compile(r'("value"\s*:\s*")' + _JSON_STRING_BODY)
_COOKIES_ARRAY_START_RE = re.compile(r'"cookies"\s*:\s*\[')
_JSON_ARRAY_TOKEN_RE = re.compile(r'"' + _JSON_STRING_BODY + r'"?|[\[\]]|[^"\[\]]+')


def _named_urlencoded_value_re(names: frozenset[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_.\-])((?:" + _name_alternatives(names) + r")=)[^&#\"\s\\]*", re.IGNORECASE)


_NAMED_FORM_VALUE_RE = _named_urlencoded_value_re(SENSITIVE_FORM_FIELD_NAMES)
_NAMED_QUERY_VALUE_RE = _named_urlencoded_value_re(SENSITIVE_QUERY_PARAM_NAMES)

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


def collect_secret_values_at_any_length(secrets: Mapping[str, Any], otp_values: Iterable[Any] = ()) -> set[str]:
    """For a consumer that replaces exact values and applies no floor of its own. Placeholder ids and
    TOTP routing markers are not secret values, so they stay out."""
    return {
        value
        for value in [*secrets.values(), *otp_values]
        if isinstance(value, str) and value and value not in secrets and value not in _TOTP_SENTINEL_VALUES
    }


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
def _compiled_secret_pattern(
    variants: frozenset[str], boundary_all_lengths: bool = False, match_inside_words: bool = False
) -> re.Pattern[str]:
    sorted_variants = sorted(variants, key=len, reverse=True)
    pattern_parts = [
        rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])"
        if not match_inside_words and (boundary_all_lengths or len(variant) < MIN_UNANCHORED_SECRET_LENGTH)
        else re.escape(variant)
        for variant in sorted_variants
    ]
    return re.compile("|".join(pattern_parts))


@functools.lru_cache(maxsize=64)
def _compiled_placeholder_id_pattern(placeholder_ids: frozenset[str]) -> re.Pattern[str]:
    # Longest first so an id that prefixes another ("placeholder_ab12" before
    # "placeholder_ab12_password") cannot leave the suffix exposed to the secret pattern.
    return re.compile("|".join(re.escape(token) for token in sorted(placeholder_ids, key=len, reverse=True)))


def redact_secrets_from_text(
    text: str,
    secret_values: Collection[str],
    *,
    boundary_all_lengths: bool = False,
    match_inside_words: bool = False,
    placeholder_ids: Collection[str] = (),
) -> str:
    """Replace every registered secret variant found in ``text`` with a placeholder.

    By default, secrets of 8+ characters match as a plain substring (matches inside a longer
    alphanumeric run too), which suits free-form prose logs. ``boundary_all_lengths=True`` anchors
    every secret length to token boundaries instead, for structured data where a short secret can
    legitimately be a substring of an unrelated longer value (e.g. "Sunshine1" inside "MySunshine1Co").
    ``match_inside_words=True`` scans every variant even inside words and placeholder-like tokens,
    for rejecting values that must never carry a registered secret.

    ``placeholder_ids`` are the run's registered secret ids, exempted so a resolvable token survives
    the scrub (a credential field name such as ``placeholder_ab12_password`` would otherwise be
    rewritten by a secret whose value is literally "password"). The exemption is exact: text that
    merely looks like an id is redacted like any other text, so a secret rendered straight after the
    literal ``placeholder_`` prefix cannot ride out on the shape alone.
    """
    if not text or not secret_values:
        return text

    variants = {
        variant for secret_value in secret_values for variant in expand_secret_encodings(secret_value) if variant
    }
    if not variants:
        return text
    pattern = _compiled_secret_pattern(frozenset(variants), boundary_all_lengths, match_inside_words)
    exempt_ids = frozenset(token for token in placeholder_ids if token)
    if match_inside_words or not exempt_ids:
        return pattern.sub(REDACTED_SECRET_PLACEHOLDER, text)
    segments: list[str] = []
    scanned = 0
    for match in _compiled_placeholder_id_pattern(exempt_ids).finditer(text):
        segments.append(pattern.sub(REDACTED_SECRET_PLACEHOLDER, text[scanned : match.start()]))
        segments.append(match.group())
        scanned = match.end()
    segments.append(pattern.sub(REDACTED_SECRET_PLACEHOLDER, text[scanned:]))
    return "".join(segments)


def redact_multi_field_totp_artifact_bytes(data: bytes, placeholder_ids: Collection[str] = ()) -> bytes:
    task_ids = skyvern_context.multi_field_totp_masking_task_ids()
    if not task_ids:
        return data
    text = data.decode("utf-8", errors="replace")
    forms = set().union(*(skyvern_context._multi_field_totp_mask_context(task_id)[2] for task_id in task_ids))
    masked = text
    if skyvern_context.multi_field_totp_artifact_scan_allowed(len(data)):
        try:
            decoded = json.loads(text) if text.lstrip().startswith(("{", "[", '"')) else None
        except (ValueError, RecursionError):
            decoded = None
        if decoded is not None:

            def mask_value(value: Any) -> Any:
                if isinstance(value, str):
                    return redact_secrets_from_text(
                        skyvern_context.mask_multi_field_totp_artifact_text(
                            value, replacement=REDACTED_SECRET_PLACEHOLDER
                        ),
                        forms,
                        placeholder_ids=placeholder_ids,
                    )
                if isinstance(value, dict):
                    return {key: mask_value(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [mask_value(item) for item in value]
                return value

            masked_value = mask_value(decoded)
            if masked_value != decoded:
                masked = json.dumps(masked_value)
        else:
            masked = skyvern_context.mask_multi_field_totp_artifact_text(text, replacement=REDACTED_SECRET_PLACEHOLDER)
    literal_forms = {form for form in forms if any(variant in masked for variant in expand_secret_encodings(form))}
    masked = redact_secrets_from_text(masked, literal_forms, placeholder_ids=placeholder_ids)
    return data if masked == text else masked.encode()


def redact_secrets_from_bytes(
    data: bytes,
    secret_values: Collection[str],
    *,
    multi_field_totp: bool = True,
    placeholder_ids: Collection[str] = (),
) -> bytes:
    if multi_field_totp:
        data = redact_multi_field_totp_artifact_bytes(data, placeholder_ids)
    text = data.decode("utf-8", errors="replace")
    return redact_secrets_from_text(text, secret_values, placeholder_ids=placeholder_ids).encode()


def redact_har_bytes(
    har_data: bytes,
    secret_values: Collection[str],
    *,
    multi_field_totp: bool = True,
    placeholder_ids: Collection[str] = (),
) -> bytes:
    if not har_data.strip():
        return har_data
    try:
        har = json.loads(har_data)
    except Exception:
        # A HAR cut short (a killed browser or worker) still holds header and cookie values.
        LOG.warning("HAR is not valid JSON; storing it with a pattern scrub", artifact_type="har")
        # Exact values go first: the name scrub stops at a space or quote and would split a registered value.
        scrubbed = redact_secrets_from_bytes(
            har_data, secret_values, multi_field_totp=multi_field_totp, placeholder_ids=placeholder_ids
        )
        return _pattern_scrub_har_text(scrubbed.decode()).encode()

    original_serialized_har = json.dumps(har)
    base64_containers: set[int] = set()

    def redact_body(container: Any) -> None:
        if (
            isinstance(container, dict)
            and container.get("encoding") == "base64"
            and isinstance(container.get("text"), str)
        ):
            _redact_base64_text(
                container, secret_values, multi_field_totp=multi_field_totp, placeholder_ids=placeholder_ids
            )
            base64_containers.add(id(container))

    log = har.get("log", {}) if isinstance(har, dict) else {}
    entries = log.get("entries", []) if isinstance(log, dict) else []
    fallback_state = _HarRedactionState(secret_values, multi_field_totp, placeholder_ids)
    state_token = _HAR_REDACTION_STATE.set(fallback_state)
    try:
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
                    if isinstance(request.get("url"), str):
                        request["url"] = _scrub_url(request["url"])
                    post_data = request.get("postData", {})
                    if isinstance(post_data, dict):
                        _redact_form_fields(post_data.get("params", []))
                        _redact_urlencoded_post_data_text(post_data)
                        _redact_json_post_data_text(post_data)
                        _redact_base64_post_data_text(post_data)
                    redact_body(post_data)
                if isinstance(response, dict):
                    _redact_headers(response.get("headers", []))
                    _redact_cookies(response.get("cookies", []))
                    if isinstance(response.get("redirectURL"), str):
                        response["redirectURL"] = _scrub_url(response["redirectURL"])
                    content = response.get("content", {})
                    if isinstance(content, dict):
                        redact_body(content)
    finally:
        _HAR_REDACTION_STATE.reset(state_token)
    if fallback_state.count:
        LOG.warning(
            "Urlencoded HAR values did not parse; stored them with a pattern scrub",
            artifact_type="har",
            count=fallback_state.count,
        )

    if base64_containers:
        task_ids = skyvern_context.multi_field_totp_masking_task_ids() if multi_field_totp else set()
        forms = set().union(*(skyvern_context._multi_field_totp_mask_context(task_id)[2] for task_id in task_ids))
        scan_allowed = bool(task_ids) and skyvern_context.multi_field_totp_artifact_scan_allowed(len(har_data))
        combined_secrets = set(secret_values) | forms

        def mask_text(text: str) -> str:
            if scan_allowed:
                text = skyvern_context.mask_multi_field_totp_artifact_text(
                    text, replacement=REDACTED_SECRET_PLACEHOLDER
                )
            return redact_secrets_from_text(text, combined_secrets, placeholder_ids=placeholder_ids)

        def mask_value(value: Any) -> Any:
            if isinstance(value, str):
                return mask_text(value)
            if isinstance(value, dict):
                return {
                    key if id(value) in base64_containers and key in {"text", "encoding"} else mask_text(key): item
                    if id(value) in base64_containers and key in {"text", "encoding"}
                    else mask_value(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [mask_value(item) for item in value]
            return value

        redacted_serialized_har = json.dumps(mask_value(har))
    else:
        serialized = json.dumps(har).encode()
        if multi_field_totp:
            serialized = redact_multi_field_totp_artifact_bytes(serialized, placeholder_ids)
        redacted_serialized_har = redact_secrets_from_text(
            serialized.decode(), secret_values, placeholder_ids=placeholder_ids
        )
    if redacted_serialized_har == original_serialized_har:
        return har_data
    return redacted_serialized_har.encode()


def redact_console_log_bytes(
    log_data: bytes, secret_values: Collection[str], *, placeholder_ids: Collection[str] = ()
) -> bytes:
    return redact_secrets_from_bytes(log_data, secret_values, placeholder_ids=placeholder_ids)


def _pattern_scrub_har_text(text: str) -> str:
    """Name-based HAR scrub for text that does not parse as JSON; errs toward removing values."""
    text = _SENSITIVE_NAME_VALUE_PAIR_RE.sub(lambda m: m.group(1) + REDACTED_SECRET_PLACEHOLDER, text)
    text = _NAMED_FORM_VALUE_RE.sub(lambda m: m.group(1) + REDACTED_SECRET_PLACEHOLDER, text)
    text = _redact_credential_shapes_in_text(text)
    parts: list[str] = []
    position = 0
    while (start := _COOKIES_ARRAY_START_RE.search(text, position)) is not None:
        end = len(text)
        depth = 1
        for token in _JSON_ARRAY_TOKEN_RE.finditer(text, start.end()):
            if token.group() == "[":
                depth += 1
            elif token.group() == "]":
                depth -= 1
                if depth == 0:
                    end = token.start()
                    break
        parts.append(text[position : start.end()])
        parts.append(
            _JSON_VALUE_FIELD_RE.sub(lambda m: m.group(1) + REDACTED_SECRET_PLACEHOLDER, text[start.end() : end])
        )
        position = end
    parts.append(text[position:])
    return "".join(parts)


@dataclasses.dataclass
class _HarRedactionState:
    secret_values: Collection[str]
    multi_field_totp: bool
    placeholder_ids: Collection[str]
    count: int = 0
    # Ordinals, never a hash of the token: distinct tokens stay distinct URLs, equal tokens stay equal.
    path_placeholders: dict[str, str] = dataclasses.field(default_factory=dict)


_HAR_REDACTION_STATE: contextvars.ContextVar[_HarRedactionState | None] = contextvars.ContextVar(
    "har_redaction_state", default=None
)


def _pattern_scrub_urlencoded(text: str, pattern: re.Pattern[str]) -> str:
    state = _HAR_REDACTION_STATE.get()
    if state is None:
        LOG.warning("Urlencoded HAR value did not parse; storing it with a pattern scrub", artifact_type="har")
    else:
        state.count += 1
        # Exact values go first: the name pattern stops at a space or quote and would split a registered value.
        text = redact_secrets_from_bytes(
            text.encode(),
            state.secret_values,
            multi_field_totp=state.multi_field_totp,
            placeholder_ids=state.placeholder_ids,
        ).decode()
    return pattern.sub(lambda m: m.group(1) + REDACTED_SECRET_PLACEHOLDER, text)


def _redact_headers(headers: Any) -> None:
    if not isinstance(headers, list):
        return
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        if not isinstance(name, str):
            continue
        normalized = _normalized_name(name)
        value = header.get("value")
        if _is_sensitive_header_name(name):
            if value != REDACTED_SECRET_PLACEHOLDER:
                header["value"] = REDACTED_SECRET_PLACEHOLDER
        elif not isinstance(value, str):
            continue
        elif normalized in _URL_HEADER_NAMES:
            header["value"] = _scrub_url(value)
        elif has_credential_shape(value):
            header["value"] = REDACTED_SECRET_PLACEHOLDER


def _is_sensitive_pair(name: Any, value: Any, normalized_names: frozenset[str]) -> bool:
    if value == REDACTED_SECRET_PLACEHOLDER:
        return False
    if isinstance(name, str) and _is_sensitive_name(name, normalized_names):
        return True
    return isinstance(value, str) and has_credential_shape(value)


def _redact_query_string(query_string: Any) -> None:
    if not isinstance(query_string, list):
        return
    for query_param in query_string:
        if isinstance(query_param, dict) and _is_sensitive_pair(
            query_param.get("name"), query_param.get("value"), _NORMALIZED_QUERY_PARAM_NAMES
        ):
            query_param["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_form_fields(params: Any) -> None:
    if not isinstance(params, list):
        return
    for field in params:
        if not isinstance(field, dict):
            continue
        if _is_sensitive_pair(field.get("name"), field.get("value"), _NORMALIZED_FORM_FIELD_NAMES):
            field["value"] = REDACTED_SECRET_PLACEHOLDER
        # A JSON body posted under a form mime type is parsed into one param whose name is the whole body.
        for key in ("name", "value"):
            if isinstance(field.get(key), str) and _looks_like_json(field[key]):
                field[key] = _scrub_json_text(field[key])


def _redact_named_urlencoded_values(text: str, normalized_names: frozenset[str], fallback: re.Pattern[str]) -> str:
    try:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    except Exception:
        return _pattern_scrub_urlencoded(text, fallback)

    changed = False
    redacted_pairs: list[tuple[str, str]] = []
    for name, value in pairs:
        if _is_sensitive_pair(name, value, normalized_names):
            redacted_pairs.append((name, REDACTED_SECRET_PLACEHOLDER))
            changed = True
        else:
            redacted_pairs.append((name, value))
    if not changed:
        return text
    try:
        return urllib.parse.urlencode(redacted_pairs, safe="[]")
    except Exception:
        return _pattern_scrub_urlencoded(text, fallback)


def _percent_decoded(text: str) -> str:
    for _ in range(_MAX_PERCENT_DECODE_ROUNDS):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _path_piece_has_credential(piece: str) -> bool:
    return has_credential_shape(_percent_decoded(piece))


def _scrub_path_piece(piece: str) -> str:
    """The decision reads the decoded piece, but the whole raw piece is replaced, keeping an extension the stem had."""
    if not _path_piece_has_credential(piece):
        return piece
    extension = _TRAILING_EXTENSION_RE.search(piece)
    if extension and _path_piece_has_credential(piece[: extension.start()]):
        return _path_placeholder(piece[: extension.start()]) + extension.group()
    return _path_placeholder(piece)


def _path_placeholder(token: str) -> str:
    state = _HAR_REDACTION_STATE.get()
    if state is None:
        return _PATH_SEGMENT_PLACEHOLDER
    key = _percent_decoded(token)
    if key not in state.path_placeholders:
        state.path_placeholders[key] = f"{_PATH_SEGMENT_PLACEHOLDER}_{len(state.path_placeholders) + 1}"
    return state.path_placeholders[key]


def _scrub_url_path(path: str) -> str:
    segments: list[str] = []
    for segment in path.split("/"):
        head, *matrix_params = segment.split(";")
        pieces = [_scrub_path_piece(head)]
        for param in matrix_params:
            name, separator, value = param.partition("=")
            if not separator:
                pieces.append(_scrub_path_piece(param))
            elif not _PATH_PLACEHOLDER_RE.fullmatch(value) and (
                _is_sensitive_pair(name, value, _NORMALIZED_QUERY_PARAM_NAMES) or _path_piece_has_credential(value)
            ):
                pieces.append(f"{name}={_path_placeholder(value)}")
            else:
                pieces.append(param)
        segments.append(";".join(pieces))
    return "/".join(segments)


def _scrub_url_query(query: str) -> str:
    return _redact_named_urlencoded_values(query, _NORMALIZED_QUERY_PARAM_NAMES, _NAMED_QUERY_VALUE_RE)


def _scrub_url_fragment(fragment: str) -> str:
    route, separator, params = fragment.partition("?")
    if separator:
        return f"{_scrub_url_path(route)}?{_scrub_url_query(params)}"
    equals = fragment.find("=")
    if equals < 0:
        return _scrub_url_path(fragment)
    # A route such as `#/cb/access_token=...` carries its params after the last `/` before the first `=`.
    slash = fragment.rfind("/", 0, equals)
    route, params = fragment[: slash + 1], fragment[slash + 1 :]
    return _scrub_url_path(route) + _scrub_url_query(params)


def _scrub_userinfo_password(netloc: str) -> str:
    """`user:pass@host` keeps the user and host; the bracket-free placeholder keeps the authority parseable."""
    userinfo, at, host = netloc.rpartition("@")
    user, colon, password = userinfo.partition(":")
    if not at or not colon or not password or password == _PATH_SEGMENT_PLACEHOLDER:
        return netloc
    return f"{user}:{_PATH_SEGMENT_PLACEHOLDER}@{host}"


def _scrub_url(url: str) -> str:
    """Scrubs request.url, redirectURL and the Referer, Location, Content-Location and `:path` headers alike, so a
    redirect target still equals its request."""
    try:
        split_url = urllib.parse.urlsplit(url)
    except Exception:
        return _redact_credential_shapes_in_text(_pattern_scrub_urlencoded(url, _NAMED_QUERY_VALUE_RE))
    netloc = _scrub_userinfo_password(split_url.netloc)
    path = _scrub_url_path(split_url.path)
    query = _scrub_url_query(split_url.query)
    fragment = _scrub_url_fragment(split_url.fragment)
    if (netloc, path, query, fragment) == (split_url.netloc, split_url.path, split_url.query, split_url.fragment):
        return url
    try:
        return urllib.parse.urlunsplit((split_url.scheme, netloc, path, query, fragment))
    except Exception:
        return _redact_credential_shapes_in_text(_pattern_scrub_urlencoded(url, _NAMED_QUERY_VALUE_RE))


def _redact_urlencoded_post_data_text(post_data: dict[str, Any]) -> None:
    mime_type = post_data.get("mimeType")
    text = post_data.get("text")
    if not isinstance(mime_type, str) or "application/x-www-form-urlencoded" not in mime_type.lower():
        return
    if post_data.get("encoding") == "base64" or not isinstance(text, str) or _looks_like_json(text):
        return
    post_data["text"] = _redact_named_urlencoded_values(text, _NORMALIZED_FORM_FIELD_NAMES, _NAMED_FORM_VALUE_RE)


def _looks_like_json(text: str) -> bool:
    return text.lstrip(_JSON_TEXT_LEADING_CHARS).startswith(("{", "["))


def _scrub_json_value(node: Any, under_sensitive_key: bool) -> tuple[Any, bool]:
    if isinstance(node, dict):
        changed = False
        scrubbed: dict[str, Any] = {}
        for key, value in node.items():
            sensitive = under_sensitive_key or _is_sensitive_name(key, _NORMALIZED_FORM_FIELD_NAMES)
            scrubbed[key], value_changed = _scrub_json_value(value, sensitive)
            changed = changed or value_changed
        return scrubbed, changed
    if isinstance(node, list):
        items = [_scrub_json_value(item, under_sensitive_key) for item in node]
        return [item for item, _ in items], any(item_changed for _, item_changed in items)
    if isinstance(node, str):
        if node != REDACTED_SECRET_PLACEHOLDER and (under_sensitive_key or has_credential_shape(node)):
            return REDACTED_SECRET_PLACEHOLDER, True
        return node, False
    if under_sensitive_key and isinstance(node, (int, float)) and not isinstance(node, bool):
        return REDACTED_SECRET_PLACEHOLDER, True
    return node, False


def _json_key(token: str) -> str:
    try:
        key = json.loads(token if len(token) > 1 and token.endswith('"') else token + '"')
    except ValueError:
        return token[1:]
    return key if isinstance(key, str) else token[1:]


def _scrub_unparsed_json_text(text: str) -> str:
    """Blanks the value under a sensitive key, and every string and number inside it when the value is a container."""
    tokens = [match.group() for match in _JSON_TEXT_TOKEN_RE.finditer(text)]
    placeholder = f'"{REDACTED_SECRET_PLACEHOLDER}"'
    parts: list[str] = []
    depth = 0
    after_sensitive_key = False
    value_is_next_string = False

    def scan_structure(piece: str) -> str:
        nonlocal depth
        pieces: list[str] = []
        for match in _JSON_STRUCTURE_PIECE_RE.finditer(piece):
            part = match.group()
            if part in _JSON_OPENERS:
                depth += 1
            elif part in _JSON_CLOSERS:
                depth = max(depth - 1, 0)
            elif depth and _JSON_NUMBER_RE.fullmatch(part):
                part = placeholder
            pieces.append(part)
        return "".join(pieces)

    for index, token in enumerate(tokens):
        if token.startswith('"'):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            is_key = _JSON_KEY_SEPARATOR_RE.match(following) is not None
            if (depth and not is_key) or value_is_next_string:
                parts.append(placeholder)
            else:
                parts.append(token)
            value_is_next_string = False
            after_sensitive_key = (
                is_key and not depth and _is_sensitive_name(_json_key(token), _NORMALIZED_FORM_FIELD_NAMES)
            )
            continue
        value_is_next_string = False
        if not after_sensitive_key:
            parts.append(scan_structure(token) if depth else token)
            continue
        after_sensitive_key = False
        separator = _JSON_KEY_SEPARATOR_RE.match(token)
        if separator is None:
            parts.append(token)
            continue
        parts.append(separator.group(1))
        rest = token[separator.end(1) :]
        if separator.group(2):
            parts.append(placeholder)
            parts.append(rest[len(separator.group(2)) :])
        elif not rest:
            value_is_next_string = True
        elif rest[0] in _JSON_OPENERS:
            parts.append(scan_structure(rest))
        else:
            parts.append(rest)
    return _redact_credential_shapes_in_text("".join(parts))


def _scrub_json_text(text: str) -> str:
    duplicate_keys = False

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate_keys
        duplicate_keys = duplicate_keys or len({key for key, _ in pairs}) != len(pairs)
        return dict(pairs)

    try:
        body, changed = _scrub_json_value(json.loads(text.lstrip("﻿"), object_pairs_hook=pairs_hook), False)
    except (ValueError, RecursionError):
        return _scrub_unparsed_json_text(text)
    # A duplicate key's earlier value is dropped by parsing, so the text is rewritten even when nothing else changed.
    if changed or duplicate_keys:
        return json.dumps(body, ensure_ascii=False)
    return text


def _redact_json_post_data_text(post_data: dict[str, Any]) -> None:
    text = post_data.get("text")
    if post_data.get("encoding") == "base64" or not isinstance(text, str):
        return
    mime_type = post_data.get("mimeType")
    if _looks_like_json(text) or (isinstance(mime_type, str) and "json" in mime_type.lower()):
        post_data["text"] = _scrub_json_text(text)


def _redact_base64_post_data_text(post_data: dict[str, Any]) -> None:
    """Runs the urlencoded and JSON passes on the decoded body, re-encoding only when they changed it."""
    text = post_data.get("text")
    if post_data.get("encoding") != "base64" or not isinstance(text, str):
        return
    if len(text) > _MAX_BASE64_POST_DATA_CHARS:
        # Too large to decode here; a body the structural passes would read is withheld rather than stored unscrubbed.
        try:
            head: str | None = base64.b64decode(text[:_BASE64_HEAD_CHARS]).decode("utf-8", errors="ignore")
        except ValueError:
            head = None
        mime_type = post_data.get("mimeType")
        mime = mime_type.lower() if isinstance(mime_type, str) else ""
        if head is None or "json" in mime or "application/x-www-form-urlencoded" in mime or _looks_like_json(head):
            post_data.pop("encoding")
            post_data["text"] = REDACTED_SECRET_PLACEHOLDER
        return
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except ValueError:
        return
    plain: dict[str, Any] = {"mimeType": post_data.get("mimeType"), "text": decoded}
    _redact_urlencoded_post_data_text(plain)
    _redact_json_post_data_text(plain)
    if plain["text"] != decoded:
        post_data["text"] = base64.b64encode(plain["text"].encode()).decode()


def _redact_credential_shapes_in_text(text: str) -> str:
    spans = [match.span() for match in iter_jwt_runs(text)]
    spans += [match.span() for match in _DELIMITED_HEX_VALUE_RE.finditer(text) if is_hex_credential(match.group())]
    for match in iter_auth_scheme_credentials(text):
        if match.group("digest"):
            value = _DIGEST_RESPONSE_VALUE_RE.match(text, match.end())
            spans.append((match.end(), value.end() if value else match.end()))
        else:
            spans.append(match.span("token") if match.group("token") is not None else match.span("basic"))
    if not spans:
        return text
    parts: list[str] = []
    position = 0
    for start, end in sorted(spans):
        if end <= position or start == end:
            continue
        start = max(start, position)
        parts.append(text[position:start])
        parts.append(REDACTED_SECRET_PLACEHOLDER)
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _redact_cookies(cookies: Any) -> None:
    if not isinstance(cookies, list):
        return
    for cookie in cookies:
        if isinstance(cookie, dict):
            cookie["value"] = REDACTED_SECRET_PLACEHOLDER


def _redact_base64_text(
    container: Any,
    secret_values: Collection[str],
    *,
    multi_field_totp: bool = True,
    placeholder_ids: Collection[str] = (),
) -> None:
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
        redacted_text = redact_secrets_from_bytes(
            encoded_text.encode(),
            secret_values,
            multi_field_totp=multi_field_totp,
            placeholder_ids=placeholder_ids,
        ).decode()
        if redacted_text != encoded_text:
            container["text"] = base64.b64encode(redacted_text.encode()).decode()
        return
    try:
        decoded_text = decoded_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return
    redacted_text = redact_secrets_from_bytes(
        decoded_text.encode(), secret_values, multi_field_totp=multi_field_totp, placeholder_ids=placeholder_ids
    ).decode()
    container["text"] = base64.b64encode(redacted_text.encode()).decode()
