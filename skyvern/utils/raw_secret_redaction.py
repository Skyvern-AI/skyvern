from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from typing import TypeVar, cast

from email_validator import EmailNotValidError, validate_email

from skyvern.utils.secret_headers import SECRET_HEADER_MASK

# The `token` keyword is guarded by negative lookbehinds so pagination cursors
# (next_token, page_token, continuation_token, ...), which are not credentials,
# aren't matched as secrets by either detection or redaction.
_SECRET_KEYWORD = (
    r"(?:^|(?<=[^A-Za-z0-9]))(?:[A-Za-z0-9]+_){0,8}"
    r"(?:password|passcode|api[_ -]?key|secret|bearer|authorization"
    r"|(?<!next_)(?<!prev_)(?<!previous_)(?<!page_)(?<!continuation_)(?<!cursor_)token)"
)
SECRET_KEYWORD_ASSIGNMENT_PATTERN = re.compile(
    # Consume an optional auth scheme word so `Authorization: Bearer <token>`
    # redacts the token, not just the scheme.
    _SECRET_KEYWORD + r"\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?\S+",
    re.I,
)
# A structured label carrying the keyword anywhere ("Password (required)"), for callers that already
# hold the label apart from its value and so cannot rely on the assignment shape above.
SECRET_KEYWORD_LABEL_PATTERN = re.compile(_SECRET_KEYWORD + r"(?![A-Za-z0-9])", re.I)
RAW_SECRET_PATTERNS = (
    SECRET_KEYWORD_ASSIGNMENT_PATTERN,
    re.compile(
        r"\b(?:otp|totp|mfa|2fa|verification|auth(?:entication)? code)(?:\s+code)?\s*(?:is|[:=])?\s*\d{6,8}\b",
        re.I,
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS = (",", ";", "|")
_COLON_DELIMITED_SECRET_EDGE_CHARS = "\"'`()[]{}<>"


_TOTP_DIAGNOSTIC = re.compile(r"\b(totp_identifier|totp_verification_url)(?:\s*=|[\"']?\s*:\s*(?=[\"']))")
_TOTP_FIELDS = frozenset({"totp_identifier", "totp_verification_url"})
_DIAGNOSTIC_FIELDS = frozenset(
    {
        "reason",
        "reasoning",
        "intention",
        "error",
        "errors",
        "error_message",
        "failure_reason",
        "terminate_reason",
        "terminated_reason",
        "display_reason",
        "workflow_error_message",
        "traceback",
        "exception",
        "action_trace",
        "action_trace_summary",
        "reviewer_output",
        "logs",
    }
)
_T = TypeVar("_T")


def redact_totp_runtime_values(node: _T, *, diagnostic: bool | None = None) -> _T:
    if isinstance(node, str):
        fields = sorted({match[1] for match in _TOTP_DIAGNOSTIC.finditer(node)}) if diagnostic is not False else []
        if fields:
            # Legacy diagnostics do not delimit values; retaining any free text can retain a value fragment.
            failure = "TOTP verification failed"
            if "No TOTP verification code found" in node or "produced no code" in node:
                failure = "TOTP verification code unavailable"
            return cast(_T, f"{failure} ({', '.join(fields)}): [WITHHELD]")
        return node
    if isinstance(node, dict):
        return cast(
            _T,
            {
                key: (
                    "[WITHHELD]"
                    if key in _TOTP_FIELDS and isinstance(value, (str, int, float)) and value != SECRET_HEADER_MASK
                    else redact_totp_runtime_values(
                        value,
                        diagnostic=key in _DIAGNOSTIC_FIELDS or (diagnostic is True and key in {"message", "detail"}),
                    )
                )
                for key, value in node.items()
            },
        )
    if isinstance(node, list):
        return cast(
            _T,
            [redact_totp_runtime_values(value, diagnostic=diagnostic) for value in node],
        )
    if isinstance(node, tuple):
        return cast(
            _T,
            tuple(redact_totp_runtime_values(value, diagnostic=diagnostic) for value in node),
        )
    return node


def _candidate_secret_segments(text: str) -> list[str]:
    segments: list[str] = []
    for raw_token in (text or "").split():
        token_segments = [raw_token]
        for separator in _COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS:
            token_segments = [part for segment in token_segments for part in segment.split(separator)]
        segments.extend(segment.strip(_COLON_DELIMITED_SECRET_EDGE_CHARS) for segment in token_segments)
    return [segment for segment in segments if segment]


def is_account_row_email(value: str) -> bool:
    if any(char.isspace() for char in value) or "/" in value or ":" in value:
        return False
    try:
        validate_email(value, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return False
    return True


def _looks_like_colon_delimited_secret_value(value: str) -> bool:
    if len(value) < 4:
        return False
    if any(char.isspace() for char in value):
        return False
    if any(char in value for char in ("/", "?", "#")):
        return False
    if value.isdigit() and len(value) <= 5:
        return False
    return True


def _email_password_pair_segments(text: str) -> list[str]:
    pairs: list[str] = []
    for segment in _candidate_secret_segments(text):
        email, separator, secret_value = segment.partition(":")
        if not separator:
            continue
        if is_account_row_email(email) and _looks_like_colon_delimited_secret_value(secret_value):
            pairs.append(segment)
    return pairs


def contains_email_password_pair(text: str) -> bool:
    # Privacy backstop for pasted account dumps. The request-policy classifier
    # owns ambiguous credential semantics; this parser keeps high-confidence raw
    # values out of model prompts and output surfaces without a broad regex rule.
    return bool(_email_password_pair_segments(text))


def raw_secret_spans_for_prompt(text: str) -> tuple[tuple[int, int], ...]:
    """Return deterministic secret spans in the original prompt text."""
    raw = text or ""
    spans = {(match.start(), match.end()) for pattern in RAW_SECRET_PATTERNS for match in pattern.finditer(raw)}
    for segment in _email_password_pair_segments(raw):
        start = 0
        while (index := raw.find(segment, start)) >= 0:
            spans.add((index, index + len(segment)))
            start = index + 1
    return tuple(sorted(spans))


def redact_raw_secrets_for_prompt(text: str) -> str:
    redacted = text or ""
    for pattern in RAW_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    for segment in _email_password_pair_segments(redacted):
        redacted = redacted.replace(segment, "[REDACTED_SECRET]")
    return redacted


# A 32+ hex run carrying a letter is signature-shaped, and a long separator-free run mixing case and
# digits is token-shaped. Both mirror the same shape test taskv3 uses for opaque signing values.
_HEX_BLOB_RE = re.compile(r"[0-9A-Fa-f]{32,}")
# Includes the separators a token alphabet uses (``ghp_...``, base64url), since the word-run
# check below is what keeps ordinary names like "Q3_Report_2026_Final" readable.
_OPAQUE_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_LOWERCASE_RUN_RE = re.compile(r"[a-z]+")
# A CamelCase export name ("CustomerOrdersExport20260911") carries several whole words; a random
# token can hit one long lowercase run by chance, so one is not evidence and two are.
_WORDLIKE_RUN_CHARS = 4
_WORDLIKE_RUNS_REQUIRED = 2


def is_secret_shaped(token: str) -> bool:
    if any(not run.isdigit() for run in _HEX_BLOB_RE.findall(token)):
        return True
    wordlike_runs = sum(1 for run in _LOWERCASE_RUN_RE.findall(token) if len(run) >= _WORDLIKE_RUN_CHARS)
    return (
        _OPAQUE_TOKEN_RE.fullmatch(token) is not None
        and wordlike_runs < _WORDLIKE_RUNS_REQUIRED
        and any(char.isupper() for char in token)
        and any(char.islower() for char in token)
        and any(char.isdigit() for char in token)
    )


# Credential shapes for stored network data. Unlike is_secret_shaped they match whole values only, so an asset name
# such as `<hash>.js` and a mixed-case site key survive.
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_JWT_JSON_PART_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}")
_HEX_CREDENTIAL_RE = re.compile(r"[0-9A-Fa-f]{32,}")
AUTH_SCHEME_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:bearer|token)\s+(?P<token>[A-Za-z0-9._~+/=%-]{8,})"
    r"|basic\s+(?P<basic>[A-Za-z0-9+/]{8,}={0,2})"
    r"|(?P<digest>digest)\s+username\s*=)",
    re.I,
)


_JWT_MIN_PART_LENGTH = len("eyJ") + 8


def _ends_with_jwt_part(part: str) -> bool:
    # The first `eyJ` leaves the longest tail, so one find decides it; a regex search here is quadratic on `eyJeyJ...`.
    start = part.find("eyJ")
    return start >= 0 and len(part) - start >= _JWT_MIN_PART_LENGTH


def _is_jwt_run(run: str) -> bool:
    parts = run.split(".")
    return any(
        _ends_with_jwt_part(parts[index]) and _JWT_JSON_PART_RE.fullmatch(parts[index + 1])
        for index in range(len(parts) - 2)
    )


def iter_jwt_runs(text: str) -> Iterator[re.Match[str]]:
    """Runs of token characters that carry a JWT anywhere, so `bearer.<jwt>`, `jwt_<jwt>` and `3D<jwt>` are one run."""
    return (match for match in _TOKEN_RUN_RE.finditer(text) if _is_jwt_run(match.group()))


def _is_basic_credential(encoded: str) -> bool:
    try:
        return ":" in base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False


def _is_auth_scheme_credential(match: re.Match[str]) -> bool:
    if match.group("digest"):
        return True
    if (token := match.group("token")) is not None:
        return any(char.isalpha() for char in token) and not token.isalpha()
    return _is_basic_credential(match.group("basic"))


_VALUE_START_CHARS = frozenset(":=\"'")
_MAX_GAP_BEFORE_SCHEME = 16


def _starts_a_value(text: str, index: int) -> bool:
    gap_start = index
    while gap_start > 0 and index - gap_start < _MAX_GAP_BEFORE_SCHEME and text[gap_start - 1] in " \t":
        gap_start -= 1
    return gap_start == 0 or text[gap_start - 1] in _VALUE_START_CHARS


def iter_auth_scheme_credentials(text: str) -> Iterator[re.Match[str]]:
    """Scheme words that start the text or follow `:`, `=` or a quote, so prose such as "the token abc-1234" is skipped.
    Matches carry the credential in group `token` or `basic`, or name a Digest response in group `digest`."""
    for match in AUTH_SCHEME_CREDENTIAL_RE.finditer(text):
        if _starts_a_value(text, match.start()) and _is_auth_scheme_credential(match):
            yield match


def _starts_with_auth_scheme_credential(value: str) -> bool:
    match = AUTH_SCHEME_CREDENTIAL_RE.match(value, len(value) - len(value.lstrip()))
    return match is not None and _is_auth_scheme_credential(match)


def is_hex_credential(value: str) -> bool:
    return (
        _HEX_CREDENTIAL_RE.fullmatch(value) is not None
        and any(char.isdigit() for char in value)
        and any(char.isalpha() for char in value)
    )


def has_credential_shape(value: str) -> bool:
    return (
        is_hex_credential(value)
        or next(iter_jwt_runs(value), None) is not None
        or _starts_with_auth_scheme_credential(value)
    )


def redact_secretlike_filename(name: str) -> str:
    """Keep an ordinary display name, but never hand the model a filename whose stem is shaped like a
    credential. The patterns above only catch secrets they can name, and a filename is user-chosen
    text that no semantic screen sees.
    """
    redacted = redact_raw_secrets_for_prompt(name or "")
    stem, dot, extension = redacted.rpartition(".")
    if not dot:
        stem, extension = redacted, ""
    if is_secret_shaped(stem.strip()):
        return f"[REDACTED_SECRET]{dot}{extension}"
    return redacted


def redact_raw_secrets_for_structured_prompt(text: str) -> str:
    # Substituting over serialized JSON lets the assignment pattern consume the closing
    # quote/comma after a value like "Password:", invalidating the whole document
    # (SKY-13986); string values are redacted individually and the document reserialized.
    raw = text or ""
    stripped = raw.strip()
    if not stripped.startswith(("{", "[")):
        return redact_raw_secrets_for_prompt(raw)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return redact_raw_secrets_for_prompt(raw)
    return json.dumps(_redact_string_values(payload), indent=2)


def redact_raw_secrets_in_object(node: object) -> object:
    """Redact every string inside a parsed structure, leaving its shape and non-string values."""
    return _redact_string_values(node)


def _redact_string_values(node: object) -> object:
    if isinstance(node, str):
        return redact_raw_secrets_for_prompt(node)
    if isinstance(node, dict):
        return {key: _redact_string_values(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_redact_string_values(item) for item in node]
    return node
