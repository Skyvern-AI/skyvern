from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from functools import partial
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.google_oauth import GOOGLE_GMAIL_SEND_SCOPE, GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service, microsoft_oauth_service
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

IntegrationStatus = Literal["live", "comingSoon"]
IntegrationKind = Literal["oauth", "credential_form", "external", "internal"]
IntegrationCategory = Literal[
    "Workspace",
    "Communication",
    "Docs",
    "Payments",
    "CRM",
    "Automation",
    "Agent Orchestrators",
    "Credentials",
]
OAuthProvider = Literal["google", "microsoft"]
AccountsUnavailable = Literal["not_enabled", "read_failed"]
OAUTH_ACCOUNT_PROVIDERS: frozenset[str] = frozenset({"google", "microsoft"})
ACCOUNT_READ_TIMEOUT_SECONDS = 5.0


class IntegrationAccount(BaseModel):
    connection_id: str | None = None
    provider: str
    name: str | None = None
    email_address: str | None = None
    state: str
    scopes_granted: list[str] | None = None
    source: Literal["organization", "instance_default"] | None = None


class IntegrationEntry(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    id: str
    name: str
    category: IntegrationCategory
    description: str
    keywords: list[str] = Field(default_factory=list)
    status: IntegrationStatus
    kind: IntegrationKind
    logo: str
    oauth_provider: OAuthProvider | None = None
    oauth_scope_profile: str | None = None
    required_scopes: list[str] | None = None
    capability_scopes: list[str] | None = None
    credential_form_id: str | None = None
    learn_more_url: str | None = None
    to: str | None = None
    # The page groups entries by kind and keeps this order inside each group.
    position: int = Field(exclude=True)
    accounts: list[IntegrationAccount] | None = None
    accounts_unavailable: AccountsUnavailable | None = None


def serialize_oauth_account(
    credential: GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase, provider: OAuthProvider
) -> dict[str, Any]:
    return oauth_account(credential, provider).model_dump(exclude_none=True)


def oauth_account(
    credential: GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase, provider: OAuthProvider
) -> IntegrationAccount:
    # Named fields rather than a model dump: both source models are token-free today,
    # so a dump would start leaking the day either one gains a token field.
    return IntegrationAccount(
        connection_id=credential.id,
        provider=provider,
        name=credential.credential_name,
        email_address=credential.email_address or None,
        state=credential.state,
        scopes_granted=list(credential.scopes_granted),
    )


def page_payload(entry: IntegrationEntry) -> dict[str, Any]:
    excluded = {"accounts", "accounts_unavailable"}
    if entry.status == "comingSoon" and entry.kind == "oauth":
        excluded.update({"oauth_provider", "oauth_scope_profile", "required_scopes"})
    return entry.model_dump(by_alias=True, exclude_none=True, exclude=excluded)


def copilot_payload(entry: IntegrationEntry) -> dict[str, Any]:
    data = entry.model_dump(exclude_none=True, exclude={"accounts", "keywords", "logo"})
    data["accounts"] = None if entry.accounts is None else [a.model_dump(exclude_none=True) for a in entry.accounts]
    return data


def listed_oauth_accounts(integrations: Sequence[Any]) -> list[dict[str, Any]]:
    # Older captures listed the accounts themselves as rows; current results nest them per entry.
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in integrations:
        if not isinstance(item, dict):
            continue
        nested = item.get("accounts") if "accounts" in item else [item]
        for account in nested if isinstance(nested, list) else []:
            if not isinstance(account, dict) or account.get("provider") not in OAUTH_ACCOUNT_PROVIDERS:
                continue
            connection_id = account.get("connection_id")
            if not isinstance(connection_id, str) or connection_id in seen:
                continue
            seen.add(connection_id)
            rows.append(account)
    return rows


_GOOGLE_GMAIL_ENTRY_ID = "gmail"

SKYVERN_INTEGRATIONS: tuple[IntegrationEntry, ...] = (
    IntegrationEntry(
        id="google_sheets",
        name="Google Sheets",
        category="Workspace",
        description="Read and write Google Sheets in your agents.",
        keywords=["spreadsheet", "workbook", "cells", "rows", "data", "google workspace"],
        status="live",
        kind="oauth",
        logo="google_sheets",
        oauth_provider="google",
        oauth_scope_profile="google_sheets",
        required_scopes=list(google_oauth_service.GOOGLE_SHEETS_SCOPES),
        capability_scopes=[google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE],
        position=0,
    ),
    IntegrationEntry(
        id=_GOOGLE_GMAIL_ENTRY_ID,
        name="Gmail",
        category="Communication",
        description="Read verification codes and send email from your agents.",
        keywords=[
            "email",
            "mail",
            "inbox",
            "2fa",
            "otp",
            "verification",
            "magic link",
            "google mail",
            "send",
            "report",
            "notification",
        ],
        status="live",
        kind="oauth",
        logo="gmail",
        oauth_provider="google",
        oauth_scope_profile="gmail",
        required_scopes=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
        position=1,
    ),
    IntegrationEntry(
        id="outlook",
        name="Outlook",
        category="Communication",
        description="Find verification codes and matching emails in connected Outlook inboxes.",
        keywords=["email", "mail", "inbox", "2fa", "otp", "verification", "magic link", "microsoft office 365 o365"],
        status="live",
        kind="oauth",
        logo="outlook",
        oauth_provider="microsoft",
        oauth_scope_profile=microsoft_oauth_service.MICROSOFT_OAUTH_SCOPE_PROFILE_OUTLOOK_MAIL,
        required_scopes=["Mail.Read"],
        position=2,
    ),
    IntegrationEntry(
        id="twilio",
        name="Twilio",
        category="Communication",
        description=(
            "Connected and enabled Twilio numbers deliver SMS 2FA codes automatically. "
            "Push codes for other numbers through the 2FA API or UI."
        ),
        keywords=["sms", "2fa", "otp", "verification", "phone number", "text message"],
        status="live",
        kind="credential_form",
        logo="message",
        credential_form_id="twilio",
        position=4,
    ),
    IntegrationEntry(
        id="google_drive",
        name="Google Drive",
        category="Workspace",
        description="Upload workflow files to Google Drive folders.",
        keywords=["cloud storage", "files", "folders", "upload", "download", "shared drive", "google workspace"],
        status="live",
        kind="oauth",
        logo="google_drive",
        oauth_provider="google",
        oauth_scope_profile="google_drive",
        required_scopes=list(google_oauth_service.GOOGLE_DRIVE_SCOPES),
        position=6,
    ),
    IntegrationEntry(
        id="mcp",
        name="MCP",
        category="Agent Orchestrators",
        description="Connect AI clients like Claude, Cursor, and Codex to Skyvern over the Model Context Protocol.",
        keywords=["model context protocol", "claude", "cursor", "codex", "ai client", "tool server"],
        status="live",
        kind="internal",
        logo="robot",
        to="/integrations/mcp",
        position=8,
    ),
    IntegrationEntry(
        id="onepassword",
        name="1Password",
        category="Credentials",
        description="Use your 1Password service account to inject credentials into agent logins.",
        keywords=["password manager", "vault", "secrets", "credentials", "service account", "login"],
        status="live",
        kind="credential_form",
        logo="onepassword",
        credential_form_id="onepassword",
        position=13,
    ),
    IntegrationEntry(
        id="bitwarden",
        name="Bitwarden",
        category="Credentials",
        description="Use your Bitwarden vault to source credentials in agent logins.",
        keywords=["password manager", "vault", "secrets", "credentials", "passwords", "login"],
        status="live",
        kind="credential_form",
        logo="bitwarden",
        credential_form_id="bitwarden",
        position=14,
    ),
    IntegrationEntry(
        id="azure_key_vault",
        name="Azure Key Vault",
        category="Credentials",
        description="Pull secrets from Azure Key Vault using a client secret credential.",
        keywords=["microsoft azure", "secrets", "credentials", "key management", "cloud vault", "client secret"],
        status="live",
        kind="credential_form",
        logo="azure_key_vault",
        credential_form_id="azure_key_vault",
        position=15,
    ),
    IntegrationEntry(
        id="custom_credential_service",
        name="Custom Credential Service",
        category="Credentials",
        description="Point Skyvern at your own HTTP API for credential management.",
        keywords=["credentials", "secrets", "passwords", "api", "http", "custom integration"],
        status="live",
        kind="credential_form",
        logo="key",
        credential_form_id="custom_credential_service",
        position=16,
    ),
    IntegrationEntry(
        id="powerbi",
        name="Power BI",
        category="Workspace",
        description="Run read-only DAX queries against Power BI datasets.",
        keywords=["microsoft", "dataset", "model", "dax", "analytics"],
        status="comingSoon",
        kind="oauth",
        logo="powerbi",
        oauth_provider="microsoft",
        oauth_scope_profile=microsoft_oauth_service.MICROSOFT_OAUTH_SCOPE_PROFILE_POWERBI_DATASET_READ,
        required_scopes=["Dataset.Read.All"],
        position=17,
    ),
)

AccountsById = dict[str, list[IntegrationAccount] | AccountsUnavailable]
AccountSource = Callable[[str], Awaitable[AccountsById]]


def _has_scopes(scopes: Collection[str], required: Collection[str]) -> bool:
    return all(scope in scopes for scope in required)


def _gmail_fallback(credential: GoogleOAuthCredentialBase, entry: IntegrationEntry) -> bool:
    if entry.id != _GOOGLE_GMAIL_ENTRY_ID:
        return False
    # A cancelled send upgrade leaves the send scope requested but not granted; it still belongs with Gmail.
    return google_oauth_service.GOOGLE_GMAIL_READONLY_SCOPE in credential.scopes_granted or GOOGLE_GMAIL_SEND_SCOPE in {
        *credential.scopes_granted,
        *credential.scopes_requested,
    }


def _page_shows_under(credential: GoogleOAuthCredentialBase, entry: IntegrationEntry) -> bool:
    required = entry.required_scopes or []
    if credential.scopes_requested:
        fits = _has_scopes(credential.scopes_requested, required)
    else:
        fits = _has_scopes(credential.scopes_granted, entry.capability_scopes or required)
    return fits or _gmail_fallback(credential, entry)


def _can_serve(credential: GoogleOAuthCredentialBase, entry: IntegrationEntry) -> bool:
    required = entry.required_scopes or []
    return (bool(credential.scopes_requested) and _has_scopes(credential.scopes_requested, required)) or _has_scopes(
        credential.scopes_granted, entry.capability_scopes or required
    )


def google_entry_ids_for_credential(
    credential: GoogleOAuthCredentialBase, entries: Sequence[IntegrationEntry]
) -> set[str]:
    shown = {entry.id for entry in entries if _page_shows_under(credential, entry)}
    # The page shows a legacy Sheets-only grant under no tile, but Copilot can still bind it.
    return shown or {entry.id for entry in entries if _can_serve(credential, entry)}


def _entries_for_provider(provider: OAuthProvider) -> list[IntegrationEntry]:
    return [entry for entry in SKYVERN_INTEGRATIONS if entry.status == "live" and entry.oauth_provider == provider]


async def _google_accounts(organization_id: str) -> AccountsById:
    credentials = await google_oauth_service.get_visible_credentials_for_org(organization_id)
    entries = _entries_for_provider("google")
    placements = [(credential, google_entry_ids_for_credential(credential, entries)) for credential in credentials]
    return {
        entry.id: [oauth_account(credential, "google") for credential, ids in placements if entry.id in ids]
        for entry in entries
    }


async def _microsoft_accounts(organization_id: str, user_id: str | None = None) -> AccountsById:
    entries = _entries_for_provider("microsoft")
    if not entries:
        return {}
    credentials = await microsoft_oauth_service.get_credentials_for_org(organization_id, user_id=user_id)
    return {
        entry.id: [
            oauth_account(credential, "microsoft")
            for credential in credentials
            if credential.state == "active"
            and microsoft_oauth_service.has_required_scopes(
                credential.scopes_requested or credential.scopes_granted, entry.required_scopes or []
            )
        ]
        for entry in entries
    }


async def _configured(organization_id: str, token_type: OrganizationAuthTokenType) -> bool:
    return await app.DATABASE.organizations.has_valid_org_auth_token(organization_id, token_type)


def _vault_source(entry_id: str, token_type: OrganizationAuthTokenType) -> AccountSource:
    async def accounts(organization_id: str) -> AccountsById:
        configured = await _configured(organization_id, token_type)
        return {entry_id: [IntegrationAccount(provider=entry_id, state="active")] if configured else []}

    return accounts


async def _onepassword_accounts(organization_id: str) -> AccountsById:
    if await _configured(organization_id, OrganizationAuthTokenType.onepassword_service_account):
        return {"onepassword": [IntegrationAccount(provider="onepassword", state="active", source="organization")]}
    if settings.OP_SERVICE_ACCOUNT_TOKEN and await app.AGENT_FUNCTION.is_onepassword_instance_default_allowed(
        organization_id
    ):
        return {"onepassword": [IntegrationAccount(provider="onepassword", state="active", source="instance_default")]}
    return {"onepassword": []}


async def _twilio_accounts(organization_id: str) -> AccountsById:
    if not SettingsManager.get_settings().TWILIO_SMS_2FA_ENABLED:
        return {"twilio": "not_enabled"}
    return await _vault_source("twilio", OrganizationAuthTokenType.twilio_credential)(organization_id)


_ACCOUNT_SOURCES: tuple[tuple[frozenset[str], AccountSource], ...] = (
    (frozenset(entry.id for entry in _entries_for_provider("google")), _google_accounts),
    (frozenset({"twilio"}), _twilio_accounts),
    (frozenset({"onepassword"}), _onepassword_accounts),
    (frozenset({"bitwarden"}), _vault_source("bitwarden", OrganizationAuthTokenType.bitwarden_credential)),
    (
        frozenset({"azure_key_vault"}),
        _vault_source("azure_key_vault", OrganizationAuthTokenType.azure_client_secret_credential),
    ),
    (
        frozenset({"custom_credential_service"}),
        _vault_source("custom_credential_service", OrganizationAuthTokenType.custom_credential_service),
    ),
)


async def read_account_source(organization_id: str, entry_ids: frozenset[str], source: AccountSource) -> AccountsById:
    try:
        async with asyncio.timeout(ACCOUNT_READ_TIMEOUT_SECONDS):
            return await source(organization_id)
    except Exception:
        LOG.warning(
            "Integration account source failed",
            organization_id=organization_id,
            entry_ids=sorted(entry_ids),
            exc_info=True,
        )
        return dict.fromkeys(entry_ids, "read_failed")


def with_accounts(
    entry: IntegrationEntry, accounts: list[IntegrationAccount] | AccountsUnavailable | None
) -> IntegrationEntry:
    if isinstance(accounts, str):
        return entry.model_copy(update={"accounts": None, "accounts_unavailable": accounts})
    return entry.model_copy(update={"accounts": accounts, "accounts_unavailable": None})


def integration_entries() -> list[IntegrationEntry]:
    return sorted(
        [*SKYVERN_INTEGRATIONS, *app.AGENT_FUNCTION.get_cloud_integration_entries()], key=lambda entry: entry.position
    )


def integration_is_live(integration_id: str) -> bool:
    return any(entry.id == integration_id and entry.status == "live" for entry in SKYVERN_INTEGRATIONS)


async def integrations_for_org(organization_id: str, user_id: str | None = None) -> list[IntegrationEntry]:
    microsoft_entry_ids = frozenset(entry.id for entry in _entries_for_provider("microsoft"))
    microsoft_reader = read_account_source(
        organization_id,
        microsoft_entry_ids,
        partial(_microsoft_accounts, user_id=user_id),
    )
    reads = await asyncio.gather(
        *(read_account_source(organization_id, entry_ids, source) for entry_ids, source in _ACCOUNT_SOURCES),
        microsoft_reader,
        app.AGENT_FUNCTION.get_cloud_integration_accounts(organization_id),
    )
    accounts: AccountsById = {}
    for found in reads:
        accounts.update(found)
    return [with_accounts(entry, accounts.get(entry.id)) for entry in integration_entries()]
