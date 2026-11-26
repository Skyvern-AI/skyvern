import pytest

from skyvern.forge.sdk.integrations import connected_accounts
from skyvern.forge.sdk.integrations.account_fit import account_fit, action_fits, block_fit, block_needs
from skyvern.forge.sdk.integrations.catalog import SKYVERN_INTEGRATIONS, IntegrationAccount, entry_action
from skyvern.forge.sdk.integrations.connected_accounts import (
    AccountApi,
    AccountBackend,
    IncompleteConnectionRead,
    OrgConnection,
)
from skyvern.forge.sdk.services.google_oauth_service import GOOGLE_GMAIL_READONLY_SCOPE
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import _google_sheets_access
from skyvern.schemas.workflows import BlockType

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
async def test_a_pinned_caller_uses_the_requested_api() -> None:
    assert await connected_accounts.resolve_account_access("o_1", "msac_1", api="microsoft") == "microsoft:msac_1"
    assert await connected_accounts.resolve_account_access("o_1", "Work", api="microsoft") == "microsoft:Work"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reference", "token"),
    [
        (IncompleteConnectionRead([_ACCOUNTS[2]], frozenset({"google"})), "acct_fake", "fake:acct_fake"),
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


def test_account_fit_compares_needs_with_granted_scopes_and_nango_is_unknown() -> None:
    _, read = entry_action(SKYVERN_INTEGRATIONS, "gmail", "read_inbox")
    _, send = entry_action(SKYVERN_INTEGRATIONS, "gmail", "send_email")
    _, outlook_read = entry_action(SKYVERN_INTEGRATIONS, "outlook", "read_inbox")
    reader = IntegrationAccount(provider="google", state="active", scopes_granted=[GOOGLE_GMAIL_READONLY_SCOPE])
    nango = IntegrationAccount(provider="nango", state="active")
    outlook = IntegrationAccount(
        provider="microsoft", state="active", scopes_granted=["https://graph.microsoft.com/Mail.ReadWrite"]
    )
    full_mail = IntegrationAccount(provider="google", state="active", scopes_granted=["https://mail.google.com/"])
    unrecorded = IntegrationAccount(provider="google", state="active", scopes_granted=[])

    assert (account_fit(reader, read), account_fit(reader, send)) == ("yes", "no")
    assert account_fit(full_mail, send) == account_fit(outlook, outlook_read) == "yes"
    assert account_fit(nango, send) == account_fit(unrecorded, send) == "unknown"
    # What the Google account list returns per credential for the pickers.
    fits = action_fits("google", [GOOGLE_GMAIL_READONLY_SCOPE])
    assert (fits["gmail.read_inbox"], fits["gmail.send_email"]) == ("yes", "no")

    reader_row = OrgConnection("goac_r", "google", "Reader", None, "active", (GOOGLE_GMAIL_READONLY_SCOPE,))
    assert (block_fit(reader_row, BlockType.SEND_EMAIL), block_fit(reader_row, BlockType.EMAIL_INBOX)) == ("no", "yes")
    # A read-only Sheets grant can read but not write, and the catalog lists one scope for both.
    sheets_reader = OrgConnection(
        "goac_s", "google", "Sheets", None, "active", ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    )
    assert block_fit(sheets_reader, BlockType.GOOGLE_SHEETS_READ) == "unknown"
    assert block_fit(sheets_reader, BlockType.GOOGLE_SHEETS_WRITE) == "unknown"
    # Full Drive access covers Sheets, so a Drive-connected account is a clear "yes" for reading and writing.
    drive = OrgConnection("goac_d", "google", "Drive", None, "active", ("https://www.googleapis.com/auth/drive",))
    assert block_fit(drive, BlockType.GOOGLE_SHEETS_READ) == block_fit(drive, BlockType.GOOGLE_SHEETS_WRITE) == "yes"
    # A narrow-only Drive grant reaches only the files a person chose, so whether an upload works is unknown.
    drive_file = OrgConnection(
        "goac_f", "google", "Drive", None, "active", ("https://www.googleapis.com/auth/drive.file",)
    )
    assert block_fit(drive_file, BlockType.FILE_UPLOAD) == "unknown"
    # The grant older Sheets connections hold, which reconnects keep: it can read and write sheets, not upload.
    legacy_sheets = OrgConnection(
        "goac_l",
        "google",
        "Sheets",
        None,
        "active",
        (
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ),
    )
    assert block_fit(legacy_sheets, BlockType.FILE_UPLOAD) == "no"
    # A refusal names only the permissions of the run's own providers.
    assert block_needs(BlockType.SEND_EMAIL, {"google"}) == ["https://www.googleapis.com/auth/gmail.send"]
    assert block_needs(BlockType.EMAIL_INBOX, {"microsoft"}) == ["Mail.Read"]
    # No Microsoft action runs as send_email, so an Outlook account's fit there is unknown, never "no".
    outlook_row = OrgConnection("msac_1", "microsoft", "work", None, "active", ("Mail.Read",))
    assert block_fit(outlook_row, BlockType.SEND_EMAIL) == "unknown"
