import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.services import bitwarden as bitwarden_module
from skyvern.forge.sdk.services.bitwarden import (
    BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
    BitwardenItemType,
    BitwardenService,
    RunCommandResult,
    get_list_response_item_from_bitwarden_item,
)
from tests.unit.scoped_asyncio import ScopedAsyncio

# The vault server omits `totp` entirely for a login saved without a two-factor secret,
# and returns it as JSON null for some hand-made items.
MISSING_TOTP_LOGINS = [
    pytest.param({"username": "user@example.com", "password": "pw"}, id="totp-absent"),
    pytest.param({"username": "user@example.com", "password": "pw", "totp": None}, id="totp-null"),
]


@pytest.mark.asyncio
async def test_login_ignores_data_file_creation_notice_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_command(*args, **kwargs) -> RunCommandResult:
        return RunCommandResult(
            stdout="You are logged in!\n\nTo unlock your vault, use the `unlock` command.",
            stderr='Could not find data file, "/tmp/bitwarden/data.json"; creating it instead.\n',
            returncode=0,
        )

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    await BitwardenService.login("client-id", "client-secret", master_password="master-password")


@pytest.mark.asyncio
async def test_server_login_item_round_trips_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"tenant": "north", "account_id": "acct_123"}
    stored_item: dict = {}
    get_json = AsyncMock(
        side_effect=[
            {"data": {"template": {}}},
            {"data": {"template": {}}},
            {"success": True, "data": stored_item},
        ]
    )
    post = AsyncMock(return_value={"success": True, "data": {"id": "item-1"}})
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)
    monkeypatch.setattr(bitwarden_module, "aiohttp_post", post)

    item_id = await BitwardenService._create_login_item_using_server(
        bw_organization_id="bw-org",
        collection_id="collection-1",
        name="Login",
        credential=PasswordCredential(username="user@example.com", password="pw", totp="", metadata=metadata),
    )
    stored_item.update(post.await_args.kwargs["data"], id=item_id)
    listed_item = get_list_response_item_from_bitwarden_item(stored_item)
    fetched_item = await BitwardenService._get_credential_item_by_id_using_server(item_id)

    assert stored_item["fields"] == [
        {
            "name": "metadata_tenant",
            "value": "north",
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        },
        {
            "name": "metadata_account_id",
            "value": "acct_123",
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        },
    ]
    assert listed_item.credential.metadata == metadata
    assert fetched_item.credential.metadata == metadata


@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
def test_list_response_item_reads_login_without_totp(login: dict) -> None:
    item = {"id": "item-1", "name": "Login", "type": BitwardenItemType.LOGIN, "login": login}

    listed_item = get_list_response_item_from_bitwarden_item(item)

    assert listed_item.credential.totp == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
async def test_get_login_item_by_id_reads_login_without_totp(monkeypatch: pytest.MonkeyPatch, login: dict) -> None:
    get_json = AsyncMock(return_value={"success": True, "data": {"login": login}})
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)

    credential = await BitwardenService._get_login_item_by_id_using_server("item-1")

    assert credential.totp == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
async def test_get_credential_item_by_id_reads_login_without_totp(monkeypatch: pytest.MonkeyPatch, login: dict) -> None:
    get_json = AsyncMock(
        return_value={
            "success": True,
            "data": {"id": "item-1", "name": "Login", "type": BitwardenItemType.LOGIN, "login": login},
        }
    )
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)

    fetched_item = await BitwardenService._get_credential_item_by_id_using_server("item-1")

    assert fetched_item.credential.totp == ""


class _HangingProcess:
    """A subprocess whose communicate() never returns, so only the deadline can end the call."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 4242
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


@pytest.mark.asyncio
async def test_run_command_names_step_and_elapsed_when_its_own_budget_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _HangingProcess()

    async def fake_exec(*args: object, **kwargs: object) -> _HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    log = MagicMock()
    monkeypatch.setattr(bitwarden_module, "LOG", log)

    with pytest.raises(asyncio.TimeoutError):
        await BitwardenService.run_command(["bw", "list", "items", "--session", "s"], timeout=0.05)

    assert process.killed is True
    kwargs = log.error.call_args.kwargs
    assert log.error.call_args.args[0] == "Bitwarden command timed out"
    assert kwargs["command"] == ["bw", "list"]
    assert kwargs["elapsed_seconds"] >= 0.05


@pytest.mark.asyncio
async def test_run_command_names_step_when_an_enclosing_deadline_cancels_it(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _HangingProcess()

    async def fake_exec(*args: object, **kwargs: object) -> _HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    log = MagicMock()
    monkeypatch.setattr(bitwarden_module, "LOG", log)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await BitwardenService.run_command(["bw", "unlock", "--passwordenv", "BW_PASSWORD"], timeout=10)

    assert process.killed is True
    assert log.warning.call_args.args[0] == "Bitwarden command cancelled by an enclosing deadline"
    assert log.warning.call_args.kwargs["command"] == ["bw", "unlock"]


def test_retry_backoff_is_full_jitter_with_doubling_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float, float]] = []

    def fake_uniform(low: float, high: float) -> float:
        seen.append((low, high))
        return high

    monkeypatch.setattr(bitwarden_module.random, "uniform", fake_uniform)

    delays = [bitwarden_module._retry_backoff_seconds(attempt) for attempt in range(5)]

    assert seen == [(0, 5.0), (0, 10.0), (0, 20.0), (0, 40.0), (0, 60.0)]
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0]


@pytest.mark.asyncio
async def test_every_cli_step_receives_the_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitwarden_module, "_cli_sessions", bitwarden_module._CliSessionCache())
    budgets: dict[str, int] = {}

    async def fake_run_command(command: list[str], additional_env=None, timeout: int = 60) -> RunCommandResult:
        budgets[" ".join(command[0:2])] = timeout
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="abc"', stderr="", returncode=0
            )
        if command[1] == "get":
            item = {"id": "item-1", "login": {"username": "u", "password": "p", "totp": "", "uris": []}}
            return RunCommandResult(stdout=json.dumps(item), stderr="", returncode=0)
        return RunCommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    await BitwardenService._get_secret_value_from_url(
        client_id="cid",
        client_secret="secret",
        master_password="mp",
        bw_organization_id=None,
        bw_collection_ids=["coll-1"],
        url=None,
        collection_id="coll-1",
        item_id="0f3a9a8e-1c6d-4b7e-9f1d-2a5b6c7d8e9f",
        timeout=37,
    )

    # Establishing the session and reading the item all run on the attempt's budget. No `bw logout`:
    # the session is kept for the next run rather than torn down (SKY-14751).
    assert budgets == {
        "bw login": 37,
        "bw unlock": 37,
        "bw sync": 37,
        "bw get": 37,
    }


def _secret_fetch_kwargs() -> dict:
    return {
        "client_id": "cid",
        "client_secret": "secret",
        "master_password": "mp",
        "url": "https://example.com/login",
        "bw_organization_id": None,
        "bw_collection_ids": ["coll-1"],
        "collection_id": "coll-1",
    }


@pytest.mark.asyncio
async def test_secret_fetch_retries_after_a_jittered_backoff_with_a_constant_step_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MAX_JITTER_SECONDS", 0)
    attempt_budgets: list[int] = []
    outcomes: list = [TimeoutError(), {"username": "u", "password": "p", "totp": ""}]

    async def fake_inner(**kwargs) -> dict[str, str]:
        attempt_budgets.append(kwargs["timeout"])
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(BitwardenService, "_get_secret_value_from_url", fake_inner)
    monkeypatch.setattr(bitwarden_module, "_retry_backoff_seconds", lambda attempt: 7.5)
    monkeypatch.setattr(bitwarden_module, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    result = await BitwardenService.get_secret_value_from_url(**_secret_fetch_kwargs(), max_retries=3, timeout=60)

    assert result == {"username": "u", "password": "p", "totp": ""}
    assert attempt_budgets == [60, 60]
    assert slept == [7.5]


@pytest.mark.asyncio
async def test_secret_fetch_gives_up_after_max_retries_with_every_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MAX_JITTER_SECONDS", 0)

    async def always_times_out(**kwargs) -> dict[str, str]:
        raise TimeoutError()

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(BitwardenService, "_get_secret_value_from_url", always_times_out)
    monkeypatch.setattr(bitwarden_module, "_retry_backoff_seconds", lambda attempt: float(attempt))
    monkeypatch.setattr(bitwarden_module, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    with pytest.raises(bitwarden_module.BitwardenListItemsError) as excinfo:
        await BitwardenService.get_secret_value_from_url(**_secret_fetch_kwargs(), max_retries=3, timeout=60)

    assert str(excinfo.value).count("TimeoutError") == 3
    assert slept == [0.0, 1.0]


@pytest.mark.asyncio
async def test_secret_fetch_does_not_retry_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MAX_JITTER_SECONDS", 0)
    calls = 0

    async def denied(**kwargs) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise bitwarden_module.BitwardenAccessDeniedError()

    monkeypatch.setattr(BitwardenService, "_get_secret_value_from_url", denied)

    with pytest.raises(bitwarden_module.BitwardenAccessDeniedError):
        await BitwardenService.get_secret_value_from_url(**_secret_fetch_kwargs(), max_retries=3, timeout=60)

    assert calls == 1


@pytest.mark.asyncio
async def test_identity_fetch_backs_off_before_its_recursive_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MAX_JITTER_SECONDS", 0)
    outcomes: list = [TimeoutError(), {"first_name": "A"}]

    async def fake_inner(**kwargs) -> dict[str, str]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(BitwardenService, "_get_sensitive_information_from_identity", fake_inner)
    monkeypatch.setattr(bitwarden_module, "_retry_backoff_seconds", lambda attempt: 3.0 + attempt)
    monkeypatch.setattr(bitwarden_module, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    result = await BitwardenService.get_sensitive_information_from_identity(
        client_id="cid",
        client_secret="secret",
        master_password="mp",
        bw_organization_id=None,
        bw_collection_ids=["coll-1"],
        collection_id="coll-1",
        identity_key="identity",
        identity_fields=["first_name"],
        remaining_retries=2,
        timeout=60,
    )

    assert result == {"first_name": "A"}
    assert slept == [3.0]
