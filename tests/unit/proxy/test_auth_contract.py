"""Reusable contract suite for AuthPort adapters.

Any adapter (the generic static-keys adapter here, the cloud Skyvern API-key
adapter under tests/cloud/) subclasses AuthPortContract and overrides the four
hooks; every behavioral guarantee of the port — credential extraction from
header/query/path, authentication, org-vs-session authorization, clean rejection,
no upstream credential leak, and token redaction — is asserted once, here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Mapping

import pytest
from structlog.testing import capture_logs

from skyvern.proxy.adapters.memory import (
    ForwardAllEventPolicy,
    InMemorySessionRegistry,
    InMemoryUpstreamBrowser,
    NoOpMetrics,
    StaticKeyAuth,
)
from skyvern.proxy.adapters.websocket_server import CdpProxyServer
from skyvern.proxy.core.session import Principal, ProxySession, ResolvedSession, SessionResolution
from skyvern.proxy.ports import AuthPort, UpstreamConnection

SESSION_ID = "s1"


class FakeClientWebSocket:
    def __init__(self, path: str, headers: Mapping[str, str] | None = None) -> None:
        self.request = SimpleNamespace(headers=dict(headers or {}), path=path)
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def __aiter__(self) -> FakeClientWebSocket:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason


class RecordingUpstreamBrowser(InMemoryUpstreamBrowser):
    def __init__(self) -> None:
        self.sessions: list[ProxySession] = []

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        self.sessions.append(session)
        return await super().connect(session)


class AuthPortContract:
    def make_auth(self) -> AuthPort:
        raise NotImplementedError

    def valid_credential(self) -> str:
        raise NotImplementedError

    def credential_org_id(self) -> str:
        raise NotImplementedError

    def invalid_credential(self) -> str:
        raise NotImplementedError

    def _server_for_org(self, org_id: str) -> tuple[CdpProxyServer, RecordingUpstreamBrowser]:
        upstream = RecordingUpstreamBrowser()
        sessions = InMemorySessionRegistry()
        sessions.put(
            ResolvedSession(
                session_id=SESSION_ID,
                upstream_adapter="memory",
                upstream_ws_url="ws://upstream.internal:9222/x",
                organization_id=org_id,
                connect_headers={"x-routing": "operator-only"},
            )
        )
        server = CdpProxyServer(
            upstream=upstream,  # type: ignore[arg-type]
            sessions=sessions,
            auth=self.make_auth(),
            metrics=NoOpMetrics(),
            event_policy=ForwardAllEventPolicy(),
        )
        return server, upstream

    @pytest.mark.asyncio
    async def test_authenticate_valid_credential_returns_principal_for_its_org(self) -> None:
        principal = await self.make_auth().authenticate({"x-api-key": self.valid_credential()})
        assert principal is not None
        assert principal.organization_id == self.credential_org_id()

    @pytest.mark.asyncio
    async def test_authenticate_invalid_credential_returns_none(self) -> None:
        assert await self.make_auth().authenticate({"x-api-key": self.invalid_credential()}) is None

    @pytest.mark.asyncio
    async def test_authenticate_missing_credential_returns_none(self) -> None:
        assert await self.make_auth().authenticate({}) is None

    def test_authorize_owning_org_is_allowed(self) -> None:
        principal = Principal(principal_id="p", organization_id=self.credential_org_id())
        resolution = SessionResolution.active(
            ResolvedSession(
                session_id=SESSION_ID,
                upstream_adapter="memory",
                upstream_ws_url="ws://x",
                organization_id=self.credential_org_id(),
            )
        )
        assert self.make_auth().authorize(principal, resolution) is True

    def test_authorize_foreign_org_is_rejected(self) -> None:
        principal = Principal(principal_id="p", organization_id=self.credential_org_id())
        resolution = SessionResolution.active(
            ResolvedSession(
                session_id=SESSION_ID,
                upstream_adapter="memory",
                upstream_ws_url="ws://x",
                organization_id="o_someone_else",
            )
        )
        assert self.make_auth().authorize(principal, resolution) is False

    def test_authorize_unknown_session_is_rejected(self) -> None:
        principal = Principal(principal_id="p", organization_id=self.credential_org_id())
        assert self.make_auth().authorize(principal, SessionResolution.unknown()) is False

    @pytest.mark.asyncio
    async def test_header_credential_authenticates_and_dials(self) -> None:
        server, upstream = self._server_for_org(self.credential_org_id())
        ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": self.valid_credential()})
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert ws.close_code is None
        assert len(upstream.sessions) == 1

    @pytest.mark.asyncio
    async def test_query_credential_authenticates_and_dials(self) -> None:
        server, upstream = self._server_for_org(self.credential_org_id())
        ws = FakeClientWebSocket(path=f"/{SESSION_ID}?token={self.valid_credential()}")
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert ws.close_code is None
        assert len(upstream.sessions) == 1

    @pytest.mark.asyncio
    async def test_path_credential_authenticates_and_dials(self) -> None:
        server, upstream = self._server_for_org(self.credential_org_id())
        ws = FakeClientWebSocket(path=f"/key/{self.valid_credential()}/{SESSION_ID}")
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert ws.close_code is None
        assert len(upstream.sessions) == 1

    @pytest.mark.asyncio
    async def test_invalid_credential_closes_cleanly_before_dial(self) -> None:
        server, upstream = self._server_for_org(self.credential_org_id())
        ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": self.invalid_credential()})
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert ws.close_code == 4401
        assert not upstream.sessions

    @pytest.mark.asyncio
    async def test_wrong_org_gets_uniform_4404_before_dial(self) -> None:
        server, upstream = self._server_for_org("o_a_different_org")
        ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": self.valid_credential()})
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert (ws.close_code, ws.close_reason) == (4404, "unknown session")
        assert not upstream.sessions

    @pytest.mark.asyncio
    async def test_wrong_org_is_indistinguishable_from_unknown_session(self) -> None:
        foreign_server, foreign_upstream = self._server_for_org("o_a_different_org")
        foreign_ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": self.valid_credential()})
        await foreign_server._handle_client(foreign_ws)  # type: ignore[arg-type]

        unknown_server, unknown_upstream = self._server_for_org(self.credential_org_id())
        unknown_ws = FakeClientWebSocket(path="/s_missing", headers={"x-api-key": self.valid_credential()})
        await unknown_server._handle_client(unknown_ws)  # type: ignore[arg-type]

        assert (foreign_ws.close_code, foreign_ws.close_reason) == (unknown_ws.close_code, unknown_ws.close_reason)
        assert not foreign_upstream.sessions
        assert not unknown_upstream.sessions

    @pytest.mark.asyncio
    async def test_client_credential_is_never_forwarded_upstream(self) -> None:
        server, upstream = self._server_for_org(self.credential_org_id())
        ws = FakeClientWebSocket(
            path=f"/{SESSION_ID}?token={self.valid_credential()}",
            headers={"x-api-key": self.valid_credential()},
        )
        await server._handle_client(ws)  # type: ignore[arg-type]
        assert len(upstream.sessions) == 1
        dialed = upstream.sessions[0]
        blob = dialed.upstream_ws_url + "".join(f"{k}={v}" for k, v in dialed.connect_headers.items())
        assert self.valid_credential() not in blob

    @pytest.mark.asyncio
    async def test_credential_value_is_redacted_from_logs(self) -> None:
        secret = self.valid_credential()
        with capture_logs() as logs:
            server, _ = self._server_for_org(self.credential_org_id())
            ok_ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": secret})
            await server._handle_client(ok_ws)  # type: ignore[arg-type]
            bad_server, _ = self._server_for_org(self.credential_org_id())
            bad_ws = FakeClientWebSocket(path=f"/{SESSION_ID}", headers={"x-api-key": self.invalid_credential()})
            await bad_server._handle_client(bad_ws)  # type: ignore[arg-type]
        assert secret not in json.dumps(logs, default=str)


class TestStaticKeyAuthContract(AuthPortContract):
    _VALID = "static-key-abc123"
    _ORG = "o_static"

    def make_auth(self) -> AuthPort:
        return StaticKeyAuth({self._VALID: Principal(principal_id=self._ORG, organization_id=self._ORG)})

    def valid_credential(self) -> str:
        return self._VALID

    def credential_org_id(self) -> str:
        return self._ORG

    def invalid_credential(self) -> str:
        return "static-key-does-not-exist"
