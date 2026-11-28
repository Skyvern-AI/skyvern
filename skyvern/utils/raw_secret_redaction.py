from __future__ import annotations

import base64
import binascii
import contextlib
import contextvars
import json
import re
import time
import urllib.parse
from array import array
from collections.abc import Callable, Generator, Iterator
from typing import TypeVar, cast

from email_validator import EmailNotValidError, validate_email

from skyvern.utils.secret_headers import SECRET_HEADER_MASK


# The `token` keyword is guarded by negative lookbehinds so pagination cursors
# (next_token, page_token, continuation_token, ...), which are not credentials,
# aren't matched as secrets by either detection or redaction.
def _secret_keyword(cursor_separators: str) -> str:
    cursor_lookbehinds = "".join(
        f"(?<!{prefix}{separator})"
        for prefix in ("next", "prev", "previous", "page", "continuation", "cursor")
        for separator in cursor_separators
    )
    return (
        r"(?:^|(?<=[^A-Za-z0-9]))(?:[A-Za-z0-9]+_){0,8}"
        r"(?:password|passcode|api[_ -]?key|secret|bearer|authorization"
        rf"|{cursor_lookbehinds}token)"
    )


_SECRET_KEYWORD = _secret_keyword("_")
# Shared by the raw-secret patterns below and the artifact scan, so a change to either shape reaches both paths.
_SK_API_KEY = r"sk-[A-Za-z0-9_-]{16,}"
_PRIVATE_KEY_HEADER = r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
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
    re.compile(rf"\b{_SK_API_KEY}\b"),
    re.compile(_PRIVATE_KEY_HEADER),
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
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_.%\-]+")
_MAX_PERCENT_DECODE_ROUNDS = 3
# The whole value is one scheme and one token68 run, so a letters-only Bearer token counts once it is long enough.
_WHOLE_SCHEME_TOKEN_RE = re.compile(r"\s*(bearer|token)\s+([A-Za-z0-9._~+/-]+=*)\s*", re.I)
_MIN_WHOLE_SCHEME_TOKEN_CHARS = 16
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


def percent_decoded(text: str) -> str:
    """Decodes to a fixed point, at most three rounds; `unquote`, not `unquote_plus`, so a base64 `+` survives."""
    for _ in range(_MAX_PERCENT_DECODE_ROUNDS):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _carries_jwt(run: str) -> bool:
    # Raw and decoded both count: a decode can consume the `e` of `eyJ` (`%2eyJ...`) or split a run.
    return _is_jwt_run(run) or _is_jwt_run(percent_decoded(run))


# Only a run holding `eyJ` or a percent escape can carry a JWT, so the scan skips the rest without a Python call each.
_JWT_CANDIDATE_RUN_RE = re.compile(r"(?<![A-Za-z0-9_.%\-])[A-Za-z0-9_.%\-]*?(?:eyJ|%)[A-Za-z0-9_.%\-]*")


def iter_jwt_runs(text: str) -> Iterator[re.Match[str]]:
    """Runs of token characters that carry a JWT anywhere, so `bearer.<jwt>`, `jwt_<jwt>` and `3D<jwt>` are one run.
    A run is judged raw and decoded, so `%2E` separators count, and the raw run is what a caller replaces."""
    return (match for match in _JWT_CANDIDATE_RUN_RE.finditer(text) if _carries_jwt(match.group()))


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


_LINE_OR_VALUE_START_CHARS = _VALUE_START_CHARS | {"\n", "\r"}


def _starts_a_value(text: str, index: int, value_start_chars: frozenset[str] = _VALUE_START_CHARS) -> bool:
    gap_start = index
    while gap_start > 0 and index - gap_start < _MAX_GAP_BEFORE_SCHEME and text[gap_start - 1] in " \t":
        gap_start -= 1
    return gap_start == 0 or text[gap_start - 1] in value_start_chars


def iter_auth_scheme_credentials(text: str, *, line_start_is_value: bool = False) -> Iterator[re.Match[str]]:
    """Scheme words that start the text or follow `:`, `=` or a quote, so prose such as "the token abc-1234" is skipped.
    Matches carry the credential in group `token` or `basic`, or name a Digest response in group `digest`."""
    value_start_chars = _LINE_OR_VALUE_START_CHARS if line_start_is_value else _VALUE_START_CHARS
    for match in AUTH_SCHEME_CREDENTIAL_RE.finditer(text):
        if _starts_a_value(text, match.start(), value_start_chars) and _is_auth_scheme_credential(match):
            yield match


_API_KEY_PREFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:{_SK_API_KEY}|[rs]k_(?:live|test)_[A-Za-z0-9]{{16,}}|gh[pousr]_[A-Za-z0-9]{{36,}}"
    r"|github_pat_[A-Za-z0-9_]{40,}|xox[abposr]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])|AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-]))"
)
_MIN_LOWERCASE_PREFIXED_KEY_CHARS = 32
# Bounded so a header with no END line costs one short scan. A block cut before its END line keeps only base64 lines
# split by real or escaped newlines, so a header named in prose is not a key.
_PRIVATE_KEY_BLOCK_RE = re.compile(
    _PRIVATE_KEY_HEADER + r"(?:(?P<full>(?:[^-]|-(?!----)){0,32768}+-----END [A-Z ]{0,40}PRIVATE KEY-----)"
    r"|(?P<body>(?:\s|\\[nr]){0,8}[A-Za-z0-9+/=]{1,4096}"
    r"(?:[ \t\r]{0,8}(?:\n|\\[nr])(?:\s|\\[nr]){0,8}[A-Za-z0-9+/=]{1,4096}){0,512}))?"
)
_KEY_BODY_SEPARATOR_RE = re.compile(r"\s|\\[nr]")
_MIN_TRUNCATED_KEY_BODY_CHARS = 64


def is_private_key_block(text: str, start: int, end: int) -> bool:
    match = _PRIVATE_KEY_BLOCK_RE.match(text, start)
    return match is not None and match.end() == end


def _has_letter_and_digit(value: str) -> bool:
    return any(char.isdigit() for char in value) and any(char.isalpha() for char in value)


def _is_prefixed_api_key(key: str) -> bool:
    # A hyphenated CSS class such as `sk-cube-grid-item-12` is lowercase and short; issued keys are not both.
    return _has_letter_and_digit(key) and (
        any(char.isupper() for char in key) or len(key) >= _MIN_LOWERCASE_PREFIXED_KEY_CHARS
    )


_TEXT_BUILDER_PIECES = 1024
TEXT_BUILDER_SLICE_CHARS = 1 << 16


class TextBuilder:
    """Joins pieces a chunk at a time, so building a text holds no list entry per escape or per span; a long piece is
    kept as its own chunk rather than copied."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._pieces: list[str] = []

    def add(self, piece: str) -> None:
        if len(piece) >= TEXT_BUILDER_SLICE_CHARS:
            self._flush()
            self._chunks.append(piece)
            return
        self._pieces.append(piece)
        if len(self._pieces) >= _TEXT_BUILDER_PIECES:
            self._flush()

    def add_slice(self, text: str, start: int, end: int) -> None:
        for chunk_start in range(start, end, TEXT_BUILDER_SLICE_CHARS):
            self.add(text[chunk_start : min(end, chunk_start + TEXT_BUILDER_SLICE_CHARS)])

    def build(self) -> str:
        self._flush()
        if len(self._chunks) == 1:
            return self._chunks.pop()
        text = "".join(self._chunks)
        self._chunks.clear()
        return text

    def _flush(self) -> None:
        if self._pieces:
            self._chunks.append("".join(self._pieces))
            self._pieces.clear()


# A `\n`, `\r` or `\t` escape (not an escaped backslash before the letter) becomes whitespace of the same length, so
# a credential right after it starts a word and spans keep the offsets of the original text.
_WHITESPACE_ESCAPE_RE = re.compile(r"(?<!\\)(?:\\\\)*+\\([nrt])")


def artifact_scan_text(text: str) -> str:
    if "\\" not in text:
        return text
    builder = TextBuilder()
    position = 0
    for match in _WHITESPACE_ESCAPE_RE.finditer(text):
        builder.add_slice(text, position, match.start(1) - 1)
        builder.add(" \t" if match.group(1) == "t" else " \n")
        position = match.end()
    if not position:
        return text
    builder.add_slice(text, position, len(text))
    return builder.build()


# A twin of a credential name (`new_password`, `password_confirmation`, `password2`) keeps the name as its exact residue
# once these modifier words and trailing digits come off. The HAR name scrub reads the same lists.
TWIN_PREFIXES = ("new", "old", "current", "confirm", "repeat", "retype", "verify", "user", "account", "login", "re")
TWIN_SUFFIXES = ("confirmation", "confirmed", "confirm", "again", "repeat", "verify", "verification")
_PASSWORD_WORDS = frozenset({"password", "passwd", "pwd", "passcode", "passphrase"})
# `session_id` is not a credential key: Skyvern's own logs carry browser and chat session ids under it.
_OPAQUE_LAST_WORDS = frozenset({"token", "secret", "jwt", "apikey", "sessid", "sessionid", "phpsessid", "jsessionid"})
_OPAQUE_LAST_PAIRS = frozenset({("api", "key"), ("private", "key"), ("secret", "key"), ("access", "key")})
_WHOLE_NAME_KEYS = {
    ("authorization",): "opaque",
    ("proxy", "authorization"): "opaque",
    ("pass",): "password",
}
# A pagination cursor, not a credential: `next_token`, `nextPageToken`.
_CURSOR_WORDS = frozenset({"next", "prev", "previous", "page", "continuation", "cursor"})
# A password name about the form rather than the value: `forgot_password`, `showPassword`.
_PASSWORD_UI_WORDS = frozenset(
    {"forgot", "reset", "show", "hide", "toggle", "change", "label", "title", "placeholder", "prompt", "error"}
    | {"msg", "message", "text", "link", "button", "btn", "field", "input", "type", "hint", "help", "policy"}
)
_KEY_NAME_CANDIDATE_RE = re.compile(
    r"[Pp](?i:assword|asswd|asscode|assphrase|wd|ass|hpsessid)|[Tt](?i:oken)|[Ss](?i:ecret|ession[_-]?id|essid)"
    r"|[Jj](?i:wt|sessionid)|[Cc](?i:ookie)|[Kk](?i:ey)|[Ii](?i:d)|[Aa](?i:pikey|uthorization)"
)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_NAME_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NAME_WORD_SPLIT_RE = re.compile(r"[_\-]+")
_KEY_SEPARATOR_RE = re.compile(r"(?P<close>\\{0,3}[\"'])?[ \t]*(?P<sep>=>|=&gt;|%3[Dd]|[:=])[ \t]*")
# Characters that may stand right before an unquoted key: the start of a line, a string, an element's text, an
# object, a list, a query or a PHP variable.
_KEY_OPENERS = frozenset("\n\r\"'>{,([?&;$")
# Before these a key is a dotted config key, a URL fragment or a Ruby symbol only with `=` or `=>`;
# `.password:invalid` is CSS.
_ASSIGNMENT_ONLY_OPENERS = frozenset(".#:")
_SCHEME_WORD = r"(?:(?i:bearer|basic|token)[ \t]+)?"
_UNESCAPED_DOUBLE_QUOTE_RE = re.compile(r'(?<!\\)"')
_UNESCAPED_SINGLE_QUOTE_RE = re.compile(r"(?<!\\)'")
# The one line break every line finder reads; a lone `\r` ends a line as `\n` does.
_LINE_BREAK = r"(?:\r\n|\r|\n)"
_LINE_BREAK_RE = re.compile(_LINE_BREAK)
_INDENT_RE = re.compile(r"[ \t]*")
# A block in a page ends at the close of an element opened on the key's line or the line above
# (`<pre>password: |\n  x</pre><p>...`); any other `</` is part of the value.
_OPENING_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9]*)")
_CLOSING_TAG_RE = re.compile(r"</([A-Za-z][A-Za-z0-9]*)")
# YAML block scalar indicator (`password: |`); the value is every following line indented past the key's line. The
# header ends at the line's end or the text's, so a header is read once and never scanned again from a later key.
_BLOCK_SCALAR_HEADER_RE = re.compile(r"[|>][-+0-9]*[ \t]*(?:#[^\r\n]*)?(?:" + _LINE_BREAK + r"|$)")
# A quoted value runs to its matching closing quote, after an optional Python string prefix. Each backslash is read
# one way only, so a run of them cannot backtrack; an escaped copy of the delimiter stays inside the value.
_QUOTE_OPEN = r"(?:[bBrRuUfF]{1,2})?(?P<quote>\\{0,3}(?:\"\"\"|'''|[\"'`]))"
_QUOTED_SPACED_VALUE_RE = re.compile(
    _QUOTE_OPEN + r"(?P<value>(?:(?!(?P=quote))(?:\\[\\\"'`]|\\(?![\\\"'`])|[^\r\n\\])){6,512}?)"
    r"(?P=quote)"
)
_QUOTED_OPAQUE_VALUE_RE = re.compile(
    _QUOTE_OPEN + _SCHEME_WORD + r"(?P<value>(?:(?!(?P=quote))(?:\\[\\\"'`]|\\(?![\\\"'`])|[^\s\\])){6,512}?)(?P=quote)"
)
# A longer run is not read as a value, as quoted values are capped.
_MAX_BARE_VALUE_CHARS = 512
_BARE_VALUE_RE = re.compile(_SCHEME_WORD + r"(?P<value>[^\s\"'`<>&,;(){}\[\]\\]{6,})")
# A password typed into a `.env` line, YAML or a command line may hold the symbols a bare value stops at
# (`DB_PASSWORD=ab&cd12`); after `=` or at a line's start such a value runs on to whitespace, a quote, `<` or `\`.
_SYMBOL_PASSWORD_VALUE_RE = re.compile(r"[^\s\"'`<>&,;(){}\[\]\\$][^\s\"'`<\\]{0,%d}" % _MAX_BARE_VALUE_CHARS)
_SYMBOL_PASSWORD_TRAILER = ",;"
# A call or an index, with an optional member chain after it, is code: `password = getPassword();`, `values[0],`.
_CODE_CALL_OR_INDEX_RE = re.compile(r"[A-Za-z_$][\w$.]*(?:[(\[][^\s]*[)\]](?:\.[\w$]+(?:\(\))?)*|[(\[])")
# A member chain or a call is code, not a value: `password = document.getElementById(...)`, `{password: e.value}`.
_CODE_REFERENCE_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+")
# Type annotations and form labels name a kind of value, not a value: `password: string`, `Password: Required`.
_NON_VALUE_WORDS = frozenset(
    {"string", "number", "boolean", "bool", "str", "int", "integer", "secretstr", "optional", "required", "null"}
    | {"undefined", "none", "object", "any", "true", "false", "hidden", "text", "digest"}
)
_MASK_CHARS = frozenset("*•●·")


class ScanBudgetExceeded(RuntimeError):
    pass


# CPU seconds of the scanning thread, so another thread holding the GIL does not use up the budget. A miss withholds
# the artifact for good, so the slope is over 4x the slowest real input measured: nested LLM requests at 1.3 s per MB.
SCAN_BUDGET_BASE_SECONDS = 0.5
SCAN_BUDGET_SECONDS_PER_MB = 6.0
_SCAN_BUDGET_CHECK_EVERY = 256
_SCAN_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("_SCAN_DEADLINE", default=None)


@contextlib.contextmanager
def scan_budget(size: int) -> Iterator[None]:
    """Inside this, a credential scan of ``size`` characters that runs past its budget raises ScanBudgetExceeded."""
    seconds = SCAN_BUDGET_BASE_SECONDS + SCAN_BUDGET_SECONDS_PER_MB * size / (1024 * 1024)
    token = _SCAN_DEADLINE.set(time.thread_time() + seconds)
    try:
        yield
    finally:
        _SCAN_DEADLINE.reset(token)


def check_scan_budget() -> None:
    deadline = _SCAN_DEADLINE.get()
    if deadline is not None and time.thread_time() > deadline:
        raise ScanBudgetExceeded


def _key_words(name: str) -> list[str]:
    return [word for word in _NAME_WORD_SPLIT_RE.split(_NAME_WORD_BOUNDARY_RE.sub("_", name).lower()) if word]


def _key_kind(name: str) -> str | None:
    """`password` or `cookie` for a key whose quoted value may hold spaces, `opaque` for another credential key, None
    for a name that is not a credential's."""
    words = _key_words(name)
    end = len(words)
    while end:
        last = words[end - 1].rstrip("0123456789")
        if last and (last not in TWIN_SUFFIXES or end == 1):
            break
        end -= 1
    if not end:
        return None
    words = [*words[: end - 1], last]
    if (whole := _WHOLE_NAME_KEYS.get(tuple(words))) is not None:
        return whole
    last = words[-1]
    if last in _PASSWORD_WORDS:
        return None if _PASSWORD_UI_WORDS.intersection(words[:-1]) else "password"
    if last == "cookie":
        return "cookie"
    if last == "token" and len(words) > 1 and words[-2] in _CURSOR_WORDS:
        return None
    if last in _OPAQUE_LAST_WORDS or tuple(words[-2:]) in _OPAQUE_LAST_PAIRS:
        return "opaque"
    return None


def _is_key_position(text: str, start: int, separator: str) -> bool:
    # An unquoted key starts a line, an object member, a query parameter or an assignment; `Forgot password: x` in a
    # sentence is prose.
    if start == 0 or text[start - 1] in _KEY_OPENERS:
        return True
    previous = text[start - 1]
    if previous in _ASSIGNMENT_ONLY_OPENERS:
        return separator != ":"
    if previous not in " \t":
        return False
    gap_start = start - 1
    while gap_start > 0 and text[gap_start - 1] in " \t":
        gap_start -= 1
    return gap_start == 0 or text[gap_start - 1] in _KEY_OPENERS or separator != ":"


def _is_bare_value(value: str, text: str, end: int, key_name: str, kind: str) -> bool:
    # A dotted value with a digit is a token (`hvs.abc123`), not a member chain.
    return not (
        text.startswith("(", end)
        or (kind == "cookie" and "=" not in value)
        or (_CODE_REFERENCE_RE.fullmatch(value) and not any(char.isdigit() for char in value))
        or value.lower() in _NON_VALUE_WORDS
        or (_key_kind(value) is not None and not any(char.isdigit() for char in value))
        or _key_words(value) == _key_words(key_name)
        or set(value) <= _MASK_CHARS
    )


def _indent(text: str, line_start: int) -> int:
    match = _INDENT_RE.match(text, line_start)
    return match.end() - line_start if match else 0


def _never_escaped(_: int) -> bool:
    return False


# What may follow the `"` that closes a string literal: optional whitespace, then a delimiter or the end of the text.
_STRING_END_FOLLOWS_RE = re.compile(r"\s*+(?:[,;)}\]:]|\Z)")
_LINE_START_WINDOW = 256


def _line_start(text: str, index: int) -> int:
    # Searched back in doubling windows, so the cost follows the line's length and not the distance to a `\r` or `\n`
    # that the text may never hold.
    end, width = index, _LINE_START_WINDOW
    while end > 0:
        window_start = max(0, end - width)
        found = max(text.rfind("\n", window_start, end), text.rfind("\r", window_start, end))
        # A `\r` whose `\n` is at ``index`` is the first half of one CRLF break.
        if found >= 0 and text[found] == "\r" and text.startswith("\n", found + 1):
            found = max(text.rfind("\n", window_start, found), text.rfind("\r", window_start, found))
        if found >= 0:
            return found + 1
        end, width = window_start, width * 2
    return 0


class _TextQuotes:
    """The next unescaped double quote of one text, cached across block headers, so no per-quote index is kept."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._double_from: int | None = None
        self._double_at: int | None = None

    def next_double_quote(self, index: int) -> int | None:
        # The last search's answer holds for any later start up to the quote it found.
        cached = self._double_from is not None and self._double_from <= index
        if not cached or (self._double_at is not None and self._double_at < index):
            match = _UNESCAPED_DOUBLE_QUOTE_RE.search(self.text, index)
            self._double_from, self._double_at = index, None if match is None else match.start()
        return self._double_at


def _closing_tag_end(text: str, line_start: int, line_end: int, opened: set[str]) -> int | None:
    for tag in _CLOSING_TAG_RE.finditer(text, line_start, line_end):
        if tag.group(1).lower() in opened:
            return tag.start()
    return None


def _block_scalar_span(
    quotes: _TextQuotes, key_start: int, header: re.Match[str], escaped: Callable[[int], bool]
) -> tuple[int, int] | None:
    text = quotes.text
    key_line_start = _line_start(text, key_start)
    key_indent = _indent(text, key_line_start)
    limit = len(text)
    # A line break written as an escape sits inside a string literal (YAML carried in a JSON value). In a string opened
    # with `"` every `"` of the content is escaped, so the block ends at the next unescaped one when a delimiter follows
    # it; otherwise the block ends by indent.
    header_break = header.end() - 1
    if (
        header.end() > header.start()
        and text[header_break] in "\r\n"
        and escaped(header_break)
        and (next_quote := quotes.next_double_quote(header.end())) is not None
        and _STRING_END_FOLLOWS_RE.match(text, next_quote + 1)
    ):
        limit = next_quote
    opened: set[str] | None = None
    span_start = span_end = -1
    line_start = header.end()
    while line_start < limit:
        line_break = _LINE_BREAK_RE.search(text, line_start, limit)
        line_end = limit if line_break is None else line_break.start()
        tag_start = None
        if text.find("</", line_start, line_end) >= 0:
            if opened is None:
                window_start = _line_start(text, key_line_start - 1) if key_line_start else 0
                opened = {name.lower() for name in _OPENING_TAG_RE.findall(text, window_start, key_start)}
            tag_start = _closing_tag_end(text, line_start, line_end, opened)
        line = text[line_start : line_end if tag_start is None else tag_start]
        if line.strip():
            indent = _indent(text, line_start)
            if indent <= key_indent:
                break
            if span_start < 0:
                span_start = line_start + indent
            span_end = line_start + len(line.rstrip())
        if tag_start is not None or line_break is None:
            break
        line_start = line_break.end()
    return None if span_start < 0 else (span_start, span_end)


def _iter_keyword_value_spans(text: str, escaped: Callable[[int], bool]) -> Iterator[tuple[int, int]]:
    scanned = 0
    quotes: _TextQuotes | None = None
    for index, candidate in enumerate(_KEY_NAME_CANDIDATE_RE.finditer(text)):
        if index % _SCAN_BUDGET_CHECK_EVERY == 0:
            check_scan_budget()
        if candidate.start() < scanned:
            continue
        start = candidate.start()
        while start > 0 and text[start - 1] in _NAME_CHARS:
            start -= 1
        end = candidate.end()
        while end < len(text) and text[end] in _NAME_CHARS:
            end += 1
        scanned = end
        separator = _KEY_SEPARATOR_RE.match(text, end)
        if separator is None:
            continue
        name = text[start:end]
        kind = _key_kind(name)
        # Page code writing `document.cookie = "theme=dark"` sets a cookie; it is not a stored request header.
        if kind is None or (kind == "cookie" and start > 0 and text[start - 1] == "."):
            continue
        close = separator.group("close")
        if close is not None:
            if text[max(0, start - len(close)) : start] != close:
                continue
        elif not _is_key_position(text, start, separator.group("sep")):
            continue
        value_start = separator.end()
        if (header := _BLOCK_SCALAR_HEADER_RE.match(text, value_start)) is not None:
            check_scan_budget()
            if quotes is None:
                quotes = _TextQuotes(text)
            block = _block_scalar_span(quotes, start, header, escaped)
            # Past the header whether a block follows or not: its comment runs to the line's end.
            scanned = header.end() if block is None else block[1]
            if block is not None:
                yield block
            continue
        if text.startswith("[", value_start):
            scanned = yield from _array_element_spans(text, value_start, name, kind)
            continue
        symbols_allowed = kind == "password" and (
            (separator.group("sep") == "=" and (start == 0 or text[start - 1] not in "?&")) or _starts_line(text, start)
        )
        value_end, span = _scalar_value_span(text, value_start, name, kind, symbols_allowed)
        if value_end is not None:
            # Past the value whether it is read or not, so a key inside a rejected value does not read it again.
            scanned = value_end
        if span is not None:
            yield span


def _scalar_value_span(
    text: str, start: int, name: str, kind: str, symbols_allowed: bool = False
) -> tuple[int | None, tuple[int, int] | None]:
    """The end of the value at ``start`` (None when no value starts there) and its span when it is a credential."""
    quoted = (_QUOTED_OPAQUE_VALUE_RE if kind == "opaque" else _QUOTED_SPACED_VALUE_RE).match(text, start)
    # As in the header rule, a value under an Authorization key without a scheme word holds a letter and a digit, so
    # `Authorization: Approved on 11/03/2024` is prose.
    prose_checked = _key_words(name)[-1] == "authorization"
    # A cookie header's value holds `name=value` pairs; spaced text without one is prose.
    if quoted is not None and (kind != "cookie" or "=" in quoted.group("value") or " " not in quoted.group("value")):
        if prose_checked and not _has_letter_and_digit(quoted.group("value")):
            return quoted.end(), None
        return quoted.end(), quoted.span("value")
    bare = _BARE_VALUE_RE.match(text, start)
    if symbols_allowed and (symbol_span := _symbol_password_span(text, start, bare)) is not None:
        return symbol_span[1], symbol_span
    if bare is None:
        return None, None
    value = bare.group("value")
    if prose_checked and not _has_letter_and_digit(value):
        return bare.end(), None
    if len(value) <= _MAX_BARE_VALUE_CHARS and _is_bare_value(value, text, bare.end(), name, kind):
        return bare.end(), bare.span("value")
    return bare.end(), None


def _starts_line(text: str, index: int) -> bool:
    while index > 0 and text[index - 1] in " \t":
        index -= 1
    return index == 0 or text[index - 1] in "\r\n"


def _symbol_password_span(text: str, start: int, bare: re.Match[str] | None) -> tuple[int, int] | None:
    match = _SYMBOL_PASSWORD_VALUE_RE.match(text, start)
    if match is None or (bare is not None and match.end() <= bare.end()):
        return None
    value = match.group().rstrip(_SYMBOL_PASSWORD_TRAILER)
    if len(match.group()) > _MAX_BARE_VALUE_CHARS or len(value) < 6 or not _has_letter_and_digit(value):
        return None
    if _CODE_CALL_OR_INDEX_RE.fullmatch(value):
        return None
    return start, start + len(value)


# An element the value rules do not read (a short string, a number, `null`) is skipped. Each element must be followed
# by `,` or `]`, so a list inside a quoted string ends at that string's closing quote instead of reading the next one.
_ARRAY_SKIPPED_ELEMENT_RE = re.compile(r"(?P<q>\\{0,3}[\"'])[^\"'\\\r\n]{0,5}(?P=q)|[A-Za-z0-9.+-]{1,5}")
_ARRAY_ELEMENT_END_RE = re.compile(r"\s*(?P<end>[,\]])")
_MAX_ARRAY_CHARS = 4096


def _array_element_spans(text: str, open_index: int, name: str, kind: str) -> Generator[tuple[int, int], None, int]:
    """Every element of a list assigned to a credential key is read as that key's value; returns where the list ends."""
    position = open_index + 1
    limit = min(len(text), open_index + _MAX_ARRAY_CHARS)
    while position < limit:
        while position < limit and text[position].isspace():
            position += 1
        if text.startswith("]", position):
            return position + 1
        value_end, span = _scalar_value_span(text, position, name, kind)
        if span is not None:
            yield span
        if value_end is None:
            skipped = _ARRAY_SKIPPED_ELEMENT_RE.match(text, position)
            if skipped is None:
                return position
            value_end = skipped.end()
        element_end = _ARRAY_ELEMENT_END_RE.match(text, value_end)
        if element_end is None:
            return value_end
        if element_end.group("end") == "]":
            return element_end.end()
        position = element_end.end()
    return position


# A password in a URL's userinfo (`https://user:pw@host`, or `redis://:pw@host` with no username).
_URL_USERINFO_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@\"'<>]{0,256}:(?P<secret>[^\s/@\"'<>]{1,512})@"
)
# Header names anywhere after a word boundary, so a log prefix (`INFO login Authorization: Bearer ...`), a header dict
# and a repr all count. An Authorization value is read after a scheme word, so its token may be letters only; a Cookie
# value must start with a `name=value` pair, so a banner reading "Cookie: accept all" is kept.
_AUTHORIZATION_PREFIX = (
    r"(?<![A-Za-z0-9_-])(?:[Pp](?i:roxy)-)?[Aa](?i:uthorization)[\"']?[ \t]*(?:=>|[:=])[ \t]*[\"'`]?"
)
# An RFC 9110 scheme token of 2 or more characters that starts with a letter (`Bearer`, `SSWS`, `A+B`), so prose such as
# "Work Authorization: Y H1B-2025" is kept. Under a scheme other than Bearer, Token or Basic the value must hold a
# letter and a digit, so "Authorization: Please authenticate" is kept too.
_AUTHORIZATION_SCHEME = r"(?P<scheme>[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]{1,63})[ \t]+"
_AUTHORIZATION_HEADER_RE = re.compile(
    _AUTHORIZATION_PREFIX + r"(?!(?i:digest)\b)" + _AUTHORIZATION_SCHEME + r"(?P<secret>[A-Za-z0-9._~+/=-]{8,})"
)
_KNOWN_AUTHORIZATION_SCHEMES = frozenset({"bearer", "token", "basic"})
# An auth-param list (`name="quoted"` or `name=token`, comma separated; Digest, OAuth 1.0, AWS SigV4) is scrubbed through
# its last parameter, as a Digest `response=` hash proves the password and an OAuth or SigV4 signature signs requests.
_AUTH_PARAM = r"[A-Za-z0-9_-]+=(?:\\{0,3}\"[^\"\r\n]*?\\{0,3}\"|[^\s,\"'<>\\]+)"
_AUTH_PARAM_AUTHORIZATION_RE = re.compile(
    _AUTHORIZATION_PREFIX
    + _AUTHORIZATION_SCHEME
    + r"(?P<secret>"
    + _AUTH_PARAM
    + r"(?:[ \t]*,[ \t]*"
    + _AUTH_PARAM
    + r")*+)"
)
# The value starts with a cookie name and `=`. A quoted value of cookie characters (RFC 6265 cookie-octet) closes with
# the escape it opened with, so it cannot run past the string that holds it; any other quoted value (a space, `,`, `;`
# or an escape such as `\073` inside) runs to its closing quote, or to the line's end when it has none; a `\n` or `\r`
# escape ends the line too.
_COOKIE_OCTETS = r"[!#-+\--:<-\[\]-~]+"
_LOOSE_QUOTED_COOKIE_VALUE = r'="(?:[^"\\\r\n]|\\[^\r\nnr]){1,512}(?:"|(?=[\r\n]|\\[nr])|$)'
# An unquoted value may hold a backslash escape other than a quote's or a line break's (`\/`, `\x2f`).
_UNQUOTED_COOKIE_ESCAPE = r"\\[^\\\"'\r\nnr]"


def _quoted_cookie_value(group: str) -> str:
    # A cookie value ends the pair: `;`, `,`, a space, the end, or the quote or tag around the header.
    strict = rf"=(?P<{group}>\\{{0,3}})\"" + _COOKIE_OCTETS + rf"(?P={group})\"(?=[;,\s\\\"'<]|$)"
    return f"{strict}|{_LOOSE_QUOTED_COOKIE_VALUE}|{_UNQUOTED_COOKIE_ESCAPE}"


# Pairs are read possessively, as a repeated group otherwise holds regex backtracking state for every character it
# reads; a run of spaces or tabs counts only before another pair character, so the value never ends in one.
_COOKIE_HEADER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[Ss](?i:et)-)?[Cc](?i:ookie)[\"']?[ \t]*:[ \t]*[\"']?"
    r"(?P<secret>[!#$%&*+.^_`|~0-9A-Za-z-]+(?==)(?:"
    + _quoted_cookie_value("quote")
    + r"|[^\s\"'<>\\]|[^\S\r\n]+(?=[^\s\"'<>\\]|"
    + _UNQUOTED_COOKIE_ESCAPE
    + r"))++)"
)
# A password input's value attribute. A quoted attribute value may hold `>`; a quote that does not close inside the
# window is read as a plain character. Each tag is matched inside a window of its start, so the scan stays linear.
_INPUT_TAG_START_RE = re.compile(r"<input\b", re.IGNORECASE)
_INPUT_TAG_RE = re.compile(r"<input\b(?:=\s*+(?:\"[^\"]*+\"|'[^']*+')|[^<>])*+>", re.IGNORECASE)
_INPUT_TAG_MAX_CHARS = len("<input>") + 2048
_PASSWORD_INPUT_ATTR_RE = re.compile(
    r"\b(?:type|name|id|autocomplete)\s*=\s*[\"']?(?P<attr>[\w-]*(?:password|passwd|pwd)[\w-]*)", re.IGNORECASE
)
# Words that name the input itself (`passwordField`), so they do not make a password input's name about the form.
_INPUT_NAMING_WORDS = frozenset({"field", "input", "text", "type"})
_INPUT_SECRET_LAST_WORDS = frozenset({"token", "secret", "key"})


def _is_password_input(tag: str) -> bool:
    # The keyword path's password-UI words apply: `password_hint` and `show-password` name inputs about the form. A name
    # ending in a secret word holds one whatever its other words (`reset_password_token`).
    for attr in _PASSWORD_INPUT_ATTR_RE.finditer(tag):
        words = _key_words(attr.group("attr"))
        if words[-1] in _INPUT_SECRET_LAST_WORDS or not (_PASSWORD_UI_WORDS - _INPUT_NAMING_WORDS).intersection(words):
            return True
    return False


# Attributes read in order, each with its quoted value, so `value=` inside another attribute's value is not the value.
_INPUT_ATTR_RE = re.compile(
    r"[\s/](?P<name>[^\s\"'<>/=]+)(?:\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s\"'>]+)))?"
)


def _password_input_value_span(tag: str) -> tuple[int, int] | None:
    for attr in _INPUT_ATTR_RE.finditer(tag, len("<input")):
        if attr.group("name").lower() == "value":
            group = next((name for name in ("dq", "sq", "bare") if attr.group(name)), None)
            return None if group is None else attr.span(group)
    return None


def _iter_structural_credential_spans(text: str) -> Iterator[tuple[int, int]]:
    for match in _AUTHORIZATION_HEADER_RE.finditer(text):
        if match.group("scheme").lower() in _KNOWN_AUTHORIZATION_SCHEMES or _has_letter_and_digit(
            match.group("secret")
        ):
            yield match.span("secret")
    for match in _AUTH_PARAM_AUTHORIZATION_RE.finditer(text):
        if match.group("scheme").lower() == "digest" or _has_letter_and_digit(match.group("secret")):
            yield match.span("secret")
    for pattern in (_URL_USERINFO_RE, _COOKIE_HEADER_RE):
        for match in pattern.finditer(text):
            yield match.span("secret")
    # Every `<input` is read on its own, so a quote that never closes cannot hide a later input.
    for start in _INPUT_TAG_START_RE.finditer(text):
        tag = _INPUT_TAG_RE.match(text, start.start(), start.start() + _INPUT_TAG_MAX_CHARS)
        if tag and _is_password_input(tag.group()) and (value := _password_input_value_span(tag.group())):
            yield tag.start() + value[0], tag.start() + value[1]


def _iter_unescaped_credential_spans(text: str, escaped: Callable[[int], bool]) -> Iterator[tuple[int, int]]:
    yield from _iter_keyword_value_spans(text, escaped)
    check_scan_budget()
    for match in _API_KEY_PREFIX_RE.finditer(text):
        if _is_prefixed_api_key(match.group()):
            yield match.span()
    for match in _PRIVATE_KEY_BLOCK_RE.finditer(text):
        body = match.group("body")
        if match.group("full") is not None or (
            body is not None and len(_KEY_BODY_SEPARATOR_RE.sub("", body)) >= _MIN_TRUNCATED_KEY_BODY_CHARS
        ):
            yield match.span()
    yield from _iter_structural_credential_spans(text)


def _iter_layer_spans(text: str, escaped: Callable[[int], bool] = _never_escaped) -> Iterator[tuple[int, int]]:
    # Read raw, so a quoted value holding a `\n` escape stays one value, and with escapes as whitespace, so a credential
    # right after `\n` starts a word. ``escaped`` tells which characters of ``text`` were escapes in the layer above.
    yield from _iter_unescaped_credential_spans(text, escaped)
    scan_text = artifact_scan_text(text)
    if scan_text != text:
        # A span never starts or ends on the whitespace standing in for an escape, so the escape is kept whole.
        for start, end in _iter_unescaped_credential_spans(
            scan_text, lambda index: scan_text[index] != text[index] or escaped(index)
        ):
            while start < end and scan_text[start] != text[start]:
                start += 1
            while end > start and scan_text[end - 1] != text[end - 1]:
                end -= 1
            if start < end:
                yield start, end
        check_scan_budget()


_HTML_QUOTE_ENTITIES = ("quot", "apos", "amp", "lt", "gt", "#34", "#39", "#x22", "#x27")
# A run of escapes of one kind is one match and is decoded in one call, so a page of backslashes costs no Python work
# per escape. The run is capped so its decoded copy stays a small piece; the next match carries on where it stopped.
_ESCAPE_RUN_RE = re.compile(
    r"(?:\\[\\\"'`/bfnrt]){1,4096}+|(?:\\u[0-9A-Fa-f]{4}){1,4096}+|"
    + "|".join(f"(?:&{entity};){{1,4096}}+" for entity in _HTML_QUOTE_ENTITIES)
)
_SIMPLE_ESCAPE_TABLE = str.maketrans({"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"})
_HTML_ENTITIES = {
    "&quot;": '"',
    "&apos;": "'",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&#34;": '"',
    "&#39;": "'",
    "&#x22;": '"',
    "&#x27;": "'",
}
_SINGLE_ESCAPES = {
    **{entity: (char, len(entity)) for entity, char in _HTML_ENTITIES.items()},
    **{f"\\{char}": (char.translate(_SIMPLE_ESCAPE_TABLE), 2) for char in "\\\"'`/bfnrt"},
}
_ESCAPE_LAYER_MARKERS = ('\\"', "\\'", "\\/", "\\u", "quot;", "apos;", "#34;", "#39;", "#x22;", "#x27;")
# JSON text inside a string inside a string, or JSON in an HTML attribute, is decoded one layer at a time and each layer
# is scanned again; this many layers below the stored text are read.
MAX_ESCAPE_DECODE_LAYERS = 5


def _decoded_escape_run(run: str) -> tuple[str, int]:
    """The decoded run and the length of each of its escapes."""
    single = _SINGLE_ESCAPES.get(run)
    if single is not None:
        return single
    if run[0] == "&":
        escape_length = run.index(";") + 1
        return _HTML_ENTITIES[run[:escape_length]] * (len(run) // escape_length), escape_length
    if run[1] == "u":
        # One character per escape, as a surrogate pair is not joined.
        return run.encode("ascii").decode("unicode_escape"), 6
    return run[1::2].translate(_SIMPLE_ESCAPE_TABLE), 2


def _set_bits(bits: bytearray, start: int, end: int) -> None:
    first, last = start >> 3, (end - 1) >> 3
    if first == last:
        bits[first] |= ((1 << (end - start)) - 1) << (start & 7)
        return
    bits[first] |= (0xFF << (start & 7)) & 0xFF
    bits[first + 1 : last] = b"\xff" * (last - first - 1)
    bits[last] |= (1 << (((end - 1) & 7) + 1)) - 1


# Offsets of a decoded layer are indexed per block of this many characters; a lookup counts escapes within one block.
_ESCAPE_INDEX_BLOCK_CHARS = 512


class _EscapeIndex:
    """Which characters of a decoded layer came from an escape, and the offset each maps back to, in about a bit per
    decoded character and a byte per escape longer than two characters."""

    def __init__(self) -> None:
        self.marks = bytearray()
        self.long_marks = bytearray()
        self.long_shifts = bytearray()
        self.block_shifts = array("q")
        self.block_long_counts = array("q")
        self._shift = 0

    def add_run(self, decoded_index: int, count: int, escape_length: int) -> None:
        """``count`` escapes of ``escape_length`` characters, decoded to one character each from ``decoded_index``."""
        if len(self.block_shifts) * _ESCAPE_INDEX_BLOCK_CHARS <= decoded_index:
            self._close_blocks_to(decoded_index)
        end = decoded_index + count
        if (end + 7) >> 3 > len(self.marks):
            grow = bytes(max(((end + 7) >> 3) - len(self.marks), 64))
            self.marks += grow
            self.long_marks += grow
        if count == 1 and escape_length == 2:
            self.marks[decoded_index >> 3] |= 1 << (decoded_index & 7)
            self._shift += 1
            return
        _set_bits(self.marks, decoded_index, end)
        step, long_count = escape_length - 1, len(self.long_shifts)
        if escape_length > 2:
            _set_bits(self.long_marks, decoded_index, end)
            self.long_shifts += bytes((step,)) * count
        # A block that starts inside the run counts the run's escapes before it.
        while len(self.block_shifts) * _ESCAPE_INDEX_BLOCK_CHARS < end:
            before = len(self.block_shifts) * _ESCAPE_INDEX_BLOCK_CHARS - decoded_index
            self.block_shifts.append(self._shift + before * step)
            self.block_long_counts.append(long_count + (before if escape_length > 2 else 0))
        self._shift += count * step

    def finish(self, decoded_length: int) -> None:
        self._close_blocks_to(decoded_length)

    def _close_blocks_to(self, decoded_index: int) -> None:
        while len(self.block_shifts) <= decoded_index // _ESCAPE_INDEX_BLOCK_CHARS:
            self.block_shifts.append(self._shift)
            self.block_long_counts.append(len(self.long_shifts))

    def escaped(self, index: int) -> bool:
        return 0 <= index >> 3 < len(self.marks) and bool(self.marks[index >> 3] >> (index & 7) & 1)

    def encoded_offset(self, offset: int) -> int:
        block = offset // _ESCAPE_INDEX_BLOCK_CHARS
        block_start = block * _ESCAPE_INDEX_BLOCK_CHARS
        escapes = _count_bits(self.marks, block_start, offset)
        long_escapes = _count_bits(self.long_marks, block_start, offset)
        long_start = self.block_long_counts[block]
        long_shift = sum(self.long_shifts[long_start : long_start + long_escapes])
        return offset + self.block_shifts[block] + escapes - long_escapes + long_shift


def _count_bits(bits: bytearray, start: int, end: int) -> int:
    # ``start`` is a multiple of 8.
    count = int.from_bytes(bits[start >> 3 : end >> 3], "little").bit_count()
    if end & 7 and end >> 3 < len(bits):
        count += (bits[end >> 3] & ((1 << (end & 7)) - 1)).bit_count()
    return count


def _decode_escape_layer(holder: list[str]) -> tuple[str, _EscapeIndex] | None:
    """One layer of string escapes and quote entities decoded, with an index mapping each decoded offset back to the
    text it came from. The text is taken out of ``holder``, so it is freed before the decoded copy is joined."""
    text = holder.pop()
    builder = TextBuilder()
    index = _EscapeIndex()
    position = decoded_length = 0
    add_run = index.add_run
    for match in _ESCAPE_RUN_RE.finditer(text):
        start, end = match.span()
        decoded, escape_length = _decoded_escape_run(match.group())
        if start == position:
            builder.add(decoded)
        elif start - position < TEXT_BUILDER_SLICE_CHARS:
            builder.add(text[position:start] + decoded)
        else:
            builder.add_slice(text, position, start)
            builder.add(decoded)
        decoded_length += start - position
        add_run(decoded_length, len(decoded), escape_length)
        decoded_length += len(decoded)
        position = end
    if not position:
        return None
    builder.add_slice(text, position, len(text))
    decoded_length += len(text) - position
    del text
    index.finish(decoded_length)
    return builder.build(), index


def iter_artifact_credential_spans(text: str) -> Iterator[tuple[int, int]]:
    """Keyword-assigned values, prefixed API keys, private key blocks and credential fields, read in the text and in up
    to MAX_ESCAPE_DECODE_LAYERS layers of escapes below it; JWTs and auth schemes have their own iterators above."""
    yield from _iter_layer_spans(text)
    holder = [text]
    del text
    indexes: list[_EscapeIndex] = []
    for _ in range(MAX_ESCAPE_DECODE_LAYERS):
        if not any(marker in holder[0] for marker in _ESCAPE_LAYER_MARKERS):
            return
        decoded = _decode_escape_layer(holder)
        if decoded is None:
            return
        check_scan_budget()
        layer_text, index = decoded
        del decoded
        indexes.append(index)
        for start, end in _iter_layer_spans(layer_text, index.escaped):
            for layer_index in reversed(indexes):
                start = layer_index.encoded_offset(start)
                end = layer_index.encoded_offset(end)
            yield start, end
        holder.append(layer_text)
        del layer_text


def _starts_with_auth_scheme_credential(value: str) -> bool:
    match = AUTH_SCHEME_CREDENTIAL_RE.match(value, len(value) - len(value.lstrip()))
    return match is not None and _is_auth_scheme_credential(match)


def is_hex_credential(value: str) -> bool:
    return (
        _HEX_CREDENTIAL_RE.fullmatch(value) is not None
        and any(char.isdigit() for char in value)
        and any(char.isalpha() for char in value)
    )


def _is_whole_scheme_token(value: str) -> bool:
    match = _WHOLE_SCHEME_TOKEN_RE.fullmatch(value)
    if match is None or len(match.group(2)) < _MIN_WHOLE_SCHEME_TOKEN_CHARS:
        return False
    token = match.group(2)
    # A long word after `Token` is prose ("Token internationalization"), so that scheme needs a non-letter or an
    # uppercase letter past the first, which a capitalized word does not have.
    mixed_case = any(char.isupper() for char in token[1:]) and any(char.islower() for char in token)
    return match.group(1).lower() == "bearer" or not token.isalpha() or mixed_case


def _has_shape(value: str) -> bool:
    return (
        is_hex_credential(value)
        or any(_is_jwt_run(match.group()) for match in _TOKEN_RUN_RE.finditer(value))
        or _starts_with_auth_scheme_credential(value)
        or _is_whole_scheme_token(value)
    )


def has_credential_shape(value: str) -> bool:
    """Judges the raw and the percent-decoded value, a match on either counts; callers replace the raw one."""
    decoded = percent_decoded(value)
    return _has_shape(value) or (decoded != value and _has_shape(decoded))


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
