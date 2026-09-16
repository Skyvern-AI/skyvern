import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import BitwardenGetItemError, BitwardenUnlockError
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


def _cli_item(item_type: BitwardenItemType = BitwardenItemType.LOGIN) -> dict:
    item = {
        "object": "item",
        "id": "11111111-1111-1111-1111-111111111111",
        "type": int(item_type),
        "organizationId": None,
        "collectionIds": [],
        "folderId": None,
        "name": "Example item",
        "notes": None,
        "favorite": False,
        "fields": None,
        "reprompt": 0,
        "revisionDate": "2026-01-01T00:00:00.000Z",
        "creationDate": "2026-01-01T00:00:00.000Z",
        "deletedDate": None,
    }
    if item_type == BitwardenItemType.LOGIN:
        item["login"] = {
            "username": "user@example.test",
            "password": "example-password",
            "totp": None,
            "uris": [{"match": None, "uri": "https://example.test/login"}],
        }
    else:
        item["card"] = {
            "cardholderName": "Example Holder",
            "number": "4111111111111111",
            "expMonth": "12",
            "expYear": "2030",
            "code": None,
            "brand": "Visa",
        }
    return item


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


@pytest.mark.parametrize("item_type", [BitwardenItemType.LOGIN, BitwardenItemType.CREDIT_CARD])
@pytest.mark.parametrize(
    ("stderr", "stderr_class"),
    [
        ("", None),
        ("Event post failed.\n", "event_post_failed"),
        ("Event post failed.\nEvent post failed.\n", "event_post_failed"),
    ],
    ids=["clean", "event-upload", "event-upload-repeated"],
)
def test_get_item_keeps_valid_credentials_with_safe_advisory_telemetry(
    item_type: BitwardenItemType, stderr: str, stderr_class: str | None
) -> None:
    item = _cli_item(item_type)
    result = RunCommandResult(stdout=json.dumps(item), stderr=stderr, returncode=0)

    with capture_logs() as logs:
        assert BitwardenService._parse_fetched_item(result, item["id"]) == item

    if stderr_class is None:
        assert not logs
    else:
        assert len(logs) == 1
        # Accepted-advisory telemetry stays bounded: a classification only, never raw stderr or the item id.
        assert logs[0] == {
            "event": "Bitwarden CLI reported advisory stderr while fetching an item",
            "log_level": "warning",
            "command_kind": "get_item",
            "stderr_class": stderr_class,
            "returncode": 0,
            "item_valid": True,
        }


def test_get_item_accepts_uppercase_uuid_request_matching_lowercase_canonical_id() -> None:
    # is_uuid() accepts an uppercase UUID, but the CLI lowercases the request and returns the
    # canonical lowercase id, so identity must compare by canonical UUID, not exact string.
    item = _cli_item()
    item["id"] = "479d0fec-6ab3-4f74-a92e-b4c2007704d3"
    requested_id = "479D0FEC-6AB3-4F74-A92E-B4C2007704D3"
    result = RunCommandResult(stdout=json.dumps(item), stderr="Event post failed.\n", returncode=0)

    with capture_logs() as logs:
        assert BitwardenService._parse_fetched_item(result, requested_id) == item
    assert len(logs) == 1 and logs[0]["stderr_class"] == "event_post_failed"


def test_get_item_rejects_valid_item_with_unknown_stderr() -> None:
    # A valid item does not license an unknown stderr line; it stays fatal and keeps the CLI's
    # diagnostic so `_repair_for_failure` can still classify it (here a stale-vault marker).
    item = _cli_item()
    result = RunCommandResult(stdout=json.dumps(item), stderr="Not found.\n", returncode=0)

    with capture_logs() as logs:
        with pytest.raises(BitwardenGetItemError, match="Not found."):
            BitwardenService._parse_fetched_item(result, item["id"])
    assert not logs


def test_get_item_rejects_exact_advisory_mixed_with_unknown_line() -> None:
    # The exact advisory line does not whitelist the whole stderr; a mixed unknown line stays fatal
    # and its repair-relevant diagnostic is retained.
    item = _cli_item()
    result = RunCommandResult(
        stdout=json.dumps(item), stderr="Event post failed.\nYou are not logged in.\n", returncode=0
    )

    with capture_logs() as logs:
        with pytest.raises(BitwardenGetItemError, match="You are not logged in."):
            BitwardenService._parse_fetched_item(result, item["id"])
    assert not logs


@pytest.mark.parametrize("stderr", ["You are not logged in.\n", "Not found.\n"], ids=["relogin", "resync"])
def test_get_item_preserves_repair_stderr_over_shape_error(stderr: str) -> None:
    # An exit-zero error/status envelope with repairable stderr must keep the CLI diagnostic so
    # `_repair_for_failure` can still pick RELOGIN/RESYNC — not be masked by a static shape error.
    result = RunCommandResult(stdout=json.dumps({"error": "not an item"}), stderr=stderr, returncode=0)

    with capture_logs() as logs:
        with pytest.raises(BitwardenGetItemError, match=stderr.strip()):
            BitwardenService._parse_fetched_item(result, _cli_item()["id"])
    assert not logs


def test_get_item_defers_exact_advisory_until_after_item_validation() -> None:
    # A malformed item carrying only the exact advisory must fail as a shape/envelope error and must
    # not be accepted or logged as a valid item — advisory acceptance is deferred until validation passes.
    result = RunCommandResult(stdout=json.dumps({"object": "not-item"}), stderr="Event post failed.\n", returncode=0)

    with capture_logs() as logs:
        with pytest.raises(BitwardenGetItemError):
            BitwardenService._parse_fetched_item(result, _cli_item()["id"])
    assert not logs


def test_get_item_still_raises_and_keeps_the_clis_message_on_a_nonzero_exit() -> None:
    result = RunCommandResult(stdout="", stderr="Not found.\n", returncode=1)

    with pytest.raises(BitwardenGetItemError, match="Not found."):
        BitwardenService._parse_fetched_item(result, "item-1")


def test_get_item_stays_fatal_when_a_field_failed_to_decrypt_despite_exit_zero() -> None:
    item = _cli_item()
    item["login"]["password"] = "[error: cannot decrypt]"
    result = RunCommandResult(
        stdout=json.dumps(item),
        stderr="[EncString Generic Decrypt] failed to decrypt encstring\n",
        returncode=0,
    )

    with pytest.raises(BitwardenGetItemError, match="failed to decrypt"):
        BitwardenService._parse_fetched_item(result, item["id"])


@pytest.mark.parametrize("nested", [False, True], ids=["password", "nested-value"])
@pytest.mark.parametrize("escaped", [False, True], ids=["literal", "json-escaped"])
def test_get_item_rejects_stdout_only_decrypt_poison(nested: bool, escaped: bool) -> None:
    item = _cli_item()
    if nested:
        item["fields"] = [{"name": "extra", "value": {"nested": ["prefix [error: cannot decrypt] suffix"]}}]
    else:
        item["login"]["password"] = "[error: cannot decrypt]"
    stdout = json.dumps(item)
    if escaped:
        stdout = stdout.replace("[error: cannot decrypt]", r"\u005berror: cannot decrypt\u005d")

    with pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(RunCommandResult(stdout=stdout, stderr="", returncode=0), item["id"])


def test_get_item_rejects_stderr_only_decrypt_failure() -> None:
    item = _cli_item()
    result = RunCommandResult(
        stdout=json.dumps(item), stderr="[EncString Generic Decrypt] FAILED TO DECRYPT encstring\n", returncode=0
    )

    with pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(result, item["id"])


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "{",
        "null",
        "{}",
        "[]",
        '[{"object":"item"}]',
        '"item"',
        "1",
        "true",
        '{"error":"Not found."}',
        '{"response":null,"statusCode":500}',
        '{"success":false,"message":"Not found."}',
        json.dumps({"success": True, "data": _cli_item()}),
        '{"serverUrl":null,"lastSync":null,"status":"unlocked"}',
    ],
    ids=[
        "empty",
        "invalid-json",
        "null",
        "empty-object",
        "empty-list",
        "item-list",
        "string",
        "number",
        "bool",
        "error",
        "http-response",
        "failure-response",
        "success-response",
        "status",
    ],
)
def test_get_item_rejects_non_item_envelopes_before_logging_advisories(stdout: str) -> None:
    result = RunCommandResult(stdout=stdout, stderr="Event post failed.\n", returncode=0)

    with capture_logs() as logs, pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(result, _cli_item()["id"])

    assert not logs


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", "22222222-2222-2222-2222-222222222222"),
        ("id", None),
        ("object", "response"),
        ("object", None),
        ("type", True),
        ("type", "1"),
        ("type", 1.0),
        ("type", 2),
        ("type", 4),
        ("type", None),
        ("login", None),
        ("login", []),
        ("login", "login"),
        ("login", {}),
        ("login", {"uris": []}),
        ("login", {"username": ["user"]}),
        ("login", {"password": {"value": "pw"}}),
        ("login", {"totp": 123}),
        ("login", {"password": False}),
    ],
)
def test_get_item_rejects_invalid_identity_type_and_login_shape(key: str, value: object) -> None:
    item = _cli_item()
    requested_id = item["id"]
    item[key] = value
    result = RunCommandResult(stdout=json.dumps(item), stderr="Event post failed.\n", returncode=0)

    with capture_logs() as logs, pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(result, requested_id)

    assert not logs


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("card", None),
        ("card", []),
        ("card", "card"),
        ("card", {}),
        ("fields", "fields"),
        ("fields", {}),
        ("fields", [None]),
        ("fields", ["field"]),
        ("fields", [{"name": ["billing_email"], "value": "value"}]),
        ("fields", [{"name": "billing_email", "value": 123}]),
    ],
)
def test_get_item_rejects_malformed_card_payload_and_custom_fields(key: str, value: object) -> None:
    item = _cli_item(BitwardenItemType.CREDIT_CARD)
    item[key] = value
    result = RunCommandResult(stdout=json.dumps(item), stderr="Event post failed.\n", returncode=0)

    with capture_logs() as logs, pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(result, item["id"])

    assert not logs


@pytest.mark.parametrize("field", ["cardholderName", "number", "expMonth", "expYear", "code", "brand"])
@pytest.mark.parametrize("missing", [False, True], ids=["non-string", "missing"])
def test_get_item_rejects_invalid_consumed_card_fields(field: str, missing: bool) -> None:
    item = _cli_item(BitwardenItemType.CREDIT_CARD)
    if missing:
        del item["card"][field]
    else:
        item["card"][field] = 123

    with pytest.raises(BitwardenGetItemError):
        BitwardenService._parse_fetched_item(
            RunCommandResult(stdout=json.dumps(item), stderr="", returncode=0), item["id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_mode", [None, "true"], ids=["parent-only", "explicit-override"])
async def test_run_command_scrubs_inherited_cli_output_modes(
    monkeypatch: pytest.MonkeyPatch, explicit_mode: str | None
) -> None:
    # Every inherited BW_* var is scrubbed fail-closed — including BW_RAW (which makes `bw unlock`
    # print the raw vault session key on stdout) and BW_SESSION — so only additional_env can re-supply
    # a BW_* var to the child.
    flags = ["BW_CLEANEXIT", "BW_RESPONSE", "BW_QUIET", "BW_PRETTY", "BW_RAW", "BW_SESSION"]
    for flag in flags:
        monkeypatch.setenv(flag, "true")
    monkeypatch.setenv("BW_TEST_PARENT", "parent-value")
    monkeypatch.setenv("KEEP_PARENT", "keep-value")
    additional_env = {"BW_TEST_EXPLICIT": "command-value"}
    if explicit_mode:
        additional_env["BW_PRETTY"] = explicit_mode
    keys = [*flags, "BW_TEST_PARENT", "KEEP_PARENT", "BW_TEST_EXPLICIT", "NODE_NO_WARNINGS"]
    script = f"import json, os; print(json.dumps({{key: os.environ.get(key) for key in {keys!r}}}))"

    result = await BitwardenService.run_command([sys.executable, "-c", script], additional_env=additional_env)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        **dict.fromkeys(flags),  # every inherited BW_* scrubbed to None...
        "BW_PRETTY": explicit_mode,  # ...unless additional_env re-supplies it (additional_env wins)
        "BW_TEST_PARENT": None,  # an inherited BW_* is scrubbed even if it is not a known flag
        "KEEP_PARENT": "keep-value",  # non-BW parent env still passes through
        "BW_TEST_EXPLICIT": "command-value",
        "NODE_NO_WARNINGS": "1",
    }
    assert all(os.environ[flag] == "true" for flag in flags)


@pytest.mark.asyncio
async def test_unlock_never_echoes_raw_session_key_in_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # With an inherited BW_RAW, `bw unlock` prints the bare session key (the vault decryption key) on
    # stdout with no banner. The unlock failure path must never interpolate that stdout into an error.
    raw_session_key = "cV3rYS3cr3tS3ss10nK3yAAAABBBBCCCC=="

    async def fake_run_command(command: list[str], additional_env: dict | None = None, timeout: int = 60):
        return RunCommandResult(stdout=raw_session_key, stderr="mac failed", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    with pytest.raises(BitwardenUnlockError) as excinfo:
        await BitwardenService.unlock("master-password")
    assert raw_session_key not in str(excinfo.value)


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
            item = _cli_item()
            item["id"] = command[3]
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
