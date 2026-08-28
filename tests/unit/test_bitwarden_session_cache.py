"""Tests for SKY-14751: a Bitwarden CLI session is reused across runs instead of rebuilt per run.

Every run used to pay `bw logout`, `login`, `sync`, `unlock` and then a whole-collection
`bw list items --search <domain>` before it saw a single credential. A scheduled batch of 100+ runs
therefore performed 100+ logins, syncs and collection decrypts within a couple of minutes, all
serialized behind one process-wide mutex, and the runs at the back of that queue burned their entire
timeout budget waiting rather than working.

Now each vault identity keeps one logged-in, unlocked CLI session in the process, pinned to its own
`BITWARDENCLI_APPDATA_DIR`, so a warm run costs one `bw get item`. These tests pin the properties
that makes safe: one login per identity, isolation between organizations, and a forced resync
before an item miss is believed.
"""

import asyncio
import json
import os
from collections.abc import Iterator

import pytest

from skyvern.forge.sdk.services import bitwarden as bitwarden_module
from skyvern.forge.sdk.services.bitwarden import (
    BitwardenConstants,
    BitwardenService,
    RunCommandResult,
)

APPDATA_ENV = bitwarden_module._BITWARDEN_APPDATA_ENV_VAR
ITEM_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ITEM_ID = "22222222-2222-2222-2222-222222222222"
CARD_ITEM = {
    "id": ITEM_ID,
    "type": 3,
    "organizationId": "org-id",
    "collectionIds": ["collection-id"],
    "card": {
        "cardholderName": "Jane Doe",
        "number": "4111111111111111",
        "expMonth": "12",
        "expYear": "2030",
        "code": "123",
        "brand": "visa",
    },
    "fields": [],
}


@pytest.fixture(autouse=True)
def _isolated_session_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # A fresh cache per test, and no jitter or ambient vault credentials to make identities drift.
    monkeypatch.setattr(bitwarden_module, "_cli_sessions", bitwarden_module._CliSessionCache())
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MAX_JITTER_SECONDS", 0)
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_EMAIL", None)
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_MASTER_PASSWORD", None)
    yield


class FakeVaultCli:
    """Stands in for the `bw` binary, recording every step and the data directory it ran against."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.item: dict = {
            "id": ITEM_ID,
            "type": 1,
            "login": {"username": "alice@example.com", "password": "hunter2", "totp": ""},
            "fields": [{"name": "first_name", "value": "Alice"}],
        }
        # stderr to answer the next `bw get` / `bw list` calls with, one per entry, before succeeding.
        self.get_failures: list[str] = []
        self.list_failures: list[str] = []

    @property
    def steps(self) -> list[str]:
        return [step for step, _ in self.calls]

    def data_dirs_for(self, step: str) -> list[str | None]:
        return [appdata_dir for recorded, appdata_dir in self.calls if recorded == step]

    async def run_command(
        self, command: list[str], additional_env: dict[str, str] | None = None, timeout: float = 60
    ) -> RunCommandResult:
        self.calls.append((command[1], (additional_env or {}).get(APPDATA_ENV)))
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="session-key"', stderr="", returncode=0
            )
        if command[1] == "get":
            if self.get_failures:
                return RunCommandResult(stdout="", stderr=self.get_failures.pop(0), returncode=1)
            return RunCommandResult(stdout=json.dumps(self.item), stderr="", returncode=0)
        if command[1] == "list":
            if self.list_failures:
                # How the CLI reports a dead session: non-zero, nothing on stdout, reason on stderr.
                return RunCommandResult(stdout="", stderr=self.list_failures.pop(0), returncode=1)
            return RunCommandResult(stdout=json.dumps([self.item]), stderr="", returncode=0)
        return RunCommandResult(stdout="", stderr="", returncode=0)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeVaultCli:
    fake = FakeVaultCli()
    monkeypatch.setattr(BitwardenService, "run_command", fake.run_command)
    return fake


def _identity_of(master_password: str = "master-password") -> "bitwarden_module._VaultIdentity":
    """The identity `_fetch` resolves to, so tests look sessions up the way production keys them."""
    return bitwarden_module._VaultIdentity.resolve(
        client_id="client-id", client_secret="client-secret", email=None, master_password=master_password
    )


async def _fetch(item_id: str = ITEM_ID, master_password: str = "master-password") -> dict[str, str]:
    return await BitwardenService.get_secret_value_from_url(
        client_id="client-id",
        client_secret="client-secret",
        master_password=master_password,
        bw_organization_id="org-id",
        bw_collection_ids=None,
        item_id=item_id,
    )


@pytest.mark.asyncio
async def test_a_batch_of_runs_for_one_organization_pays_a_single_login(cli: FakeVaultCli) -> None:
    """AC: 100 runs against one organization inside three minutes make <=1 identity call per run."""
    results = await asyncio.gather(*(_fetch() for _ in range(100)))

    assert all(result[BitwardenConstants.PASSWORD] == "hunter2" for result in results)
    assert cli.steps.count("login") == 1
    assert cli.steps.count("unlock") == 1
    assert cli.steps.count("sync") == 1
    # Only the per-item decrypt is paid per run, and nothing logs the batch back out mid-flight.
    assert cli.steps.count("get") == 100
    assert "logout" not in cli.steps


@pytest.mark.asyncio
async def test_a_warm_run_only_decrypts_the_one_item_it_names(cli: FakeVaultCli) -> None:
    await _fetch()
    cli.calls.clear()

    await _fetch()

    assert cli.steps == ["get"]


@pytest.mark.asyncio
async def test_two_organizations_keep_separate_sessions_and_data_directories(cli: FakeVaultCli) -> None:
    await _fetch(master_password="first-organization")
    await _fetch(master_password="second-organization")
    cli.calls.clear()

    # Coming back to the first organization must not cost another login, which is what a shared
    # CLI data directory would have forced.
    await _fetch(master_password="first-organization")

    assert cli.steps == ["get"]
    first_dir = cli.data_dirs_for("get")[0]
    assert first_dir is not None


@pytest.mark.asyncio
async def test_each_identity_logs_in_against_its_own_data_directory(cli: FakeVaultCli) -> None:
    await _fetch(master_password="first-organization")
    await _fetch(master_password="second-organization")

    login_dirs = cli.data_dirs_for("login")
    assert len(login_dirs) == 2
    assert login_dirs[0] != login_dirs[1]
    assert all(directory is not None for directory in login_dirs)


@pytest.mark.asyncio
async def test_an_item_missing_from_the_cached_vault_forces_one_resync(cli: FakeVaultCli) -> None:
    """A credential created seconds ago is absent from a cached vault until it is synced."""
    await _fetch()
    cli.calls.clear()
    cli.get_failures = ["Not found."]

    result = await _fetch()

    assert result[BitwardenConstants.PASSWORD] == "hunter2"
    assert cli.steps == ["get", "sync", "get"]
    assert cli.steps.count("login") == 0


@pytest.mark.asyncio
async def test_an_expired_session_is_re_established_before_the_run_fails(cli: FakeVaultCli) -> None:
    await _fetch()
    cli.calls.clear()
    cli.get_failures = ["You are not logged in."]

    result = await _fetch()

    assert result[BitwardenConstants.PASSWORD] == "hunter2"
    assert cli.steps == ["get", "login", "unlock", "sync", "get"]


@pytest.mark.asyncio
async def test_a_failure_that_is_not_about_the_session_is_not_retried(cli: FakeVaultCli) -> None:
    await _fetch()
    cli.calls.clear()
    cli.get_failures = ["Something went wrong.", "Something went wrong."]

    with pytest.raises(bitwarden_module.BitwardenListItemsError):
        await BitwardenService.get_secret_value_from_url(
            client_id="client-id",
            client_secret="client-secret",
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            item_id=ITEM_ID,
            max_retries=1,
        )

    assert cli.steps == ["get"]


@pytest.mark.asyncio
async def test_the_cached_vault_is_not_resynced_inside_the_sync_interval(
    cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_SESSION_SYNC_INTERVAL_SECONDS", 3600)
    await _fetch()
    cli.calls.clear()

    await _fetch()

    assert "sync" not in cli.steps


@pytest.mark.asyncio
async def test_the_cached_vault_is_resynced_once_the_interval_lapses(
    cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_SESSION_SYNC_INTERVAL_SECONDS", 0)
    await _fetch()
    cli.calls.clear()

    await _fetch()

    assert cli.steps == ["sync", "get"]


@pytest.mark.asyncio
async def test_a_failed_mandatory_refresh_is_reported_rather_than_answered_from_cache(
    cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing missing an item someone just added looks exactly like a listing that is right."""

    async def list_overviews() -> list:
        return await BitwardenService.list_item_overviews(
            client_id=None,
            client_secret=None,
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            email="someone@example.com",
        )

    await list_overviews()

    async def failing_sync(**_: object) -> None:
        raise bitwarden_module.BitwardenSyncError("upstream is down")

    monkeypatch.setattr(BitwardenService, "sync", failing_sync)

    with pytest.raises(bitwarden_module.BitwardenSyncError):
        await list_overviews()


@pytest.mark.asyncio
async def test_a_failed_refresh_still_reads_the_cached_vault(
    cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync that fails is not worth failing a run over when the cached vault still answers."""
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_SESSION_SYNC_INTERVAL_SECONDS", 0)
    await _fetch()

    async def failing_sync(**_: object) -> None:
        raise bitwarden_module.BitwardenSyncError("upstream is down")

    monkeypatch.setattr(BitwardenService, "sync", failing_sync)
    cli.calls.clear()

    result = await _fetch()

    assert result[BitwardenConstants.PASSWORD] == "hunter2"
    assert cli.steps == ["get"]


@pytest.mark.asyncio
async def test_a_login_that_fails_leaves_no_half_built_session_behind(
    cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    logins = 0

    async def failing_login(*_: object, **__: object) -> None:
        nonlocal logins
        logins += 1
        raise bitwarden_module.BitwardenLoginError("new device verification required")

    monkeypatch.setattr(BitwardenService, "login", failing_login)

    with pytest.raises(bitwarden_module.BitwardenListItemsError):
        await BitwardenService.get_secret_value_from_url(
            client_id="client-id",
            client_secret="client-secret",
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            item_id=ITEM_ID,
            max_retries=2,
        )

    # A cached entry that never finished logging in must not be handed to the next run as if it had.
    assert logins == 2


@pytest.mark.asyncio
async def test_fetches_for_different_organizations_do_not_serialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate data directories mean one organization's slow fetch no longer blocks another's."""
    first_get_entered = asyncio.Event()
    release_first_get = asyncio.Event()
    second_get_entered = asyncio.Event()
    order: list[str] = []

    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: float = 60
    ) -> RunCommandResult:
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="session-key"', stderr="", returncode=0
            )
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "get":
            if not order:
                order.append("first")
                first_get_entered.set()
                await release_first_get.wait()
            else:
                order.append("second")
                second_get_entered.set()
            return RunCommandResult(
                stdout=json.dumps({"id": ITEM_ID, "login": {"username": "u", "password": "p", "totp": ""}}),
                stderr="",
                returncode=0,
            )
        return RunCommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", run_command)

    first = asyncio.create_task(_fetch(master_password="first-organization"))
    await first_get_entered.wait()
    second = asyncio.create_task(_fetch(master_password="second-organization"))

    # The second organization gets all the way into its own `bw get` while the first is still stuck.
    await asyncio.wait_for(second_get_entered.wait(), timeout=1)

    release_first_get.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_listing_two_vaults_does_not_serialize_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-wide CLI mutex is gone; separate data directories are what make that safe."""
    first_list_entered = asyncio.Event()
    release_first_list = asyncio.Event()
    second_list_entered = asyncio.Event()
    order: list[str] = []

    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: float = 60
    ) -> RunCommandResult:
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="session-key"', stderr="", returncode=0
            )
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "list":
            if not order:
                order.append("first")
                first_list_entered.set()
                await release_first_list.wait()
            else:
                order.append("second")
                second_list_entered.set()
            return RunCommandResult(stdout="[]", stderr="", returncode=0)
        return RunCommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", run_command)

    async def list_for(email: str) -> list:
        return await BitwardenService.list_item_overviews(
            client_id=None,
            client_secret=None,
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            email=email,
            timeout=5,
        )

    first = asyncio.create_task(list_for("first@example.com"))
    await first_list_entered.wait()
    second = asyncio.create_task(list_for("second@example.com"))

    await asyncio.wait_for(second_list_entered.wait(), timeout=1)

    release_first_list.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_browsing_a_vault_always_syncs_but_reuses_the_login(cli: FakeVaultCli) -> None:
    """Someone who just added an item expects to see it, so the interactive path still syncs."""

    async def list_overviews() -> list:
        return await BitwardenService.list_item_overviews(
            client_id=None,
            client_secret=None,
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            email="someone@example.com",
        )

    await list_overviews()
    cli.calls.clear()

    await list_overviews()

    assert cli.steps == ["sync", "list"]


@pytest.mark.asyncio
async def test_concurrent_cli_commands_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitwarden_module, "_cli_command_semaphore", asyncio.Semaphore(2))
    in_flight = 0
    peak = 0

    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: float = 60
    ) -> RunCommandResult:
        nonlocal in_flight, peak
        async with bitwarden_module._cli_command_semaphore:
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="session-key"', stderr="", returncode=0
            )
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "get":
            return RunCommandResult(
                stdout=json.dumps({"id": ITEM_ID, "login": {"username": "u", "password": "p", "totp": ""}}),
                stderr="",
                returncode=0,
            )
        return RunCommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", run_command)

    await asyncio.gather(*(_fetch() for _ in range(20)))

    assert peak <= 2


async def _fetch_by_url(master_password: str = "master-password") -> dict[str, str]:
    return await BitwardenService.get_secret_value_from_url(
        client_id="client-id",
        client_secret="client-secret",
        master_password=master_password,
        bw_organization_id="org-id",
        bw_collection_ids=None,
        url="https://example.com/login",
    )


@pytest.mark.asyncio
async def test_an_expired_session_is_re_established_on_the_url_search_path(cli: FakeVaultCli) -> None:
    """The URL search is the default path, so a dead session must be recoverable from it too."""
    await _fetch_by_url()
    cli.calls.clear()
    cli.list_failures = ["You are not logged in."]

    result = await _fetch_by_url()

    assert result[BitwardenConstants.PASSWORD] == "hunter2"
    assert cli.steps == ["list", "login", "unlock", "sync", "list"]


@pytest.mark.asyncio
async def test_an_expired_session_is_re_established_on_the_identity_path(cli: FakeVaultCli) -> None:
    async def fetch_identity() -> dict[str, str]:
        return await BitwardenService.get_sensitive_information_from_identity(
            client_id="client-id",
            client_secret="client-secret",
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            collection_id="collection-id",
            identity_key="identity",
            identity_fields=["first_name"],
        )

    await fetch_identity()
    cli.calls.clear()
    cli.list_failures = ["You are not logged in."]

    result = await fetch_identity()

    assert result == {"first_name": "Alice"}
    assert cli.steps == ["list", "login", "unlock", "sync", "list"]


@pytest.mark.asyncio
async def test_a_failed_list_reports_the_cli_error_not_a_json_error(cli: FakeVaultCli) -> None:
    """Parsing an empty stdout first would hide the stderr that says what to do about it."""
    await _fetch_by_url()
    cli.list_failures = ["Something the repair logic does not know about."] * 4

    with pytest.raises(bitwarden_module.BitwardenListItemsError) as excinfo:
        await BitwardenService.get_secret_value_from_url(
            client_id="client-id",
            client_secret="client-secret",
            master_password="master-password",
            bw_organization_id="org-id",
            bw_collection_ids=None,
            url="https://example.com/login",
            max_retries=1,
        )

    assert "Something the repair logic does not know about." in str(excinfo.value)


@pytest.mark.asyncio
async def test_retiring_a_session_cannot_delete_its_replacements_data(cli: FakeVaultCli) -> None:
    """A session torn down while another request still held it must not outlive its own generation."""
    identity = _identity_of()
    first = await bitwarden_module._cli_sessions.checkout(identity)
    await bitwarden_module._cli_sessions.discard(first)

    second = await bitwarden_module._cli_sessions.checkout(identity)
    assert second is not first
    assert second.appdata_dir != first.appdata_dir

    # A late teardown arriving from the dead generation must leave the live one untouched.
    await bitwarden_module._cli_sessions.discard(first)

    assert os.path.isdir(second.appdata_dir)


@pytest.mark.asyncio
async def test_a_sessions_data_survives_until_its_last_reader_finishes(cli: FakeVaultCli) -> None:
    identity = _identity_of()
    session = await bitwarden_module._cli_sessions.checkout(identity)

    async with session.in_use():
        await bitwarden_module._cli_sessions.discard(session)
        # A command still running against this directory keeps it alive.
        assert os.path.isdir(session.appdata_dir)

    assert not os.path.isdir(session.appdata_dir)


@pytest.mark.asyncio
async def test_a_batch_hitting_a_dead_session_all_recovers(cli: FakeVaultCli) -> None:
    """Concurrent expirations must not cascade into failures across the rest of the batch."""
    await _fetch()
    cli.calls.clear()
    cli.get_failures = ["You are not logged in."] * 8

    results = await asyncio.gather(*(_fetch() for _ in range(8)))

    assert all(result[BitwardenConstants.PASSWORD] == "hunter2" for result in results)


async def _fetch_card(item_id: str = ITEM_ID) -> dict[str, str]:
    return await BitwardenService.get_credit_card_data(
        client_id="client-id",
        client_secret="client-secret",
        master_password="master-password",
        bw_organization_id="org-id",
        bw_collection_ids=["collection-id"],
        collection_id="collection-id",
        item_id=item_id,
    )


@pytest.mark.asyncio
async def test_an_expired_session_is_re_established_on_the_credit_card_path(cli: FakeVaultCli) -> None:
    cli.item = CARD_ITEM
    await _fetch_card()
    cli.calls.clear()
    cli.get_failures = ["You are not logged in."]

    result = await _fetch_card()

    assert result[BitwardenConstants.CREDIT_CARD_NUMBER] == "4111111111111111"
    assert cli.steps == ["get", "login", "unlock", "sync", "get"]


@pytest.mark.asyncio
async def test_a_card_missing_from_the_cached_vault_forces_one_resync(cli: FakeVaultCli) -> None:
    cli.item = CARD_ITEM
    await _fetch_card()
    cli.calls.clear()
    cli.get_failures = ["Not found."]

    result = await _fetch_card()

    assert result[BitwardenConstants.CREDIT_CARD_NUMBER] == "4111111111111111"
    assert cli.steps == ["get", "sync", "get"]


@pytest.mark.asyncio
async def test_a_saturated_cache_does_not_evict_the_session_it_just_handed_out(cli: FakeVaultCli) -> None:
    """Every older session being busy must not make the newest one the only eviction candidate."""
    cache = bitwarden_module._cli_sessions
    capacity = bitwarden_module.settings.BITWARDEN_SESSION_CACHE_SIZE
    identities = [
        bitwarden_module._VaultIdentity.resolve(None, None, f"org{index}@example.com", "master-password")
        for index in range(capacity + 1)
    ]

    held = []
    for identity in identities[:capacity]:
        busy = await cache.checkout(identity)
        reader = busy.in_use()
        await reader.__aenter__()
        held.append(reader)

    newest = await cache.checkout(identities[capacity])

    assert newest.retired is False
    assert cache._sessions.get(identities[capacity].fingerprint) is newest
    assert os.path.isdir(newest.appdata_dir)

    for reader in held:
        await reader.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_a_session_left_idle_is_logged_out(cli: FakeVaultCli, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unlocked vault should not sit in memory just because nothing has needed it lately."""
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_SESSION_MAX_IDLE_SECONDS", 300)
    await _fetch()
    cache = bitwarden_module._cli_sessions
    identity = _identity_of()
    idle = cache._sessions[identity.fingerprint]
    idle.last_used_at -= 301

    # Any later checkout is what notices; a different vault's run is enough.
    await _fetch(master_password="another-organization")

    assert idle.retired is True
    assert cache._sessions.get(identity.fingerprint) is not idle
    assert not os.path.isdir(idle.appdata_dir)


@pytest.mark.asyncio
async def test_a_session_still_in_use_is_never_reclaimed_for_being_idle(cli: FakeVaultCli, monkeypatch) -> None:
    monkeypatch.setattr(bitwarden_module.settings, "BITWARDEN_SESSION_MAX_IDLE_SECONDS", 300)
    await _fetch()
    cache = bitwarden_module._cli_sessions
    identity = _identity_of()
    session = cache._sessions[identity.fingerprint]

    async with session.in_use():
        session.last_used_at -= 301
        await _fetch(master_password="another-organization")
        assert session.retired is False
        assert os.path.isdir(session.appdata_dir)


@pytest.mark.asyncio
async def test_a_burst_across_more_vaults_than_the_cache_holds_drains_back_to_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkout alone cannot bring this back: at that moment every other session is in flight."""
    capacity = bitwarden_module.settings.BITWARDEN_SESSION_CACHE_SIZE
    vaults = capacity * 2
    # Hold every fetch at its read until all of them have got there, so each one checks out while
    # every other session is busy and therefore unreclaimable.
    all_reading = asyncio.Event()
    reading = 0

    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: float = 60
    ) -> RunCommandResult:
        nonlocal reading
        if command[1] == "login":
            return RunCommandResult(stdout="You are logged in!", stderr="", returncode=0)
        if command[1] == "unlock":
            return RunCommandResult(
                stdout='Your vault is now unlocked!\n$ export BW_SESSION="session-key"', stderr="", returncode=0
            )
        if command[1] == "get":
            reading += 1
            if reading == vaults:
                all_reading.set()
            await all_reading.wait()
            return RunCommandResult(
                stdout=json.dumps({"id": ITEM_ID, "login": {"username": "u", "password": "p", "totp": ""}}),
                stderr="",
                returncode=0,
            )
        return RunCommandResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", run_command)
    monkeypatch.setattr(bitwarden_module, "_cli_command_semaphore", asyncio.Semaphore(vaults))

    await asyncio.gather(*(_fetch(master_password=f"organization-{index}") for index in range(vaults)))

    assert len(bitwarden_module._cli_sessions._sessions) <= capacity


def test_the_identity_fingerprint_is_salted_per_process_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A log reader who knows the email must not be able to confirm password guesses against it."""
    email, master_password = "user@example.com", "hunter2"
    monkeypatch.setattr(bitwarden_module, "_IDENTITY_FINGERPRINT_SALT", b"a" * 32)
    first_process = bitwarden_module._VaultIdentity.resolve(
        client_id=None, client_secret=None, email=email, master_password=master_password
    )
    fingerprint = first_process.fingerprint

    monkeypatch.setattr(bitwarden_module, "_IDENTITY_FINGERPRINT_SALT", b"b" * 32)
    second_process = bitwarden_module._VaultIdentity.resolve(
        client_id=None, client_secret=None, email=email, master_password=master_password
    )

    assert first_process.fingerprint == fingerprint
    assert second_process.fingerprint != fingerprint


def test_an_identity_never_reveals_its_secrets_when_logged() -> None:
    identity = _identity_of()

    rendered = f"{identity!r}"

    assert "master-password" not in rendered
    assert "client-secret" not in rendered
    assert identity.fingerprint[:12] in rendered
