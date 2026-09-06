"""Deterministic credential matching shared by every seam that needs credential authority.

Turn start and the browser scout run the same tiers over different evidence: prompt text
before a page exists, the live page URL once one does. Authority is therefore a function of
the evidence at hand, not a set computed once at turn start.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse

from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType

CredentialUrlTier = Literal["url_exact", "url_path", "url_host"]
CredentialResolutionTier = Literal["url_exact", "url_path", "url_host", "urlless_sole"]
CredentialResolutionVerdict = Literal["resolved", "ambiguous", "unresolved"]

_URL_TIERS: tuple[CredentialResolutionTier, ...] = ("url_exact", "url_path", "url_host")


@dataclass(frozen=True)
class CredentialResolution:
    verdict: CredentialResolutionVerdict
    candidates: tuple[Credential, ...] = ()
    tier: CredentialResolutionTier | None = None

    def contains(self, credential_id: str) -> bool:
        return any(candidate.credential_id == credential_id for candidate in self.candidates)


async def load_credentials(organization_id: str) -> list[Credential]:
    page = 1
    credentials: list[Credential] = []
    while True:
        items = await app.DATABASE.credentials.get_credentials(organization_id=organization_id, page=page, page_size=50)
        credentials.extend(items)
        if len(items) < 50:
            return sorted(credentials, key=lambda c: getattr(c, "created_at", None) or "", reverse=True)
        page += 1


def _parse_url(url: str) -> ParseResult | None:
    """`urlparse` that declines a malformed authority instead of raising.

    A bracket it cannot read as an IPv6 literal raises `ValueError`, and chat turns carry those
    routinely — a pasted markdown auto-link leaves `example.com](https://example.com` in the text.
    Every helper here runs ahead of its caller's own guard, so raising aborts the turn where
    declining only means the value names no site.
    """
    try:
        return urlparse(url)
    except ValueError:
        return None


def url_parts(url: str) -> tuple[str, str, str] | None:
    parsed = _parse_url(url if "://" in url else f"https://{url}")
    if parsed is None or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    # Login pages can differ only by query (?tenant=a vs ?tenant=b), so the
    # exact tier keys on the full URL with a param-order-insensitive query.
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    origin = f"{parsed.scheme.lower()}://{host}"
    # A live sign-in URL carries ?next=/?state=, so the path tier is what a saved tested_url
    # actually matches; the host tier alone would let any page on the host claim the credential.
    without_query = f"{origin}{path}"
    return (f"{without_query}?{query}" if query else without_query), without_query, origin


def safe_admitted_url(url: str) -> str:
    """The part of an admitted login URL that the origin check reads, with nothing else.

    A live sign-in URL carries `?state=`/`?code=`/reset tokens and can carry `user:pass@`. This value
    is persisted into the chat context and enters the next model prompt, so it keeps only the scheme,
    host, port and path the exact and path tiers match on.
    """
    parsed = _parse_url(url if "://" in url else f"https://{url}")
    if parsed is None or not parsed.hostname:
        return ""
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    # urlparse strips the brackets an IPv6 literal needs; without them the port reads as part of
    # the address and the next turn's origin check cannot canonicalize it.
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme.lower()}://{host}{port}{parsed.path.rstrip('/')}"


def loggable_origin(url: str) -> str:
    """The origin with any `user:pass@` dropped, for logs.

    `url_parts` keys on the whole netloc so that `https://real.example.com@evil.example/` cannot
    match `real.example.com`; that same netloc would carry basic-auth credentials into a log line.
    """
    parsed = _parse_url(url if "://" in url else f"https://{url}")
    if parsed is None or not parsed.hostname:
        return ""
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme.lower()}://{parsed.hostname}{port}"


def is_resolved_page_url(url: str) -> bool:
    """Whether this is an absolute page URL a URL match may be computed against.

    Scheme-less input is not a page. The `https://` normalization these matchers apply would turn
    any bare token — a prompt sentinel, a tool-argument echo, a placeholder — into a hostname that
    matches nothing, and the resulting "no match" reads as a fact about the org's saved credentials.
    """
    parsed = _parse_url(url)
    if parsed is None:
        return False
    # Keyed on `hostname` where `url_parts` keys the whole netloc, so `https://a.example@b.example`
    # passes here and still matches no saved credential there; netloc would not harden this.
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def unresolved_page_url_for_log(url: str) -> str:
    """What a value that is not a page URL points at: its host, else its scheme, else the bare token.

    Userinfo, query, fragment, params and path are the parts a URL carries content in, and content
    is where a secret rides, so the identity a log may name is only ever the components above them.
    A value too malformed to parse has no identity to name, and the outcome field carries that.
    """
    parsed = _parse_url(url)
    if parsed is None:
        return ""
    if parsed.hostname:
        return f"{parsed.scheme.lower()}://{parsed.hostname}"[:64] if parsed.scheme else f"//{parsed.hostname}"[:64]
    if parsed.scheme:
        return f"{parsed.scheme.lower()}:"[:64]
    return parsed.path[:64]


def deduplicate_credentials(credentials: list[Credential]) -> list[Credential]:
    by_id: dict[str, Credential] = {}
    for credential in credentials:
        by_id.setdefault(credential.credential_id, credential)
    return list(by_id.values())


def credential_reference_spans(message: str, reference: str) -> list[tuple[int, int]]:
    """Return structurally delimited occurrences of one exact saved reference."""
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = message.find(reference, start)
        if index < 0:
            return spans
        end = index + len(reference)
        before = message[index - 1] if index else ""
        before_is_boundary = not before or before.isspace() or before in "([{\"'`"
        cursor = end
        while cursor < len(message) and message[cursor] in ")]\"}'`":
            cursor += 1
        if cursor < len(message) and message[cursor] in ".,;:!?":
            cursor += 1
        after_is_boundary = cursor == len(message) or message[cursor].isspace()
        if before_is_boundary and after_is_boundary:
            spans.append((index, end))
        start = index + 1


def grounded_references(message: str, candidates: Iterable[str]) -> set[str]:
    """Find complete candidate literals in a turn, preferring the longest overlap: a shorter
    candidate that only occurs inside a longer one is not cited on its own."""
    occurrences = [
        (reference, start, end)
        for reference in {candidate for candidate in candidates if candidate}
        for start, end in credential_reference_spans(message, reference)
    ]
    return {
        reference
        for reference, start, end in occurrences
        if not any(
            other_start <= start and end <= other_end and (other_start, other_end) != (start, end)
            for _other, other_start, other_end in occurrences
        )
    }


def grounded_credential_references(message: str, credentials: list[Credential]) -> set[str]:
    """Find complete saved names/IDs in a turn, preferring the longest overlap."""
    return grounded_references(
        message, (value for credential in credentials for value in (credential.name, credential.credential_id))
    )


def _match_by_url_tiered(
    credentials: list[Credential],
    urls: list[str],
    *,
    tiers: Sequence[CredentialUrlTier],
) -> tuple[CredentialResolutionTier | None, list[Credential]]:
    indexed = [
        (credential, parts)
        for credential in credentials
        if credential.tested_url and (parts := url_parts(credential.tested_url))
    ]
    requested = [parts for url in urls if (parts := url_parts(url))]
    # Ranked in _URL_TIERS order whatever order the caller listed them in: an exact match must
    # never lose to a looser one.
    for tier in sorted(set(tiers), key=_URL_TIERS.index):
        index = _URL_TIERS.index(tier)
        matches = [
            credential
            for credential, parts in indexed
            # A query on the *saved* URL is identity — a tenant, an account. Only the live page
            # brings transient ?next=/?state=, so dropping the query is safe in that direction only.
            if not (tier == "url_path" and parts[0] != parts[1])
            and any(parts[index] == target[index] for target in requested)
        ]
        if matches:
            return tier, deduplicate_credentials(matches)
    return None, []


def resolve_by_url(
    credentials: list[Credential],
    urls: list[str],
    *,
    tiers: Sequence[CredentialUrlTier],
    password_only: bool = True,
    allow_urlless_sole: bool = False,
) -> CredentialResolution:
    """Rank saved credentials against login-page URLs over ``tiers``, in `_URL_TIERS` order, then a
    URL-less sole candidate. ``password_only`` keeps a card or secret credential carrying a
    ``tested_url`` from auto-binding as a login; naming one explicitly is a different tier."""
    pool = (
        [credential for credential in credentials if credential.credential_type == CredentialType.PASSWORD]
        if password_only
        else list(credentials)
    )
    tier, matches = _match_by_url_tiered(pool, urls, tiers=tiers)
    if not matches and allow_urlless_sole:
        # A credential saved without a login URL is still a valid login candidate (SKY-12959);
        # it ranks below every URL tier so a tested match always wins.
        tier, matches = (
            "urlless_sole",
            deduplicate_credentials([credential for credential in pool if not credential.tested_url]),
        )
    if len(matches) == 1:
        return CredentialResolution("resolved", tuple(matches), tier)
    if matches:
        return CredentialResolution("ambiguous", tuple(matches), tier)
    return CredentialResolution("unresolved")
