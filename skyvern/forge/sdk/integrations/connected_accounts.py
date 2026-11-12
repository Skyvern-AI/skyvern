from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice
from skyvern.forge.sdk.schemas.google_oauth import STATE_ACTIVE, GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service, microsoft_oauth_service
from skyvern.schemas.workflows import (
    BLOCK_YAML_SUBCLASSES,
    CONNECTED_ACCOUNT_ACTS_MARK,
    CONNECTED_ACCOUNT_CONDITION_MARK,
    CONNECTED_ACCOUNT_SCHEMA_MARK,
    BlockType,
)

LOG = structlog.get_logger()

ConnectionProvider = Literal["google", "microsoft"]

# ponytail: Nango connections are not among the org connections resolved here, so their fields stay outside the
# approval check; add a Nango reader to org_connections to bring them under it (SKY-19944).
_UNRESOLVED_CONNECTION_SOURCES = frozenset({"nango"})


@dataclass(frozen=True)
class _MarkedField:
    name: str
    condition: dict[str, tuple[Any, Any]]
    acts: bool


@functools.cache
def _connected_account_fields() -> dict[str, tuple[_MarkedField, ...]]:
    fields: dict[str, tuple[_MarkedField, ...]] = {}
    for model in get_args(BLOCK_YAML_SUBCLASSES):
        marked: list[_MarkedField] = []
        for name, info in model.model_fields.items():
            extra = info.json_schema_extra
            if not isinstance(extra, dict) or extra.get(CONNECTED_ACCOUNT_SCHEMA_MARK) is not True:
                continue
            condition = extra.get(CONNECTED_ACCOUNT_CONDITION_MARK)
            condition = dict(condition) if isinstance(condition, dict) else {}
            if condition.get("credential_source") in _UNRESOLVED_CONNECTION_SOURCES:
                continue
            defaults = {key: model.model_fields[key].default for key in condition if key in model.model_fields}
            marked.append(
                _MarkedField(
                    name,
                    {key: (value, defaults.get(key)) for key, value in condition.items()},
                    extra.get(CONNECTED_ACCOUNT_ACTS_MARK) is True,
                )
            )
        if marked:
            fields[BlockType(model.model_fields["block_type"].default).value] = tuple(marked)
    return fields


def connected_account_fields(block: Mapping[str, Any], *, acting_only: bool = False) -> tuple[str, ...]:
    """The marked fields this block reads, given the sibling values each mark's condition names. With
    `acting_only`, only those the schema marks as acting on the outside world."""
    return tuple(
        field.name
        for field in _connected_account_fields().get(str(block.get("block_type")), ())
        if (field.acts or not acting_only)
        and all(block.get(key, default) == value for key, (value, default) in field.condition.items())
    )


def block_connection_refs(block: Mapping[str, Any], *, acting_only: bool = False) -> list[str]:
    """The values of the block fields the block schema marks as connected-account ids, as written."""
    return [
        value.strip()
        for field in connected_account_fields(block, acting_only=acting_only)
        if isinstance(value := block.get(field), str) and value.strip()
    ]


@dataclass(frozen=True)
class OrgConnection:
    id: str
    provider: ConnectionProvider
    name: str
    email_address: str | None
    state: str
    scopes_granted: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE

    def choice(self) -> ConnectedAccountChoice:
        return ConnectedAccountChoice(
            connection_id=self.id, name=self.name, state=self.state, email_address=self.email_address
        )

    def facts(self) -> dict[str, Any]:
        return {
            "connection_id": self.id,
            "provider": self.provider,
            "name": self.name,
            "email_address": self.email_address,
            "state": self.state,
            "scopes_granted": list(self.scopes_granted),
        }


def _connection(
    row: GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase, provider: ConnectionProvider
) -> OrgConnection:
    return OrgConnection(
        id=row.id,
        provider=provider,
        name=row.credential_name,
        email_address=row.email_address,
        state=row.state,
        scopes_granted=tuple(row.scopes_granted),
    )


class IncompleteConnectionRead(Exception):
    """A provider's rows could not be read. `connections` holds the providers that did load; only an id lookup may
    use them, because a name that looks unique among them can still match a row in the provider that failed."""

    def __init__(self, connections: list[OrgConnection]) -> None:
        super().__init__("a connected-account provider could not be read")
        self.connections = connections


async def org_connections(organization_id: str, user_id: str | None = None) -> list[OrgConnection]:
    """Every OAuth connection `user_id` may see in the org, as one flat list."""
    # Not integrations_for_org: it files accounts under catalog entries by scope, and this list must not.
    results: list[Sequence[GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase] | BaseException] = list(
        await asyncio.gather(
            google_oauth_service.get_visible_credentials_for_org(organization_id),
            microsoft_oauth_service.get_credentials_for_org(organization_id, user_id=user_id),
            return_exceptions=True,
        )
    )
    providers: tuple[ConnectionProvider, ...] = ("google", "microsoft")
    connections: list[OrgConnection] = []
    failure: BaseException | None = None
    for provider, rows in zip(providers, results):
        if isinstance(rows, BaseException):
            if not isinstance(rows, Exception):
                raise rows
            LOG.warning(
                "org_connections_provider_read_failed",
                organization_id=organization_id,
                provider=provider,
                exc_info=rows,
            )
            failure = rows
            continue
        connections.extend(_connection(row, provider) for row in rows if row.organization_id == organization_id)
    if failure is not None:
        raise IncompleteConnectionRead(connections) from failure
    return connections


async def org_connections_by_id(organization_id: str, user_id: str | None = None) -> list[OrgConnection]:
    """For lookups by id only: when one provider cannot be read, the rows that did load still find their ids."""
    try:
        return await org_connections(organization_id, user_id)
    except IncompleteConnectionRead as incomplete:
        return incomplete.connections


@dataclass(frozen=True)
class UseHop:
    """The account's provider token stays behind the hop: the block calls its hop path instead of using a token."""

    credential_source: Literal["nango"] = "nango"


AccountAccess = str | UseHop | None
AccessFn = Callable[[str, str, list[str] | None], Awaitable[AccountAccess]]
# Each API's own backend is registered under the API's name.
AccountApi = Literal["google", "microsoft"]


@dataclass(frozen=True)
class AccountBackend:
    access: AccessFn
    # The provider APIs a token from this backend can call; a block pinned to an API only reaches these backends.
    apis: frozenset[AccountApi]


async def _google_access(organization_id: str, account_id: str, required_scopes: list[str] | None) -> AccountAccess:
    # Without scopes there is no grant gate, so a missing permission comes back as Google's own error at call time.
    if required_scopes is None:
        return await app.AGENT_FUNCTION.get_google_sheets_credentials(
            organization_id=organization_id, credential_id=account_id
        )
    credentials = await app.AGENT_FUNCTION.get_google_workspace_credentials(
        organization_id=organization_id, credential_id=account_id, required_scopes=required_scopes
    )
    return credentials.token if credentials else None


async def _microsoft_access(organization_id: str, account_id: str, required_scopes: list[str] | None) -> AccountAccess:
    return await app.AGENT_FUNCTION.get_microsoft_credentials(
        organization_id=organization_id, credential_id=account_id, required_scopes=required_scopes
    )


async def _nango_access(organization_id: str, account_id: str, required_scopes: list[str] | None) -> AccountAccess:
    return UseHop()


# ponytail: Nango accounts are not among org_connections yet, so the lookup never reaches "nango" until a Nango
# reader joins it. Its token never leaves the hop, so no block can call a provider API with it directly.
ACCOUNT_BACKENDS: dict[str, AccountBackend] = {
    "google": AccountBackend(_google_access, frozenset({"google"})),
    "microsoft": AccountBackend(_microsoft_access, frozenset({"microsoft"})),
    "nango": AccountBackend(_nango_access, frozenset()),
}


async def _known_account(organization_id: str, account_ref: str, api: AccountApi | None) -> OrgConnection | None:
    ref = account_ref.strip()
    return next(
        (
            connection
            for connection in await org_connections_by_id(organization_id)
            if connection.id == ref
            and connection.provider in ACCOUNT_BACKENDS
            and (api is None or api in ACCOUNT_BACKENDS[connection.provider].apis)
        ),
        None,
    )


async def resolve_account_access(
    organization_id: str,
    account_ref: str,
    required_scopes: list[str] | None = None,
    *,
    api: AccountApi | None = None,
) -> AccountAccess:
    """What a block needs to use the connected account `account_ref`, from the backend its provider names.
    A block that calls one provider's API passes `api`, so only an account whose token can call it resolves."""
    if api is not None and [name for name, backend in ACCOUNT_BACKENDS.items() if api in backend.apis] == [api]:
        # Only the API's own backend can serve it, so reading the org's accounts could not change the answer.
        return await ACCOUNT_BACKENDS[api].access(organization_id, account_ref, required_scopes)
    try:
        account = await _known_account(organization_id, account_ref, api)
    except Exception:
        if api is None:
            raise
        LOG.warning("connected_account_lookup_failed", organization_id=organization_id, exc_info=True)
        account = None
    if account is not None:
        return await ACCOUNT_BACKENDS[account.provider].access(organization_id, account.id, required_scopes)
    if api is not None:
        # Not a known id (a hand-written name or email, or an unreadable account list): the API's own backend
        # resolves the reference itself.
        return await ACCOUNT_BACKENDS[api].access(organization_id, account_ref, required_scopes)
    LOG.info("connected_account_not_resolved", organization_id=organization_id)
    return None


async def resolve_account_token(
    organization_id: str, account_ref: str, required_scopes: list[str] | None = None, *, api: AccountApi
) -> str | None:
    access = await resolve_account_access(organization_id, account_ref, required_scopes, api=api)
    if isinstance(access, UseHop):
        LOG.warning("connected_account_needs_hop", organization_id=organization_id, api=api)
        return None
    return access or None
