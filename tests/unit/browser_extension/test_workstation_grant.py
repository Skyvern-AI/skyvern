from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.broker_server import BrowserExtensionBrokerServer
from skyvern.browser_extension.broker_state import ensure_run_directory
from skyvern.browser_extension.errors import BrowserExtensionBrokerError
from skyvern.browser_extension.workstation_grant import (
    load_workstation_grant,
    remove_workstation_grant,
    validate_workstation_grant,
    workstation_grant_path,
    write_workstation_grant,
)

from .test_broker_server import FakeRelay, _connect_over_socketpair, _ignore_event


@pytest.mark.parametrize("source", ["pairing", "cli"])
def test_workstation_grant_roundtrip_and_token_binding(tmp_path: Path, source: str) -> None:
    path = workstation_grant_path(tmp_path / ".skyvern")

    grant = write_workstation_grant(path, "token-a", source=source, granted_at=123.5)

    assert load_workstation_grant(path, "token-a") == grant
    assert load_workstation_grant(path, "rotated-token") is None
    path.chmod(0o644)
    assert load_workstation_grant(path, "token-a") == grant
    assert path.stat().st_mode & 0o777 == 0o600
    assert remove_workstation_grant(path) is True
    assert remove_workstation_grant(path) is False


def test_empty_expected_token_invalidates_workstation_grant(tmp_path: Path) -> None:
    path = workstation_grant_path(tmp_path / ".skyvern")
    grant = write_workstation_grant(path, "token-a", source="cli", granted_at=123.5)

    assert validate_workstation_grant(grant, "") is False
    assert load_workstation_grant(path, "") is None


@pytest.mark.asyncio
async def test_authentication_with_valid_workstation_grant_skips_pairing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    write_workstation_grant(workstation_grant_path(), token, source="cli")
    server = BrowserExtensionBrokerServer(19777)
    server._broker_auth_token = token
    server._relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client, auto_approve=False)
    try:
        assert (await client.broker_status())["approved"] is True
        assert server._pairing_owner is None
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_interactive_pairing_refreshes_workstation_grant_and_approves_peers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client, auto_approve=False)
    peer = BrokerClient(19777, _ignore_event, auto_spawn=False)
    peer_task = await _connect_over_socketpair(server, peer, auto_approve=False)
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        grant = load_workstation_grant(workstation_grant_path(), token)
        assert grant is not None
        assert grant.source == "pairing"
        assert (await client.broker_status())["approved"] is True
        assert await peer.wait_connected(1.0) is True
        assert (await peer.broker_status())["approved"] is True
        assert server._credentials[peer._client_id].approval_source == "grant"
    finally:
        await peer.stop()
        await asyncio.wait_for(peer_task, 1.0)
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_interactive_pairing_ack_survives_grant_persist_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    server_task = await _connect_over_socketpair(server, client, auto_approve=False)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("skyvern.browser_extension.broker_server.write_workstation_grant", fail_write)
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert (await client.broker_status())["approved"] is True
        assert server._pairing_owner is None
        assert relay.sent_events[-1] == (
            "pairing.approved_ack",
            {"approvalNonce": offer["approvalNonce"], "approved": True},
        )
        assert not workstation_grant_path().exists()
    finally:
        await client.stop()
        await asyncio.wait_for(server_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_workstation_control_ops_are_operator_only_and_revoke_future_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    try:
        with pytest.raises(BrowserExtensionBrokerError) as error_info:
            await client.grant_workstation()
        assert error_info.value.code == "OP_NOT_ALLOWED"
        await client.begin_pairing()
        assert server._pairing_owner == client._client_id

        assert (await operator.grant_workstation())["granted"] is True
        assert load_workstation_grant(workstation_grant_path(), token) is not None
        assert (await client.broker_status())["approved"] is True

        assert (await operator.revoke_workstation())["revoked"] is True
        assert server._pairing_owner is None
        assert relay.nonce == "cancelled"
        assert not workstation_grant_path().exists()
        # Grant-approved connections lose approval immediately after revocation.
        assert (await client.broker_status())["approved"] is False

        await client.stop()
        await asyncio.wait_for(client_task, 1.0)
        replacement_task = await _connect_over_socketpair(server, client, auto_approve=False)
        try:
            assert (await client.broker_status())["approved"] is False
        finally:
            await client.stop()
            await asyncio.wait_for(replacement_task, 1.0)
    finally:
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


def _write_malformed_grant(path: Path, kind: str) -> None:
    path.parent.mkdir(mode=0o700)
    if kind == "fifo":
        os.mkfifo(path)
        return
    if kind == "deep_json":
        nested_json = "[" * 3000 + json.dumps("cli") + "]" * 3000
        path.write_text(
            '{"granted_at":123.5,"source":'
            + nested_json
            + ',"token_binding":'
            + json.dumps("0" * 64)
            + ',"version":1}',
            encoding="utf-8",
        )
    else:
        payload: dict[str, object] = {
            "version": 1,
            "granted_at": 123.5,
            "source": "cli",
            "token_binding": "0" * 64,
        }
        if kind == "huge_timestamp":
            payload["granted_at"] = 10**1000
        elif kind == "non_ascii_binding":
            payload["token_binding"] = "é" * 64
        else:
            raise AssertionError(f"unknown malformed grant kind: {kind}")
        path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _replace_grant_with_unsafe_target(path: Path, kind: str) -> None:
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(path.parent / "missing-grant-target")
    else:
        raise AssertionError(f"unknown unsafe grant kind: {kind}")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["huge_timestamp", "non_ascii_binding", "deep_json", "fifo"])
async def test_malformed_grants_are_no_grant_and_auth_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    path = workstation_grant_path()
    _write_malformed_grant(path, kind)
    run_base = tmp_path / "broker-run"
    startup_paths = ensure_run_directory(19777, base_dir=run_base, prepare_control_endpoint=False)
    startup_paths.extension_secret.write_text(token)
    startup_paths.extension_secret.chmod(0o600)

    def relay_factory(secret: str, port: int, on_event, on_disconnect, _on_pairing_complete):
        return FakeRelay(secret, port, on_event, on_disconnect)

    server = BrowserExtensionBrokerServer(19777, base_dir=run_base, relay_factory=relay_factory)
    assert load_workstation_grant(path, token) is None
    await server.start()
    try:
        assert server._workstation_grant is None
        client = BrokerClient(19777, _ignore_event, auto_spawn=False)
        server_task = await _connect_over_socketpair(server, client, auto_approve=False)
        try:
            assert (await client.broker_status())["approved"] is False
        finally:
            await client.stop()
            await asyncio.wait_for(server_task, 1.0)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_revoked_grant_does_not_approve_overlapping_reauthentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777)
    server._broker_auth_token = token
    server._relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    replacement: BrokerClient | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        await operator.grant_workstation()
        assert (await client.broker_status())["approved"] is True
        await operator.revoke_workstation()
        assert (await client.broker_status())["approved"] is False

        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement._client_id = client._client_id
        replacement._recovery_secret = client._recovery_secret
        replacement_task = await _connect_over_socketpair(server, replacement, auto_approve=False)
        assert (await replacement.broker_status())["approved"] is False
    finally:
        if replacement is not None:
            await replacement.stop()
        if replacement_task is not None:
            await asyncio.wait_for(replacement_task, 1.0)
        await client.stop()
        await asyncio.wait_for(client_task, 1.0)
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_revoke_preserves_interactive_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    replacement: BrokerClient | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert (await client.broker_status())["approved"] is True
        assert server._credentials[client._client_id].approval_source == "interactive"

        result = await operator.revoke_workstation()
        assert result["revoked"] is True
        assert result["scope"] == "grant"
        assert result["cleared"]["interactive"] == 0
        assert (await client.broker_status())["approved"] is True

        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement._client_id = client._client_id
        replacement._recovery_secret = client._recovery_secret
        replacement_task = await _connect_over_socketpair(server, replacement, auto_approve=False)
        assert (await replacement.broker_status())["approved"] is True
        assert server._credentials[client._client_id].approval_source == "interactive"
    finally:
        if replacement is not None:
            await replacement.stop()
        if replacement_task is not None:
            await asyncio.wait_for(replacement_task, 1.0)
        await client.stop()
        await asyncio.wait_for(client_task, 1.0)
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_interactive_approval_dies_on_true_disconnect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    replacement: BrokerClient | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert (await client.broker_status())["approved"] is True
        assert server._credentials[client._client_id].approval_source == "interactive"

        # Pairing also persists a workstation grant. Remove it to isolate true
        # disconnect semantics from the independent grant-source path.
        assert remove_workstation_grant(workstation_grant_path()) is True
        await client.stop()
        await asyncio.wait_for(client_task, 1.0)

        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement._client_id = client._client_id
        replacement._recovery_secret = client._recovery_secret
        replacement_task = await _connect_over_socketpair(server, replacement, auto_approve=False)
        assert (await replacement.broker_status())["approved"] is False
        credential = server._credentials[client._client_id]
        assert credential.approved is False
        assert credential.approval_source is None
        assert credential.approval_event.is_set() is False
    finally:
        if replacement is not None:
            await replacement.stop()
        if replacement_task is not None:
            await asyncio.wait_for(replacement_task, 1.0)
        await client.stop()
        if not client_task.done():
            await asyncio.wait_for(client_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_revoke_all_clears_interactive_approval_and_blocks_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    replacement: BrokerClient | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert (await client.broker_status())["approved"] is True

        result = await operator.revoke_workstation(scope="all")
        assert result == {
            "revoked": True,
            "scope": "all",
            "cleared": {"grant": 0, "interactive": 1},
        }
        assert (await client.broker_status())["approved"] is False
        credential = server._credentials[client._client_id]
        assert credential.approved is False
        assert credential.approval_source is None
        assert credential.approval_event.is_set() is False

        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement._client_id = client._client_id
        replacement._recovery_secret = client._recovery_secret
        replacement_task = await _connect_over_socketpair(server, replacement, auto_approve=False)
        assert (await replacement.broker_status())["approved"] is False
    finally:
        if replacement is not None:
            await replacement.stop()
        if replacement_task is not None:
            await asyncio.wait_for(replacement_task, 1.0)
        await client.stop()
        await asyncio.wait_for(client_task, 1.0)
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fifo", "symlink"])
async def test_revoke_all_clears_live_approvals_when_grant_path_is_unsafe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    interactive_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    grant_client: BrokerClient | None = None
    interactive_task = await _connect_over_socketpair(server, interactive_client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    grant_task: asyncio.Task[None] | None = None
    try:
        await interactive_client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert server._credentials[interactive_client._client_id].approval_source == "interactive"

        grant_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
        grant_task = await _connect_over_socketpair(server, grant_client, auto_approve=False)
        assert (await grant_client.broker_status())["approved"] is True
        assert server._credentials[grant_client._client_id].approval_source == "grant"

        _replace_grant_with_unsafe_target(workstation_grant_path(), kind)
        result = await operator.revoke_workstation(scope="all")
        assert result == {
            "revoked": False,
            "scope": "all",
            "cleared": {"grant": 1, "interactive": 1},
            "file_removal_error": "Workstation grant path failed safety validation",
        }
        assert (await interactive_client.broker_status())["approved"] is False
        assert (await grant_client.broker_status())["approved"] is False
    finally:
        if grant_client is not None:
            await grant_client.stop()
        if grant_task is not None:
            await asyncio.wait_for(grant_task, 1.0)
        await interactive_client.stop()
        await asyncio.wait_for(interactive_task, 1.0)
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_revoke_default_clears_live_grant_approval_when_grant_path_is_fifo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    interactive_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    operator = BrokerClient(19777, _ignore_event, auto_spawn=False, operator=True)
    grant_client: BrokerClient | None = None
    interactive_task = await _connect_over_socketpair(server, interactive_client, auto_approve=False)
    operator_task = await _connect_over_socketpair(server, operator)
    grant_task: asyncio.Task[None] | None = None
    try:
        await interactive_client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})

        grant_client = BrokerClient(19777, _ignore_event, auto_spawn=False)
        grant_task = await _connect_over_socketpair(server, grant_client, auto_approve=False)
        assert (await grant_client.broker_status())["approved"] is True
        assert server._credentials[grant_client._client_id].approval_source == "grant"

        _replace_grant_with_unsafe_target(workstation_grant_path(), "fifo")
        result = await operator.revoke_workstation()
        assert result == {
            "revoked": False,
            "scope": "grant",
            "cleared": {"grant": 1, "interactive": 0},
            "file_removal_error": "Workstation grant path failed safety validation",
        }
        assert (await grant_client.broker_status())["approved"] is False
        assert (await interactive_client.broker_status())["approved"] is True
    finally:
        if grant_client is not None:
            await grant_client.stop()
        if grant_task is not None:
            await asyncio.wait_for(grant_task, 1.0)
        await interactive_client.stop()
        await asyncio.wait_for(interactive_task, 1.0)
        await operator.stop()
        await asyncio.wait_for(operator_task, 1.0)
        await server.stop()


@pytest.mark.asyncio
async def test_failed_successor_handshake_clears_transferred_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token = "broker-token"
    server = BrowserExtensionBrokerServer(19777, pairing_opener=lambda _url: True)
    server._broker_auth_token = token
    relay = FakeRelay(token, 19777, server._handle_extension_event, server._handle_disconnect)
    server._relay = relay
    client = BrokerClient(19777, _ignore_event, auto_spawn=False)
    client_task = await _connect_over_socketpair(server, client, auto_approve=False)
    replacement: BrokerClient | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        await client.begin_pairing()
        offer = await server._handle_pairing_complete()
        assert offer is not None
        await relay.emit_event("pairing.approved", {"approvalNonce": offer["approvalNonce"]})
        assert server._credentials[client._client_id].approved is True

        async def fail_snapshot(_connection) -> None:
            raise RuntimeError("successor snapshot failed")

        monkeypatch.setattr(server, "_send_client_snapshot", fail_snapshot)
        replacement = BrokerClient(19777, _ignore_event, auto_spawn=False)
        replacement._client_id = client._client_id
        replacement._recovery_secret = client._recovery_secret
        replacement_task = await _connect_over_socketpair(server, replacement, auto_approve=False)
        await asyncio.wait_for(replacement_task, 1.0)

        credential = server._credentials[client._client_id]
        assert client._client_id not in server._clients
        assert credential.approved is False
        assert credential.approval_source is None
        assert credential.approval_event.is_set() is False
    finally:
        if replacement is not None:
            await replacement.stop()
        if replacement_task is not None and not replacement_task.done():
            await asyncio.wait_for(replacement_task, 1.0)
        await client.stop()
        if not client_task.done():
            await asyncio.wait_for(client_task, 1.0)
        await server.stop()


def test_workstation_cli_commands_issue_authenticated_control_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from skyvern.cli.commands import browser as browser_commands
    from skyvern.cli.commands.browser import browser_app

    class StubClient:
        def __init__(self) -> None:
            self.grant_calls = 0
            self.revoke_scopes: list[str] = []
            self.stop_calls = 0
            self.fail_revoke = False

        async def grant_workstation(self) -> dict[str, object]:
            self.grant_calls += 1
            return {"granted": True, "source": "cli", "grantedAt": 123.0}

        async def revoke_workstation(self, *, scope: str = "grant") -> dict[str, object]:
            self.revoke_scopes.append(scope)
            result: dict[str, object] = {
                "revoked": True,
                "scope": scope,
                "cleared": {"grant": 1, "interactive": 1 if scope == "all" else 0},
            }
            if self.fail_revoke:
                result["file_removal_error"] = "Workstation grant path failed safety validation"
            return result

        async def stop(self) -> None:
            self.stop_calls += 1

    client = StubClient()
    monkeypatch.setattr(browser_commands, "prepare_cli_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(browser_commands.BrowserExtensionRuntime, "configured_port", lambda: 19777)

    async def fake_broker_client(_port: int, *, auto_spawn: bool, operator: bool) -> StubClient:
        assert auto_spawn is False
        assert operator is True
        return client

    monkeypatch.setattr(browser_commands, "_broker_client", fake_broker_client)
    runner = CliRunner()

    approved = runner.invoke(browser_app, ["extension-approve-workstation"])
    revoked = runner.invoke(browser_app, ["extension-revoke-workstation"])
    revoked_all = runner.invoke(browser_app, ["extension-revoke-workstation", "--all"])
    client.fail_revoke = True
    failed_revoke = runner.invoke(browser_app, ["extension-revoke-workstation"])

    assert approved.exit_code == 0, approved.output
    assert revoked.exit_code == 0, revoked.output
    assert revoked_all.exit_code == 0, revoked_all.output
    assert failed_revoke.exit_code == 1, failed_revoke.output
    assert client.grant_calls == 1
    assert client.revoke_scopes == ["grant", "all", "grant"]
    assert client.stop_calls == 4
    assert "Workstation approval: granted." in approved.output
    assert "Workstation approval source: cli." in approved.output
    assert "Workstation approval: revoked." in revoked.output
    assert "Grant-source approvals cleared: 1." in revoked.output
    assert "Interactive approvals were not cleared; they die on true disconnect." in revoked.output
    assert "Interactive approvals cleared: 1." in revoked_all.output
    assert "Warning: Workstation grant path failed safety validation." in failed_revoke.output
