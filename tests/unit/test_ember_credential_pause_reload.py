import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.ember import agent as agent_module
from skyvern.forge.sdk.ember import credential_pause as pauses
from skyvern.forge.sdk.ember.config import EmberConfig
from skyvern.forge.sdk.ember.request_policy import RequestPolicy, _ground_user_provided_sites
from skyvern.forge.sdk.ember.tools.credential_fill import _request_credential
from skyvern.forge.sdk.routes import ember as routes
from skyvern.forge.sdk.schemas.browser_profiles import BrowserProfile
from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.schemas.ember import CredentialRegistration, EmberChatRequest
from tests.unit.ember_test_helpers import stub_ember_agent_loop, wire_credential_vault
from tests.unit.test_ember_ask_user import setup_question_chat
from tests.unit.test_ember_credential_pause import (
    _REGISTRATION,
    _FakeCache,
    _make_credential,
    _make_stream,
    _RegistrationVault,
)

both_recovery_headers = pytest.mark.parametrize(
    "recovery_header", ["X-Ember-Credential-Recovery-Token", "X-Copilot-Credential-Recovery-Token"]
)


@pytest.mark.asyncio
async def test_recovery_capability_does_not_keep_a_direct_credential_ask_alive(sqlite_engine, monkeypatch):
    _, _, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    ctx.client_supports_credential_pause = True
    ctx.client_supports_credential_pause_recovery = True
    ctx.credential_recovery_token_digest = pauses.credential_recovery_token_digest("a" * 64)
    ctx.ember_config = EmberConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    ctx.stream = _make_stream(disconnected=True)

    result = await _request_credential("https://portal.example.com/login", "Login", ctx)

    assert result["status"] == "unanswered"
    assert result["outcome"] == "declined"
    ctx.stream.send.assert_not_awaited()


async def _turn_ctx(monkeypatch, chat_id: str, recovery_token: str, *, client_detached: bool):
    monkeypatch.setattr(agent_module, "restore_pending_workflow_proposal", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "seed_repair_origin_run", AsyncMock(return_value=SimpleNamespace(finished=True)))
    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", AsyncMock(return_value=None))
    stub_ember_agent_loop(
        monkeypatch,
        AsyncMock(return_value=SimpleNamespace(final_output='{"type":"REPLY","user_response":"ok"}', new_items=[])),
    )
    sink = []
    await agent_module.run_ember_agent(
        stream=_make_stream(),
        organization_id="org",
        chat_request=EmberChatRequest(
            message="Sign in to the portal",
            workflow_yaml="title: Portal\n",
            workflow_id="workflow",
            workflow_permanent_id="workflow",
            workflow_copilot_chat_id=chat_id,
            supports_credential_pause=True,
            supports_credential_pause_recovery=True,
            supports_credential_generation=True,
            credential_recovery_token=recovery_token,
            client_detached=client_detached,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=EmberConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5),
        turn_id="turn",
        ctx_sink=sink,
    )
    ctx = sink[0]
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "https://portal.example.com/login", [])
    ctx.stream = _make_stream(disconnected=True)
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("client_detached", [True, False])
async def test_detached_turn_keeps_a_card_raised_after_the_stream_closed(sqlite_engine, monkeypatch, client_detached):
    _, client, question_ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(
        app.DATABASE,
        "credentials",
        SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()])),
    )
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "RECONCILE_ABANDON_AFTER_SECONDS", -1)
    recovery_token = "a" * 64
    ctx = await _turn_ctx(
        monkeypatch, question_ctx.workflow_copilot_chat_id, recovery_token, client_detached=client_detached
    )
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        try:
            if not client_detached:
                result = await asyncio.wait_for(invocation, 1)
                assert result["outcome"] == "declined"
                ctx.stream.send.assert_not_awaited()
                return
            await asyncio.sleep(0.05)
            assert not invocation.done()
            history = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Ember-Credential-Recovery-Token": recovery_token},
            )
            [card] = history.json()["pending_credential_requests"]
            accepted = await client.post(
                "/credential-response",
                json={
                    "workflow_copilot_chat_id": card["workflow_copilot_chat_id"],
                    "turn_id": card["turn_id"],
                    "resume_token": card["resume_token"],
                    "action": "connected",
                    "credential_id": "cred_1",
                },
            )
            assert accepted.status_code == 200
            result = await asyncio.wait_for(invocation, 1)
            assert (result["status"], result["credential_id"]) == ("connected", "cred_1")
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)


@pytest.mark.asyncio
async def test_detached_turn_registration_card_raised_after_the_stream_closed_resolves_by_generate(
    sqlite_engine, monkeypatch
):
    _, client, question_ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "RECONCILE_ABANDON_AFTER_SECONDS", -1)
    credential = _make_credential("cred_new", "Portal test account")
    monkeypatch.setattr(
        app.DATABASE,
        "credentials",
        SimpleNamespace(
            get_credentials_by_ids=AsyncMock(return_value=[credential]), get_credentials=AsyncMock(return_value=[])
        ),
    )
    _RegistrationVault(credential).install(monkeypatch)
    recovery_token = "a" * 64
    ctx = await _turn_ctx(monkeypatch, question_ctx.workflow_copilot_chat_id, recovery_token, client_detached=True)
    api = client._transport.app
    api.dependency_overrides[routes.org_auth_service.get_current_user_id_or_none] = lambda: "user-1"
    api.add_api_route("/credential-generate", routes.ember_credential_generate, methods=["POST"])
    async with client:
        invocation = asyncio.create_task(
            _request_credential(
                "https://portal.example.com/signup",
                "Create the test account",
                ctx,
                registration=CredentialRegistration(**_REGISTRATION),
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not invocation.done()
            history = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={"X-Copilot-Credential-Recovery-Token": recovery_token},
            )
            [card] = history.json()["pending_credential_requests"]
            assert card["reason"] == "credential_registration"
            with skyvern_context.scoped(SkyvernContext()):
                generated = await client.post(
                    "/credential-generate",
                    json={
                        "workflow_copilot_chat_id": card["workflow_copilot_chat_id"],
                        "turn_id": card["turn_id"],
                        "resume_token": card["resume_token"],
                    },
                )
            assert generated.json()["result"] == "connected"
            result = await asyncio.wait_for(invocation, 1)
            assert (result["status"], result["credential_id"]) == ("connected", "cred_new")
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)


@both_recovery_headers
@pytest.mark.asyncio
async def test_disconnected_card_recovers_through_authenticated_history_and_resumes_same_call(
    recovery_header: str, sqlite_engine, monkeypatch
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
    ctx.ember_config = EmberConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "https://portal.example.com/login", [])
    ctx.stream = _make_stream(disconnected=True)
    emitted = asyncio.Event()
    ctx.stream.send.side_effect = lambda _: emitted.set()
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
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
                headers={recovery_header: "b" * 64},
            )
            assert wrong_proof.json()["pending_credential_requests"] == []
            history = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
                headers={recovery_header: recovery_token},
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
                    recovery_header: recovery_token,
                },
            )
            assert foreign.json()["pending_credential_requests"] == []
            other_chat = await repo.create_ember_chat(organization_id="org", workflow_permanent_id="other")
            other = await client.get(
                "/history",
                params={"workflow_copilot_chat_id": other_chat.workflow_copilot_chat_id},
                headers={recovery_header: recovery_token},
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
                headers={recovery_header: recovery_token},
            )
            assert reloaded.json()["pending_credential_requests"] == []
            duplicate = await client.post("/credential-response", json=body)
            assert duplicate.status_code == 409
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)


@both_recovery_headers
@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["expired", "cancelled"])
async def test_history_hides_expired_or_cancelled_recoverable_card(
    recovery_header: str, sqlite_engine, monkeypatch, ending
):
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
    ctx.ember_config = EmberConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
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
                headers={recovery_header: recovery_token},
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


def _signed_in_profile() -> BrowserProfile:
    now = datetime.now(UTC)
    return BrowserProfile(
        browser_profile_id="bp_signed_in",
        organization_id="org",
        name="Sign-in for portal.example.com",
        created_at=now,
        modified_at=now,
    )


def _sign_in_card_context(ctx, *, recovery: bool):
    ctx.browser_session_id = "pbs_debug"
    ctx.client_supports_credential_pause = True
    ctx.client_supports_credential_pause_recovery = recovery
    ctx.credential_recovery_armed = False
    ctx.credential_recovery_token_digest = pauses.credential_recovery_token_digest("a" * 64) if recovery else None
    ctx.ember_config = EmberConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "https://portal.example.com/login", [])
    ctx.stream = _make_stream()
    emitted = asyncio.Event()
    ctx.stream.send.side_effect = lambda _: emitted.set()
    ctx.stream.is_disconnected.side_effect = lambda: recovery and emitted.is_set()
    return emitted


@both_recovery_headers
@pytest.mark.asyncio
async def test_signing_in_survives_a_reload_and_done_resumes_the_same_call_with_the_saved_profile(
    recovery_header: str, sqlite_engine, monkeypatch
):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "RECONCILE_ABANDON_AFTER_SECONDS", -1)
    release_first_save = asyncio.Event()
    save_calls: list[dict] = []

    async def save(**kwargs):
        save_calls.append(kwargs)
        if len(save_calls) == 1:
            await release_first_save.wait()
            return None, 0
        if len(save_calls) == 2:
            raise RuntimeError("The live browser is not reachable")
        return _signed_in_profile(), 2

    monkeypatch.setattr(routes, "create_profile_from_running_session", save)
    emitted = _sign_in_card_context(ctx, recovery=True)
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    headers = {recovery_header: "a" * 64}
    params = {"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id}
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            card = ctx.stream.send.await_args.args[0]
            assert card.sign_in_browser_session_id == "pbs_debug"
            body = {
                "workflow_copilot_chat_id": card.workflow_copilot_chat_id,
                "turn_id": card.turn_id,
                "resume_token": card.resume_token,
            }

            started = await client.post("/credential-response", json={**body, "action": "signing_in"})
            assert started.status_code == 200
            extended = datetime.fromisoformat(started.json()["expires_at"])
            manual_window = timedelta(seconds=settings.EMBER_MANUAL_SIGN_IN_TIMEOUT_SECONDS)
            assert extended - card.expires_at > manual_window - timedelta(seconds=30)
            again = await client.post("/credential-response", json={**body, "action": "signing_in"})
            assert datetime.fromisoformat(again.json()["expires_at"]) == extended

            reloaded = (await client.get("/history", params=params, headers=headers)).json()
            restored = reloaded["pending_credential_requests"][0]
            assert restored["signing_in"] is True
            assert datetime.fromisoformat(restored["expires_at"]) == extended

            saving = asyncio.create_task(client.post("/credential-response", json={**body, "action": "signed_in"}))
            while not save_calls:
                await asyncio.sleep(0.01)
            during_save = (await client.get("/history", params=params, headers=headers)).json()
            assert during_save["pending_credential_requests"][0]["resume_token"] == card.resume_token
            release_first_save.set()
            nothing = await saving
            assert nothing.json() == {
                "result": "no_sign_in_found",
                "expires_at": None,
                "host": "portal.example.com",
                "browser_profile_id": None,
                "site_question": None,
            }
            failed = await client.post("/credential-response", json={**body, "action": "signed_in"})
            assert failed.json()["result"] == "save_failed"
            await asyncio.sleep(0.05)
            assert not invocation.done()

            with capture_logs() as logs:
                done = await client.post("/credential-response", json={**body, "action": "signed_in"})
                result = await asyncio.wait_for(invocation, 1)
            assert done.json()["result"] == "signed_in"
            assert done.json()["browser_profile_id"] == "bp_signed_in"
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    assert save_calls[-1]["browser_session_id"] == "pbs_debug"
    assert save_calls[-1]["login_urls"] == ["https://portal.example.com/login"]
    assert result["status"] == "signed_in"
    assert result["browser_profile_id"] == "bp_signed_in"
    assert result["credential_provided"] is False
    assert "next" not in result
    assert ctx.credential_pause_outcome == "signed_in"
    assert ctx.request_policy.credential_pause_signed_in_profile_id == "bp_signed_in"
    assert ctx.request_policy.credential_pause_signed_in_site == "portal.example.com"
    assert ctx.request_policy.resolved_credentials == []
    assert any(entry["event"] == "ember_credential_pause_signed_in" for entry in logs)


@both_recovery_headers
@pytest.mark.asyncio
async def test_an_update_card_restores_its_sign_in_after_a_reload_and_done_answers_it(
    recovery_header: str, sqlite_engine, monkeypatch
):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        routes, "create_profile_from_running_session", AsyncMock(return_value=(_signed_in_profile(), 2))
    )
    emitted = _sign_in_card_context(ctx, recovery=True)
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp="seed"))
    ctx.request_policy.resolved_credentials = [credential]
    ctx.request_policy.auto_bound_credentials = [credential]
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    headers = {recovery_header: "a" * 64}
    params = {"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id}
    async with client:
        invocation = asyncio.create_task(
            _request_credential("https://portal.example.com/login", "Login", ctx, "cred_1", rejected_by_site=True)
        )
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            card = ctx.stream.send.await_args.args[0]
            body = {
                "workflow_copilot_chat_id": card.workflow_copilot_chat_id,
                "turn_id": card.turn_id,
                "resume_token": card.resume_token,
            }
            started = await client.post("/credential-response", json={**body, "action": "signing_in"})
            assert started.status_code == 200

            reloaded = (await client.get("/history", params=params, headers=headers)).json()
            [restored] = reloaded["pending_credential_requests"]
            assert (restored["reason"], restored["signing_in"], restored["credential_binding"]) == (
                "credential_rejected_by_site",
                True,
                "matched_by_saved_url",
            )
            assert restored["sign_in_browser_session_id"] == "pbs_debug"

            done = await client.post("/credential-response", json={**body, "action": "signed_in"})
            result = await asyncio.wait_for(invocation, 1)
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    assert done.json()["result"] == "signed_in"
    assert (result["status"], result["browser_profile_id"]) == ("signed_in", "bp_signed_in")
    assert ctx.credential_pause_outcome == "signed_in"
    assert ctx.request_policy.credential_pause_signed_in_profile_id == "bp_signed_in"


@pytest.mark.asyncio
async def test_done_without_card_recovery_still_resumes_and_a_skip_after_no_sign_in_still_works(
    sqlite_engine, monkeypatch
):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "create_profile_from_running_session", AsyncMock(return_value=(None, 0)))
    emitted = _sign_in_card_context(ctx, recovery=False)
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            card = ctx.stream.send.await_args.args[0]
            body = {
                "workflow_copilot_chat_id": card.workflow_copilot_chat_id,
                "turn_id": card.turn_id,
                "resume_token": card.resume_token,
            }
            nothing = await client.post("/credential-response", json={**body, "action": "signed_in"})
            assert nothing.json()["result"] == "no_sign_in_found"
            skipped = await client.post("/credential-response", json={**body, "action": "skip"})
            assert skipped.status_code == 200
            result = await asyncio.wait_for(invocation, 1)
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    assert result["status"] == "skipped"
    assert ctx.request_policy.credential_pause_signed_in_profile_id is None


@both_recovery_headers
@pytest.mark.asyncio
async def test_a_card_after_a_settled_sign_in_restores_alone_and_only_its_own_token_answers_it(
    recovery_header: str, sqlite_engine, monkeypatch
):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        routes, "create_profile_from_running_session", AsyncMock(return_value=(_signed_in_profile(), 2))
    )
    emitted = _sign_in_card_context(ctx, recovery=True)
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    headers = {recovery_header: "a" * 64}
    params = {"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id}
    cards = []
    results = []
    async with client:
        for card_number in (1, 2):
            emitted.clear()
            invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
            try:
                await asyncio.wait_for(emitted.wait(), 1)
                card = ctx.stream.send.await_args.args[0]
                cards.append(card)
                body = {
                    "workflow_copilot_chat_id": card.workflow_copilot_chat_id,
                    "turn_id": card.turn_id,
                    "resume_token": card.resume_token,
                }
                if card_number == 2:
                    reloaded = (await client.get("/history", params=params, headers=headers)).json()
                    pending = [request["resume_token"] for request in reloaded["pending_credential_requests"]]
                    assert pending == [card.resume_token]
                    stale = await client.post(
                        "/credential-response",
                        json={**body, "resume_token": cards[0].resume_token, "action": "skip"},
                    )
                    assert stale.status_code == 403
                    assert not invocation.done()
                done = await client.post("/credential-response", json={**body, "action": "signed_in"})
                assert done.json()["result"] == "signed_in"
                results.append(await asyncio.wait_for(invocation, 1))
            finally:
                if not invocation.done():
                    invocation.cancel()
                    await asyncio.gather(invocation, return_exceptions=True)

    assert cards[0].resume_token != cards[1].resume_token
    assert [result["status"] for result in results] == ["signed_in", "signed_in"]
    assert ctx.credential_pause_outcome == "signed_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_fails", [False, True], ids=["delete-succeeds", "delete-fails"])
async def test_a_done_whose_pause_ended_while_saving_discards_the_profile(sqlite_engine, monkeypatch, delete_fails):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    cache = _FakeCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    emitted = _sign_in_card_context(ctx, recovery=True)
    invocation: asyncio.Task | None = None

    profiles = BrowserSessionsRepository(BaseAlchemyDB(sqlite_engine).Session)
    monkeypatch.setattr(app.DATABASE, "browser_sessions", profiles)
    profile = await profiles.create_browser_profile("org", "Sign-in", browser_profile_id="bp_signed_in")
    profile_key = ("org", profile.browser_profile_id)
    stored_profiles = {profile_key}
    delete_attempts: list[tuple[str, str, bool]] = []

    async def save_while_the_turn_is_cancelled(**_kwargs):
        assert invocation is not None
        invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
        return profile, 2

    async def delete_browser_profile(organization_id: str, profile_id: str, *, hard_delete: bool) -> None:
        delete_attempts.append((organization_id, profile_id, hard_delete))
        if delete_fails:
            raise OSError("synthetic object deletion failure")
        stored_profiles.discard((organization_id, profile_id))

    monkeypatch.setattr(routes, "create_profile_from_running_session", save_while_the_turn_is_cancelled)
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(delete_browser_profile=delete_browser_profile))
    client._transport.app.add_api_route("/credential-response", routes.ember_credential_response, methods=["POST"])
    async with client:
        invocation = asyncio.create_task(_request_credential("https://portal.example.com/login", "Login", ctx))
        await asyncio.wait_for(emitted.wait(), 1)
        card = ctx.stream.send.await_args.args[0]
        done = await client.post(
            "/credential-response",
            json={
                "workflow_copilot_chat_id": card.workflow_copilot_chat_id,
                "turn_id": card.turn_id,
                "resume_token": card.resume_token,
                "action": "signed_in",
            },
        )

    assert done.status_code == 409
    assert await profiles.get_browser_profile(profile.browser_profile_id, "org", include_deleted=True) is None
    assert delete_attempts == [(*profile_key, True)]
    assert stored_profiles == ({profile_key} if delete_fails else set())
    response_key = pauses.credential_response_cache_key("org", card.workflow_copilot_chat_id, card.turn_id)
    assert response_key not in cache.store


@both_recovery_headers
@pytest.mark.asyncio
async def test_registration_card_survives_a_reload_with_its_one_generate_attempt_recorded(
    recovery_header: str, sqlite_engine, monkeypatch
):
    _, client, ctx, _ = await setup_question_chat(sqlite_engine, monkeypatch)
    monkeypatch.setattr(app, "CACHE", _FakeCache())
    monkeypatch.setattr(pauses, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "RECONCILE_ABANDON_AFTER_SECONDS", -1)
    monkeypatch.setattr(app.DATABASE, "credentials", SimpleNamespace(get_credentials=AsyncMock(return_value=[])))
    emitted = _sign_in_card_context(ctx, recovery=True)
    ctx.client_supports_credential_generation = True
    recovery_token = "a" * 64
    vault = _RegistrationVault(_make_credential(), fail=True)
    vault.install(monkeypatch)
    recovered_while_saving: list[list] = []
    fail_store = vault.store

    async def store_and_reload(*args):
        recovered_while_saving.append(
            await pauses.pending_credential_requests("org", ctx.workflow_copilot_chat_id, ["turn"], recovery_token)
        )
        return await fail_store(*args)

    monkeypatch.setattr(routes, "store_new_credential", store_and_reload)
    api = client._transport.app
    api.dependency_overrides[routes.org_auth_service.get_current_org_for_credential_routes] = api.dependency_overrides[
        routes.org_auth_service.get_current_org
    ]
    api.dependency_overrides[routes.org_auth_service.get_current_user_id_or_none] = lambda: "user-1"
    api.add_api_route("/credential-generate", routes.ember_credential_generate, methods=["POST"])

    async def history() -> list[dict]:
        response = await client.get(
            "/history",
            params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id},
            headers={recovery_header: recovery_token},
        )
        return response.json()["pending_credential_requests"]

    async with client:
        invocation = asyncio.create_task(
            _request_credential(
                "https://portal.example.com/signup",
                "Create the test account",
                ctx,
                registration=CredentialRegistration(**_REGISTRATION),
            )
        )
        try:
            await asyncio.wait_for(emitted.wait(), 1)
            [raised] = await history()
            assert raised["reason"] == "credential_registration"
            assert raised["registration"]["credential_name"] == "Portal test account"
            assert raised["registration"]["attempted"] is False
            body = {
                "workflow_copilot_chat_id": raised["workflow_copilot_chat_id"],
                "turn_id": raised["turn_id"],
                "resume_token": raised["resume_token"],
            }

            with skyvern_context.scoped(SkyvernContext()):
                generated = await client.post("/credential-generate", json=body)

            assert generated.json()["result"] == "unknown"
            [[saving]] = recovered_while_saving
            assert saving.registration is not None and saving.registration.attempted is True
            [reopened] = await history()
            assert reopened["registration"]["attempted"] is True
            assert reopened["registration"]["outcome"] == "unknown"
            with skyvern_context.scoped(SkyvernContext()):
                assert (await client.post("/credential-generate", json=body)).status_code == 409
            assert len(vault.passwords) == 1
            assert not invocation.done()
        finally:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
