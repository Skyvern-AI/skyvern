from typing import NoReturn

import pytest

from skyvern.forge.sdk.integrations import connected_accounts
from skyvern.forge.sdk.integrations.connected_accounts import (
    AccountApi,
    AccountBackend,
    IncompleteConnectionRead,
    OrgConnection,
)
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import _google_sheets_access

_ACCOUNTS = [
    OrgConnection("goac_1", "google", "Work", "ops@example.com", "active", ()),
    OrgConnection("msac_1", "microsoft", "work", None, "active", ()),
    OrgConnection("acct_fake", "fake", "Fake Sheet", None, "active", ()),  # type: ignore[arg-type]
]


@pytest.fixture(autouse=True)
def backends(monkeypatch: pytest.MonkeyPatch) -> None:
    def stub(provider: str, apis: frozenset[AccountApi]) -> None:
        async def access(organization_id: str, account_id: str, required_scopes: list[str] | None) -> str:
            return f"{provider}:{account_id}"

        monkeypatch.setitem(connected_accounts.ACCOUNT_BACKENDS, provider, AccountBackend(access, apis))

    stub("google", frozenset({"google"}))
    stub("microsoft", frozenset({"microsoft"}))
    # A new backend whose tokens call Google's API, registered with no change to any block.
    stub("fake", frozenset({"google"}))

    async def accounts(organization_id: str, user_id: str | None = None) -> list[OrgConnection]:
        return _ACCOUNTS

    monkeypatch.setattr(connected_accounts, "org_connections", accounts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "token"),
    [
        ("goac_1", "google:goac_1"),
        ("acct_fake", "fake:acct_fake"),
        # Anything but a known id that can call Google goes to Google's own hook, which resolves names itself.
        (" Work ", "google: Work "),
        ("msac_1", "google:msac_1"),
    ],
)
async def test_a_sheets_credential_reaches_only_a_backend_that_can_call_google(reference: str, token: str) -> None:
    assert (await _google_sheets_access("o_1", reference)).access_token == token


@pytest.mark.asyncio
async def test_an_unpinned_caller_resolves_only_a_known_account_id() -> None:
    assert await connected_accounts.resolve_account_access("o_1", "msac_1") == "microsoft:msac_1"
    assert await connected_accounts.resolve_account_access("o_1", "Work") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reference", "token"),
    [
        (IncompleteConnectionRead([_ACCOUNTS[2]]), "acct_fake", "fake:acct_fake"),
        (RuntimeError("account list unavailable"), "goac_1", "google:goac_1"),
    ],
)
async def test_an_account_read_failure_still_routes_the_ids_that_loaded(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, reference: str, token: str
) -> None:
    async def failing(organization_id: str, user_id: str | None = None) -> list[OrgConnection]:
        raise failure

    monkeypatch.setattr(connected_accounts, "org_connections", failing)

    assert (await _google_sheets_access("o_1", reference)).access_token == token


@pytest.mark.asyncio
async def test_sheets_reads_no_accounts_while_only_googles_own_backend_serves_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(connected_accounts.ACCOUNT_BACKENDS, "fake")

    async def unread(organization_id: str, user_id: str | None = None) -> NoReturn:
        raise pytest.fail.Exception("the account list was read")

    monkeypatch.setattr(connected_accounts, "org_connections", unread)

    assert (await _google_sheets_access("o_1", "goac_1")).access_token == "google:goac_1"
