"""Signed payload URLs reach the model only as opaque tokens, minted here and resolved inside the tool
handlers the way credential placeholders are.

This module owns the URL/token SHAPE predicates the rest of v3 redacts against, so it must stay a
leaf: it imports nothing else from taskv3, and auth_tools/handoff_redaction import from it. Reversing
any of those edges reintroduces a cycle through loop.py."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote, unquote_plus, urlsplit

import structlog

from skyvern.forge.sdk.core.skyvern_context import mask_opaque_urls_in_text

LOG = structlog.get_logger()

_TOKEN_PREFIX = "opaque_url_"
# A value is a signing artifact only when its own key says so, since an order id or utm param can be
# just as token-shaped; matched by exact key component so "monkeyval" does not collide with "key".
_SIGNING_KEY_WORDS = {
    "sig",
    "signature",
    "token",
    "key",
    "apikey",
    "credential",
    "credentials",
    "secret",
    "auth",
    "authorization",
    "hmac",
    "sas",
    "signed",
}
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEP_RE = re.compile(r"[^A-Za-z0-9]+")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>`]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}"
# A JWT's own shape (three base64url segments joined by literal dots) identifies it as a signing
# artifact regardless of which key or URL part carries it - ordinary path segments and query values
# don't contain two internal dots surrounded by 10+ url-safe characters on each side. The header
# segment is required to start "eyJ" (the base64url encoding of a JSON object's leading `{"`, which
# every real JWT header is) rather than just matching the generic charset: without that anchor, three
# dot-separated runs of digits (build timestamps) or an ordinary dotted filename are shaped the same
# and would otherwise be masked for no signing-related reason.
#
# Each segment's quantifier is capped at 512 chars (far past any real JWT segment - HS256/RS256/ES256
# signatures top out under 350) rather than left unbounded: an unbounded quantifier followed by a
# literal "." that a crafted, dot-free, "eyJ"-repeated string never satisfies forces the engine to
# backtrack from the end of the string at every "eyJ" occurrence, which is quadratic in input length
# (measured: ~0.03s/0.11s/0.44s at 10/20/40KB - each doubling roughly quadruples the cost). Capping
# the quantifier bounds that backtrack to a constant per occurrence, keeping total cost linear.
_JWT_SEGMENT = r"[A-Za-z0-9_-]{10,512}"
_JWT_HEADER_SEGMENT = r"eyJ[A-Za-z0-9_-]{7,512}"
_JWT_RE = re.compile(rf"{_JWT_HEADER_SEGMENT}\.{_JWT_SEGMENT}\.{_JWT_SEGMENT}")
# A long run of nothing but hex digits is never a slug or sentence - real words need letters past
# "f" - so this identifies a hex-encoded signature/hash wherever it appears. Digits are technically
# valid hex characters too, though, so a match still needs an actual hex LETTER (checked separately)
# to rule out a plain padded numeric id (an invoice or tracking number) matching on digits alone.
_HEX_BLOB_RE = re.compile(r"[0-9a-fA-F]{32,}")
# When the key gives no hint, only a strong shape signal qualifies: base64-style output from random
# bytes mixes upper, lower, and digit characters the way a slug or URL path never does (those stick
# to one case convention), so require all three plus a length well above the keyed threshold. But
# case-mixing alone still matches a camelCase business identifier (an order ref, a campaign code),
# which mixes case too - the discriminator is that a real identifier concatenates whole fields (a
# literal year, a padded batch number), producing a run of 4+ consecutive digits, where random byte
# output almost never does by chance (measured false-negative rate on real random signatures: ~2-4%
# at typical lengths, versus ~40-70% for a same-case-letter-run heuristic that was tried and reverted
# - random output frequently contains a same-case run of 5+ letters purely by chance, so that signal
# would silently under-mask a large fraction of genuine signing blobs).
#
# Accepted tradeoff: a vendor scheme that packs a literal decimal field (a timestamp, a numeric key
# id) directly inside an otherwise-random blob, with no signing keyword in its key name, is a 100%
# miss here - there is no shape-only signal that can tell "a structured field concatenated into an
# identifier" apart from "a timestamp concatenated into a signing blob." Not fixed for the same reason
# the UUID/hex collision below isn't: no known production case exercises it, and chasing a better
# heuristic risks the same under-masking regression the same-case-run attempt caused.
#
# Accepted tradeoff: this floor is also a miss for a signing value that's short (<32 chars), single-
# case (a base32-style token), or digit-free, riding a non-allowlisted key or bare in a path segment -
# e.g. a 24-char single-case base64 path token is not caught. Not lowering the floor or relaxing the
# case/digit requirement: both were the exact axis that caused the two false-positive regressions
# above (a lower bar catches more benign short/single-case identifiers, not just more real tokens).
_MIN_SHAPE_ONLY_SIGNING_VALUE_CHARS = 32
_DIGIT_RUN_RE = re.compile(r"\d{4,}")
# Shorter values are codes/flags (lang=en, v=2), not link secrets, and a real link secret is at
# least this long; redacting the short ones would blank harmless text across the run's artifacts.
_MIN_REDACTED_QUERY_VALUE_CHARS = 16
# The charset an opaque token draws from. Excludes emails, URLs, and prose, which are readable
# values the model needs and redaction is global for the run.
_OPAQUE_QUERY_VALUE_RE = re.compile(r"[A-Za-z0-9._~+/=-]+")


def _is_signing_value(decoded: str) -> bool:
    # The key already established intent, so unlike a bare token the value need not mix letters and
    # digits: a base64 HMAC with no digit is still a signature.
    return len(decoded) >= _MIN_REDACTED_QUERY_VALUE_CHARS and _OPAQUE_QUERY_VALUE_RE.fullmatch(decoded) is not None


def _is_signing_key(key: str) -> bool:
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", key)
    components = {component.lower() for component in _KEY_SEP_RE.split(spaced) if component}
    return bool(components & _SIGNING_KEY_WORDS)


def _is_hex_blob(text: str) -> bool:
    """True iff ``text`` contains a 32+ run of hex-only characters that includes a hex LETTER - a
    plain decimal digit run (a padded invoice/tracking number) is not hex-shaped even though every
    digit is technically valid hex.

    A dash-stripped UUID used as a public resource id, a git commit SHA, or a content-hash cache-bust
    path segment (Docker/OCI digest, CDN asset hash) is shape-identical to a 32+-char MD5/SHA-style
    signature and will also match; there's no shape-only signal that tells them apart, and treating a
    real hash as a benign id would leak it to the model unmasked, so this accepts that false positive."""
    return any(not run.isdigit() for run in _HEX_BLOB_RE.findall(text))


def _is_high_entropy_blob(decoded: str) -> bool:
    """True for a value shaped like a random signing blob even though its key gives no hint."""
    if _is_hex_blob(decoded):
        return True
    return (
        len(decoded) >= _MIN_SHAPE_ONLY_SIGNING_VALUE_CHARS
        and _OPAQUE_QUERY_VALUE_RE.fullmatch(decoded) is not None
        and any(char.isupper() for char in decoded)
        and any(char.islower() for char in decoded)
        and any(char.isdigit() for char in decoded)
        and not _DIGIT_RUN_RE.search(decoded)
    )


def urls_in_text(text: str) -> list[str]:
    """Every http(s) URL literal in free ``text``, with sentence punctuation split off the end the way
    ``OpaqueUrlRefs.mint_in_text`` splits it — one scan, so a reader that has to decide whether a URL
    was written by the caller sees exactly the URLs the masker would have seen."""
    return [match.group(0).rstrip(_TRAILING_PUNCTUATION) for match in _URL_IN_TEXT_RE.finditer(text)]


def _signing_values(part: str) -> list[tuple[str, str]]:
    """Return (raw, decoded) for each ``key=value`` pair of a query/fragment string that is a signing
    secret: either the key names one, or - when it doesn't - the decoded value's own shape (a JWT, or a
    high-entropy blob) is unambiguous enough to qualify on its own. A capability-style token can also
    ride in the KEY position itself, bare, with a trivial value tacked on, or fused onto a signing word
    (``token-<blob>``) - the key's own shape is checked whenever the key match alone didn't already
    account for a qualifying value, rather than assuming a recognized signing word means the key is
    fully covered. JWT detection uses ``search`` everywhere (path, key, value), not ``fullmatch``, so a
    JWT with surrounding text (e.g. an echoed "Bearer <jwt>" value) is still caught the same way it is
    in the path."""
    values: list[tuple[str, str]] = []
    for pair in part.split("&"):
        if not pair:
            continue
        key, sep, raw_value = pair.partition("=")
        if _is_signing_key(key) and sep and _is_signing_value(unquote_plus(raw_value)):
            values.append((raw_value, unquote_plus(raw_value)))
            continue
        decoded_key = unquote_plus(key)
        if _JWT_RE.search(decoded_key) or _is_high_entropy_blob(decoded_key):
            values.append((key, decoded_key))
            continue
        if not sep:
            continue
        decoded_value = unquote_plus(raw_value)
        if _JWT_RE.search(decoded_value) or _is_high_entropy_blob(decoded_value):
            values.append((raw_value, decoded_value))
    return values


def is_signed_url(value: str) -> bool:
    """True iff ``value`` is an http(s) URL carrying a signing artifact: a signing-shaped query/fragment
    value (by key or, failing that, by shape alone), or a JWT / hex blob embedded directly in the path."""
    try:
        split = urlsplit(value)
    except ValueError:
        return False  # malformed URL-like text (e.g. an unclosed IPv6 bracket) is ordinary prose
    if split.scheme not in ("http", "https") or not split.netloc:
        return False
    # Split the RAW path on "/" first, then decode each segment individually - not the other way
    # around. A legitimate signing value can itself contain a "/" (standard base64's own alphabet),
    # percent-encoded to survive as one path segment; decoding the whole path before splitting would
    # turn that back into a literal separator and shred one opaque value into two shorter fragments
    # that individually evade every shape check below. Segments are also the natural boundary for the
    # per-segment checks: "/" is itself a valid character in the shared opaque-value charset, so
    # fullmatching a joined path would let it slip in as part of a "value" and catch ordinary
    # multi-segment slugs these checks were never meant to catch.
    for raw_segment in split.path.split("/"):
        if not raw_segment:
            continue
        # unquote(), not unquote_plus(): "+" is a legal, unreserved path character per RFC 3986 (it
        # only means space under query-string form-encoding), and the shared opaque-value charset
        # treats "+" as valid content - unquote_plus would silently corrupt an un-percent-encoded "+"
        # in a real signing value into a space, breaking the shape match.
        segment = unquote(raw_segment)
        if _JWT_RE.search(segment) or _is_hex_blob(segment) or _is_high_entropy_blob(segment):
            return True
    return bool(_signing_values(split.query) or _signing_values(split.fragment))


def _token_for(url: str) -> str:
    return f"{_TOKEN_PREFIX}{hashlib.sha256(url.encode()).hexdigest()[:8]}"


@dataclass
class OpaqueUrlRefs:
    masked: dict[str, Any] | None
    refs: dict[str, str]

    def resolve(self, text: str) -> str:
        if text in self.refs:
            return self.refs[text]
        resolved = text
        for token, url in self.refs.items():
            if token in resolved:
                resolved = resolved.replace(token, url)
        return resolved

    def derive(self, url: str) -> str:
        """Give ``url`` the provenance of a payload ref — a redirect target reached through one — so the
        boundary masks it and a later tool call can resolve it. Returns its token."""
        token = _token_for(url)
        self.refs[token] = url
        return token

    def mint_in_text(self, text: str) -> str:
        """Mint a ref for every signed URL inside free ``text`` and return the text with each replaced by
        its token. Unlike mask(), this is by shape: it is how a URL first gains payload provenance."""

        def _mint(match: re.Match[str]) -> str:
            # Sentence punctuation after a URL is not part of it and would fail the signature charset.
            url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
            trailing = match.group(0)[len(url) :]
            return (self.derive(url) if is_signed_url(url) else url) + trailing

        return _URL_IN_TEXT_RE.sub(_mint, text)

    def mask(self, text: str, *, cut: bool = False) -> str:
        """Replace every occurrence of a known payload signed-URL in ``text`` with its opaque token —
        the inverse of resolve(). Masking is by PROVENANCE, not URL shape: only a URL we minted from
        the payload is rewritten, so a live-page URL the model must reason about is never touched, even
        when it is itself signing-shaped (a ``?gclid=``/``?token=`` landing page). Output-only surfaces
        never resolve the token back; the token is the same one the payload masker minted for that URL.

        Shares one implementation with the model-facing boundary (SkyvernContext.hide_from_model), so
        the pre-truncation surfaces (observe/get_html) and the universal tool-result boundary can never
        diverge."""
        return mask_opaque_urls_in_text(text, self.refs, cut=cut)

    def resolve_deep(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.resolve(value)
        if isinstance(value, dict):
            return {key: self.resolve_deep(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.resolve_deep(val) for val in value)
        return value

    def chain(self, inner: Callable[[str], Any] | None) -> Callable[[str], Any]:
        def resolver(text: str) -> Any:
            # Credential resolver first (it pins active_credential_parameter_key as a side effect), opaque
            # tokens last: a resolved URL containing "placeholder_" must never reach the credential resolver.
            try:
                result: Any = inner(text) if inner is not None else text
            except Exception as exc:
                LOG.warning(
                    "taskv3 credential resolution failed; resolving opaque refs only",
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                result = text
            resolved_text = result if isinstance(result, str) else text
            return self.resolve(resolved_text)

        return resolver


def mask_opaque_urls(parameters: dict[str, Any] | None) -> OpaqueUrlRefs:
    """Replace every signed-URL string value in ``parameters`` with a deterministic opaque token.

    Never mutates ``parameters``; other values are copied unchanged."""
    result = OpaqueUrlRefs(masked=None, refs={})
    if parameters is None:
        return result

    def _mask(value: Any) -> Any:
        if isinstance(value, str):
            if is_signed_url(value):
                return result.derive(value)
            # A free-form payload string (e.g. task_data prose) can carry the URL inline.
            return result.mint_in_text(value)
        if isinstance(value, dict):
            return {key: _mask(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(_mask(val) for val in value)
        return value

    result.masked = _mask(parameters)
    return result
