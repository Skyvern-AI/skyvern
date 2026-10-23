"""Deterministic credential matching shared by every seam that needs credential authority.

Turn start and the browser scout run the same tiers over different evidence: prompt text
before a page exists, the live page URL once one does. Authority is therefore a function of
the evidence at hand, not a set computed once at turn start.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse

import structlog

from skyvern.exceptions import BitwardenBaseError, BitwardenLoginError, BitwardenUnlockError
from skyvern.forge import app
from skyvern.forge.sdk.copilot.workflow_credential_utils import connected_vault_credential_id, connected_vault_item_id
from skyvern.forge.sdk.credential_site_policy import LoginSite
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.schemas.organizations import BitwardenOrganizationAuthToken, Organization
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.services.request_principal import get_request_principal

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy

LOG = structlog.get_logger()

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


async def credential_folder_ids_for_current_request() -> set[str] | None:
    principal = get_request_principal()
    return await app.AGENT_FUNCTION.accessible_credential_folder_ids(principal) if principal is not None else None


async def load_credentials(
    organization_id: str,
    *,
    credential_ids: Sequence[str] | None = None,
    page: int | None = None,
    page_size: int = 50,
    include_deleted: bool = False,
) -> list[Credential]:
    allowed_folder_ids = await credential_folder_ids_for_current_request()
    if credential_ids is not None:
        return await app.DATABASE.credentials.get_credentials_by_ids(
            list(credential_ids),
            organization_id=organization_id,
            allowed_folder_ids=allowed_folder_ids,
            include_deleted=include_deleted,
        )
    if page is not None:
        return await app.DATABASE.credentials.get_credentials(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            allowed_folder_ids=allowed_folder_ids,
        )

    page = 1
    credentials: list[Credential] = []
    while True:
        items = await app.DATABASE.credentials.get_credentials(
            organization_id=organization_id,
            page=page,
            page_size=50,
            allowed_folder_ids=allowed_folder_ids,
        )
        credentials.extend(items)
        if len(items) < 50:
            return sorted(credentials, key=lambda c: getattr(c, "created_at", None) or "", reverse=True)
        page += 1


CONNECTED_VAULT_TIMEOUT_SECONDS = 30

ConnectedVaultStatus = Literal["not_configured", "connected", "unreadable", "not_available_to_user"]
ConnectedVaultFailure = Literal["login_failed", "timeout", "unreadable"]


@dataclass(frozen=True)
class ConnectedVaultLogin:
    credential: Credential
    collection_id: str | None
    # The first URI, a display hint only.
    url: str | None
    # Every saved URI without its match mode: enough to say a login competes for a page, never to grant it.
    urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectedVault:
    status: ConnectedVaultStatus
    reason: ConnectedVaultFailure | None = None
    logins: tuple[ConnectedVaultLogin, ...] = ()

    @property
    def credentials(self) -> list[Credential]:
        return [login.credential for login in self.logins]

    def login(self, credential_id: str) -> ConnectedVaultLogin | None:
        return next((login for login in self.logins if login.credential.credential_id == credential_id), None)


def connected_vault_credential(organization_id: str, item_id: str, title: str) -> Credential:
    now = datetime.now(UTC)
    return Credential(
        credential_id=connected_vault_credential_id(item_id),
        organization_id=organization_id,
        name=title,
        vault_type=CredentialVaultType.BITWARDEN,
        item_id=item_id,
        credential_type=CredentialType.PASSWORD,
        username=None,
        card_last4=None,
        card_brand=None,
        created_at=now,
        modified_at=now,
    )


def _connected_vault_failure(error: BaseException) -> ConnectedVaultFailure:
    if isinstance(error, (BitwardenLoginError, BitwardenUnlockError)):
        return "login_failed"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "unreadable"


async def connected_vault_access_denied(organization_id: str) -> bool:
    """Vault items carry no credential folder, so only a principal no folder restricts may use them."""
    principal = get_request_principal()
    if principal is None:
        return await app.AGENT_FUNCTION.credential_folders_restricted(organization_id)
    return await app.AGENT_FUNCTION.accessible_credential_folder_ids(principal) is not None


async def _org_vault_access(organization_id: str) -> tuple[BitwardenOrganizationAuthToken, Organization] | None:
    # Org-scoped only, like the editor's item listing: never the instance's global Bitwarden credentials.
    token = await app.DATABASE.organizations.get_valid_org_auth_token(
        organization_id, OrganizationAuthTokenType.bitwarden_credential.value
    )
    if token is None:
        return None
    organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
    return (token, organization) if organization is not None else None


async def _read_connected_vault(organization_id: str) -> ConnectedVault:
    access = await _org_vault_access(organization_id)
    if access is None:
        return ConnectedVault("not_configured")
    if await connected_vault_access_denied(organization_id):
        return ConnectedVault("not_available_to_user")
    token, organization = access
    try:
        overviews = await BitwardenService.list_item_overviews(
            client_id=None,
            client_secret=None,
            master_password=token.credential.master_password,
            bw_organization_id=organization.bw_organization_id,
            bw_collection_ids=organization.bw_collection_ids,
            email=str(token.credential.email),
            timeout=CONNECTED_VAULT_TIMEOUT_SECONDS,
        )
    except (BitwardenBaseError, TimeoutError) as error:
        reason = _connected_vault_failure(error)
        # The exception text can carry the account email, so only its type is logged.
        LOG.warning(
            "copilot connected vault unreadable",
            organization_id=organization_id,
            reason=reason,
            error_type=type(error).__name__,
        )
        return ConnectedVault("unreadable", reason)
    return ConnectedVault(
        "connected",
        logins=tuple(
            ConnectedVaultLogin(
                credential=connected_vault_credential(organization_id, overview.item_id, overview.title),
                collection_id=overview.collection_id,
                url=overview.url,
                urls=tuple(overview.urls),
            )
            for overview in overviews
            if overview.credential_type == CredentialType.PASSWORD
        ),
    )


async def load_connected_vault(organization_id: str, policy: RequestPolicy | None) -> ConnectedVault:
    """The organization's connected Bitwarden logins. A listing force-syncs the vault, so a connected one is
    read once per turn; a failed read is not kept, so the next call retries."""
    if policy is not None and policy.connected_vault is not None:
        return policy.connected_vault
    vault = await _read_connected_vault(organization_id)
    if policy is not None and vault.status == "connected":
        policy.connected_vault = vault
        listed = {credential.credential_id: credential for credential in vault.credentials}
        # Approvals hydrate as id-only stubs; the listing supplies their titles.
        policy.resolved_credentials = [listed.get(c.credential_id, c) for c in policy.resolved_credentials]
        policy.auto_bound_credentials = [listed.get(c.credential_id, c) for c in policy.auto_bound_credentials]
    return vault


async def load_credentials_by_ids(organization_id: str, credential_ids: Sequence[str]) -> list[Credential]:
    """Saved credentials by id, plus an id-only stub for each connected-vault id. Nothing here lists the vault:
    a stub's login, collection and sites are read when a tool first needs them."""
    saved_ids = [credential_id for credential_id in credential_ids if connected_vault_item_id(credential_id) is None]
    credentials = await load_credentials(organization_id, credential_ids=saved_ids) if saved_ids else []
    vault_item_ids = [
        item_id for credential_id in credential_ids if (item_id := connected_vault_item_id(credential_id))
    ]
    if vault_item_ids and not await connected_vault_access_denied(organization_id):
        credentials.extend(
            connected_vault_credential(organization_id, item_id, connected_vault_credential_id(item_id))
            for item_id in vault_item_ids
        )
    return credentials


async def connected_vault_login_sites(
    organization_id: str, policy: RequestPolicy, credential_id: str
) -> list[LoginSite] | None:
    """Sites a vault login is saved for, scoped by its own URI match modes; None when they cannot be read."""
    cached = policy.connected_vault_sites.get(credential_id)
    if cached is not None:
        return cached
    item_id = connected_vault_item_id(credential_id)
    access = await _org_vault_access(organization_id) if item_id else None
    if item_id is None or access is None or await connected_vault_access_denied(organization_id):
        return None
    token, organization = access
    try:
        sites = await BitwardenService.get_login_item_sites(
            None,
            None,
            token.credential.master_password,
            organization.bw_organization_id,
            organization.bw_collection_ids,
            item_id,
            email=str(token.credential.email),
            timeout=CONNECTED_VAULT_TIMEOUT_SECONDS,
        )
    except (BitwardenBaseError, TimeoutError) as error:
        LOG.info(
            "copilot could not read the connected vault login's sites",
            credential_id=credential_id,
            error_type=type(error).__name__,
        )
        return None
    policy.connected_vault_sites[credential_id] = sites
    return sites


async def load_connected_vault_item(organization_id: str, login: ConnectedVaultLogin) -> CredentialItem:
    item_id = login.credential.item_id
    access = await _org_vault_access(organization_id)
    if access is None:
        raise LookupError("the connected vault is no longer configured")
    token, organization = access
    secret = await BitwardenService.get_secret_value_from_url(
        None,
        None,
        token.credential.master_password,
        organization.bw_organization_id,
        organization.bw_collection_ids,
        collection_id=login.collection_id,
        item_id=item_id,
        max_retries=1,
        timeout=CONNECTED_VAULT_TIMEOUT_SECONDS,
        email=str(token.credential.email),
    )
    totp = secret.get(BitwardenConstants.TOTP) or None
    return CredentialItem(
        item_id=item_id,
        name=login.credential.name,
        credential_type=CredentialType.PASSWORD,
        credential=PasswordCredential(
            username=secret.get(BitwardenConstants.USERNAME) or "",
            password=secret.get(BitwardenConstants.PASSWORD) or "",
            totp=totp,
            totp_type=TotpType.AUTHENTICATOR if totp else TotpType.NONE,
        ),
    )


async def count_visible_credentials(organization_id: str) -> int:
    return await app.DATABASE.credentials.count_credentials(
        organization_id, allowed_folder_ids=await credential_folder_ids_for_current_request()
    )


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
        port = f":{parsed.port}" if parsed.port is not None else ""
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
