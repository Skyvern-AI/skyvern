import asyncio
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot import credential_pause as pauses
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, _ground_user_provided_sites
from skyvern.forge.sdk.copilot.tools.credential_fill import _request_credential
from skyvern.forge.sdk.routes import workflow_copilot as routes
from tests.unit.test_copilot_ask_user import setup_question_chat
from tests.unit.test_copilot_credential_pause import _FakeCache, _make_credential, _make_stream


@pytest.mark.asyncio
async def test_recovery_capability_does_not_keep_a_direct_credential_ask_alive(sqlite_engine, monkeypatch):
    _, _, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    ctx.client_supports_credential_pause = True
    ctx.client_supports_credential_pause_recovery = True
    ctx.credential_recovery_token_digest = pauses.credential_recovery_token_digest("a" * 64)
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    ctx.stream = _make_stream(disconnected=True)

    result = await _request_credential("https://portal.example.com/login", "Login", ctx)

    assert result["status"] == "unanswered"
    assert result["outcome"] == "declined"
    ctx.stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnected_card_recovers_through_authenticated_history_and_resumes_same_call(
    sqlite_engine, monkeypatch
):
    repo, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    cache = _FakeCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(
        app.DATABASE,
        "credentials",
        SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()])),
    )
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "RECONCILE_ABANDON_AFTER_SECONDS", -1)
    ctx.client_supports_credential_pause = True
    ctx.client_supports_credential_pause_recovery = True
    ctx.credential_recovery_armed = True
    recovery_token = "a" * 64
    ctx.credential_recovery_token_digest = pauses.credential_recovery_token_digest(recovery_token)
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "https://portal.example.com/login", [])
    ctx.stream = _make_stream(disconnected=True)
    emitted = asyncio.Event()
    ctx.stream.send.side_effect = lambda _: emitted.set()
    client._transport.app.add_api_route(
        "/credential-response", routes.workflow_copilot_credential_response, methods=["POST"]
    )
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            await asyncio.sleep(0.03)
            assert not invocation.done()
            missing_proof = await client.get(
                "/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id}
            )
            assert missing_proof.json()["pending_credential_requests"] == []
            wrong_proof = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": "b" * 64},
            )
            assert wrong_proof.json()["pending_credential_requests"] == []
            history = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": recovery_token},
            )
            card = history.json()["pending_credential_requests"][0]
            assert not invocation.done()
            assert card == ctx.stream.send.await_args.args[0].model_dump(mode="json")
            assert card["turn_id"] == "turn"
            foreign = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={
                    "test-org": "other",
                    "X-Copilot-Credential-Recovery-Token": recovery_token,
                },
            )
            assert foreign.json()["pending_credential_requests"] == []
            other_chat = await repo.create_workflow_copilot_chat(organization_id="org", workflow_permanent_id="other")
            other = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": other_chat.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": recovery_token},
            )
            assert other.json()["pending_credential_requests"] == []
            body = {
                "workflow_copilot_chat_id": card["workflow_copilot_chat_id"],
                "turn_id": card["turn_id"],
                "resume_token": card["resume_token"],
                "action": "connected",
                "credential_id": "cred_1",
            }
            invalid = await client.post("/credential-response", json={**body, "resume_token": "invalid"})
            assert invalid.status_code == 403
            accepted = await client.post("/credential-response", json=body)
            assert accepted.status_code == 200
            result = await asyncio.wait_for(invocation, 1)
            assert result["status"] == "connected"
            assert result["credential_id"] == "cred_1"
            reloaded = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": recovery_token},
            )
            assert reloaded.json()["pending_credential_requests"] == []
            duplicate = await client.post("/credential-response", json=body)
            assert duplicate.status_code == 409
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["expired", "cancelled"])
async def test_history_hides_expired_or_cancelled_recoverable_card(sqlite_engine, monkeypatch, ending):
    from datetime import datetime, timedelta

    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    cache = _FakeCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    ctx.client_supports_credential_pause = True
    ctx.client_supports_credential_pause_recovery = True
    ctx.credential_recovery_armed = True
    recovery_token = "a" * 64
    recovery_digest = pauses.credential_recovery_token_digest(recovery_token)
    ctx.credential_recovery_token_digest = recovery_digest
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "https://portal.example.com/login", [])
    ctx.stream = _make_stream(disconnected=True)
    emitted = asyncio.Event()
    ctx.stream.send.side_effect = lambda _: emitted.set()
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            card = ctx.stream.send.await_args.args[0]
            if ending == "expired":
                expires_at = datetime.now(UTC) - timedelta(seconds=1)
                cache.store[pauses.credential_pause_active_key("org", ctx.workflow_copilot_chat_id, "turn")] = (
                    pauses._encode_active_pause(
                        card.resume_token,
                        expires_at,
                        card=card.model_copy(update={"expires_at": expires_at}),
                        recovery_token_digest=recovery_digest,
                    )
                )
            else:
                invocation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await invocation
            history = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": recovery_token},
            )
            assert history.json()["pending_credential_requests"] == []
            with pytest.raises(pauses.CredentialPauseRejection) as rejection:
                await pauses.resolve_credential_pause(
                    cache,
                    organization_id="org",
                    workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
                    turn_id="turn",
                    resume_token=card.resume_token,
                    action="skip",
                    credential_id=None,
                )
            assert rejection.value.status_code == (410 if ending == "expired" else 409)
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)


@pytest.mark.asyncio
async def test_pending_credential_requests_propagates_redis_failure(monkeypatch):
    cache = AsyncMock()
    cache.get.side_effect = RuntimeError("redis unavailable")
    monkeypatch.setattr(app, "CACHE", cache)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await pauses.pending_credential_requests("org", "chat", ["turn"], "a" * 64)
