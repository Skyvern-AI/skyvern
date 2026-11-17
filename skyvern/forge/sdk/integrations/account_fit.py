"""Whether a connected account holds the permissions an action needs. Fit labels and filters account choices, and the
agent step refuses a tool call naming a "no" account; no run is refused on scopes, and anything short of a clear miss is
"unknown" so no working account is hidden."""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from skyvern.forge.sdk.integrations.catalog import (
    SKYVERN_INTEGRATIONS,
    IntegrationAccount,
    IntegrationAction,
    IntegrationTrigger,
)
from skyvern.forge.sdk.integrations.connected_accounts import OrgConnection
from skyvern.forge.sdk.schemas.google_oauth import GOOGLE_GMAIL_SEND_SCOPE
from skyvern.forge.sdk.services import google_oauth_service, microsoft_oauth_service
from skyvern.schemas.workflows import BlockType

AccountFit = Literal["yes", "no", "unknown"]

_GOOGLE = "https://www.googleapis.com/auth/"
_GOOGLE_DRIVE_SCOPE = google_oauth_service.GOOGLE_DRIVE_SCOPES[0]
# Scopes that grant everything a listed scope does.
_BROADER: dict[str, tuple[str, ...]] = {
    google_oauth_service.GOOGLE_GMAIL_READONLY_SCOPE: (f"{_GOOGLE}gmail.modify", "https://mail.google.com/"),
    GOOGLE_GMAIL_SEND_SCOPE: (f"{_GOOGLE}gmail.modify", f"{_GOOGLE}gmail.compose", "https://mail.google.com/"),
    google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE: (_GOOGLE_DRIVE_SCOPE,),
    "Mail.Read": ("Mail.ReadWrite",),
}
# Scopes that grant part of a listed scope (its read half, or only some files), so whether the action works is unknown.
_PARTIAL: dict[str, tuple[str, ...]] = {
    google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE: (
        f"{_GOOGLE}spreadsheets.readonly",
        f"{_GOOGLE}drive.readonly",
        f"{_GOOGLE}drive.file",
    ),
}


def _holds(provider: str, scopes_granted: Collection[str], scopes: Collection[str]) -> bool:
    if provider == "microsoft":
        # Matches by scope name, so a "https://graph.microsoft.com/" prefix on either side does not matter.
        return any(microsoft_oauth_service.has_required_scopes(tuple(scopes_granted), [scope]) for scope in scopes)
    return not set(scopes).isdisjoint(scopes_granted)


def _fit(provider: str, scopes_granted: Collection[str] | None, needs: Collection[str]) -> AccountFit:
    # Nango accounts and rows that never recorded a grant report no scopes, so the provider answers at call time.
    if not scopes_granted or provider == "nango":
        return "unknown"
    fits = {_need_fit(provider, scopes_granted, need) for need in needs}
    if "no" in fits:
        return "no"
    return "unknown" if "unknown" in fits else "yes"


def _need_fit(provider: str, scopes_granted: Collection[str], need: str) -> AccountFit:
    if _holds(provider, scopes_granted, (need, *_BROADER.get(need, ()))):
        return "yes"
    return "unknown" if _holds(provider, scopes_granted, _PARTIAL.get(need, ())) else "no"


def account_fit(account: IntegrationAccount, action: IntegrationAction | IntegrationTrigger) -> AccountFit:
    return _fit(account.provider, account.scopes_granted, action.needs)


def connection_fit(connection: OrgConnection, action: IntegrationAction | IntegrationTrigger) -> AccountFit:
    return _fit(connection.provider, connection.scopes_granted, action.needs)


def action_fits(provider: str, scopes_granted: Collection[str] | None) -> dict[str, AccountFit]:
    """The account's fit for every built-in action of its provider, keyed "<integration id>.<action id>"."""
    return {
        f"{entry.id}.{action.id}": _fit(provider, scopes_granted, action.needs)
        for entry in SKYVERN_INTEGRATIONS
        if entry.oauth_provider == provider
        for action in entry.actions
    }


def block_needs(block_type: BlockType, providers: Collection[str]) -> list[str]:
    """Every permission the catalog lists for the actions this block type runs on these account providers, to name
    in a refusal."""
    return sorted(
        {
            scope
            for entry in SKYVERN_INTEGRATIONS
            if entry.oauth_provider in providers
            for action in entry.actions
            if action.runs_via == f"block:{block_type.value}"
            for scope in action.needs
        }
    )


def block_fit(connection: OrgConnection, block_type: BlockType) -> AccountFit:
    """The fit for the actions this block type runs on the account's provider: "no" only when every one is a clear
    "no", unknown when none is listed."""
    fits = {
        connection_fit(connection, action)
        for entry in SKYVERN_INTEGRATIONS
        if entry.oauth_provider == connection.provider
        for action in entry.actions
        if action.runs_via == f"block:{block_type.value}"
    }
    if "yes" in fits:
        return "yes"
    return "no" if fits == {"no"} else "unknown"
