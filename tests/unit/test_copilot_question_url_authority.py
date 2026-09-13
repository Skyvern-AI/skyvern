from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.copilot import credential_pause as credential_pause_module
from skyvern.forge.sdk.copilot.ask_user import AskUserArguments, QuestionInteraction, ask_user
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.request_policy import (
    QuestionResponseSiteURLSource,
    RequestPolicy,
    UserMessageSiteURLSource,
    _ground_user_provided_sites,
)
from skyvern.forge.sdk.copilot.tools.credential_fill import (
    _credential_fill_origin_grant,
    _request_credential,
)
from skyvern.forge.sdk.copilot.tools.discovery import _user_provided_entry_url
from skyvern.forge.sdk.routes.workflow_copilot import _make_error_narrative_payload, _persist_turn_messages
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
    WorkflowCopilotStreamMessageType,
)
from tests.unit.test_copilot_ask_user import setup_question_chat
from tests.unit.test_copilot_credential_pause import (
    _FakeCache,
    _make_credential,
    _make_stream,
)

_LOGIN_URL = "https://portal.example.com/login"


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["text", "answer_text"])
async def test_authenticated_question_url_reaches_the_real_card_and_rebuilds_from_history(
    sqlite_engine, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    repo, client, ctx, frames = await setup_question_chat(sqlite_engine, monkeypatch)
    ctx.request_policy = RequestPolicy()
    _ground_user_provided_sites(ctx.request_policy, "Repair the login workflow", [])

    async with client:
        waiting = asyncio.create_task(
            ask_user(
                ctx,
                AskUserArguments.model_validate({"parts": [{"prompt": "Paste the sign-in URL"}]}),
                "url-call",
            )
        )
        try:
            required = await asyncio.wait_for(frames.get(), 5)
            question = required["interactions"][0]
            body = {
                "workflow_copilot_chat_id": ctx.workflow_copilot_chat_id,
                "interaction_id": question["interaction_id"],
            }
            if form == "text":
                body["text"] = _LOGIN_URL
            else:
                body["answers"] = [{"part_id": question["parts"][0]["part_id"], "text": _LOGIN_URL}]

            accepted = await client.post("/reply", json=body)
            assert accepted.status_code == 200
            duplicate = await client.post("/reply", json={**body, "text": "https://retry.example.net"})
            assert duplicate.json() == accepted.json()
            await asyncio.wait_for(waiting, 5)

            source = ctx.request_policy.user_site_url_sources[_LOGIN_URL]
            assert source == QuestionResponseSiteURLSource(interaction_id=question["interaction_id"])
            assert ctx.request_policy.user_provided_site_urls == [_LOGIN_URL]
            assert _user_provided_entry_url(ctx) == _LOGIN_URL

            chat = await repo.get_workflow_copilot_chat_by_id("org", ctx.workflow_copilot_chat_id)
            await _persist_turn_messages(
                chat=chat,
                turn_id="turn",
                user_message="Repair the login workflow",
                audio_artifact_id=None,
                user_row_already_persisted=True,
                sender=WorkflowCopilotChatSender.USER,
                assistant_content="Response received",
                global_llm_context=None,
                turn_outcome=None,
                narrative_payload=None,
            )
            saved = await client.get("/history", params={"workflow_copilot_chat_id": ctx.workflow_copilot_chat_id})
            persisted_history = [
                WorkflowCopilotChatHistoryMessage.model_validate(message) for message in saved.json()["chat_history"]
            ]
            rebuilt = RequestPolicy()
            _ground_user_provided_sites(rebuilt, "", persisted_history)
            assert rebuilt.user_provided_site_urls == ctx.request_policy.user_provided_site_urls
            assert rebuilt.user_site_url_sources == ctx.request_policy.user_site_url_sources

            cache = _FakeCache()
            cache.store[
                credential_pause_module.credential_response_cache_key("org", ctx.workflow_copilot_chat_id, "turn")
            ] = credential_pause_module.encode_credential_response("connected", "cred_1")
            monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
            monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
            monkeypatch.setattr(
                app,
                "DATABASE",
                SimpleNamespace(
                    credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()]))
                ),
            )
            ctx.client_supports_credential_pause = True
            ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
            ctx.stream = _make_stream()

            card_result = await _request_credential(_LOGIN_URL, "Login required", ctx)

            assert card_result["status"] == "connected"
            assert card_result["credential_id"] == "cred_1"
            card = ctx.stream.send.await_args_list[0].args[0]
            assert card.type is WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED
            assert card.login_page_urls == [_LOGIN_URL]
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)


def test_projection_preserves_first_origin_order_and_ignores_non_response_text() -> None:
    interaction = QuestionInteraction.model_validate(
        {
            "interaction_id": "interaction-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
            "parts": [
                {
                    "part_id": "part-1",
                    "prompt": "Prompt https://prompt.example.net",
                    "choices": [{"choice_id": "choice-1", "text": "https://choice.example.net"}],
                },
                {"part_id": "part-2", "prompt": "Second", "choices": []},
            ],
            "status": "resolved",
            "response": {
                "text": "https://question.example.com/top",
                "answers": [
                    {"part_id": "part-1", "choice_id": "choice-1", "text": None},
                    {"part_id": "part-2", "text": "https://answer.example.org/path"},
                ],
                "skipped": False,
            },
            "created_at": datetime.now(UTC),
            "resolved_at": datetime.now(UTC),
        }
    )
    skipped = interaction.model_copy(
        update={
            "interaction_id": "interaction-skipped",
            "response": interaction.response.model_copy(
                update={"text": "https://skipped.example.net", "skipped": True}
            ),
        }
    )
    unresolved = interaction.model_copy(
        update={"interaction_id": "interaction-pending", "status": "pending", "response": None}
    )
    interrupted = interaction.model_copy(
        update={"interaction_id": "interaction-interrupted", "status": "interrupted", "response": None}
    )
    redacted = interaction.model_copy(
        update={
            "interaction_id": "interaction-redacted",
            "response": interaction.response.model_copy(update={"text": "[REDACTED]", "answers": []}),
        }
    )
    narrative_payload = _make_error_narrative_payload("turn-1", None, "done")
    narrative_payload["questionInteractions"] = [
        interaction.model_dump(mode="json"),
        skipped.model_dump(mode="json"),
        unresolved.model_dump(mode="json"),
        interrupted.model_dump(mode="json"),
        redacted.model_dump(mode="json"),
    ]
    history = [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="Start at https://first.example.com/a",
            created_at=datetime.now(UTC),
        ),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.PRODUCT,
            content="https://product.example.net",
            created_at=datetime.now(UTC),
        ),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI,
            content="https://assistant.example.net",
            narrative_payload=narrative_payload,
            created_at=datetime.now(UTC),
        ),
    ]
    policy = RequestPolicy()

    _ground_user_provided_sites(
        policy,
        "Repeat https://question.example.com/other then https://last.example.edu/report",
        history,
    )

    assert policy.user_provided_site_urls == [
        "https://first.example.com/a",
        "https://question.example.com/top",
        "https://answer.example.org/path",
        "https://last.example.edu/report",
    ]
    assert policy.user_site_url_sources == {
        "https://first.example.com/a": UserMessageSiteURLSource(message_index=1),
        "https://question.example.com/top": QuestionResponseSiteURLSource(interaction_id="interaction-1"),
        "https://answer.example.org/path": QuestionResponseSiteURLSource(interaction_id="interaction-1"),
        "https://last.example.edu/report": UserMessageSiteURLSource(message_index=2),
    }


def test_immediate_question_projection_appends_without_replacing_composer_urls() -> None:
    policy = RequestPolicy(
        user_provided_site_urls=["https://first.example.com/login"],
        user_site_url_sources={
            "https://first.example.com/login": UserMessageSiteURLSource(message_index=1),
        },
    )
    interaction = QuestionInteraction.model_validate(
        {
            "interaction_id": "interaction-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
            "parts": [{"part_id": "part-1", "prompt": "Backup URL?", "choices": []}],
            "status": "resolved",
            "response": {"text": "https://second.example.net/login", "skipped": False},
            "created_at": datetime.now(UTC),
            "resolved_at": datetime.now(UTC),
        }
    )

    policy.project_question_response_sites(interaction)

    assert policy.user_provided_site_urls == [
        "https://first.example.com/login",
        "https://second.example.net/login",
    ]
    assert policy.user_site_url_sources["https://first.example.com/login"] == UserMessageSiteURLSource(message_index=1)
    assert policy.user_site_url_sources["https://second.example.net/login"] == QuestionResponseSiteURLSource(
        interaction_id="interaction-1"
    )


def test_question_response_with_detected_raw_secret_grants_no_url_authority() -> None:
    policy = RequestPolicy()
    interaction = QuestionInteraction.model_validate(
        {
            "interaction_id": "interaction-1",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
            "parts": [{"part_id": "part-1", "prompt": "Sign-in URL?", "choices": []}],
            "status": "resolved",
            "response": {
                "text": "https://portal.example.com/login",
                "skipped": False,
                "raw_secret_detected": True,
            },
            "created_at": datetime.now(UTC),
            "resolved_at": datetime.now(UTC),
        }
    )

    policy.project_question_response_sites(interaction)

    assert policy.user_provided_site_urls == []
    assert policy.user_site_url_sources == {}
    assert policy.raw_secret_detected is True
    assert policy.raw_secret_safety_status == "detected"
    assert policy.testing_intent == "skip_test"
    assert policy.allow_run_blocks is False
    assert policy.allow_missing_credentials_in_draft is True
    assert policy.credential_draft_deferred_explicitly is True


@pytest.mark.asyncio
async def test_question_source_reaches_fill_boundary_without_weakening_origin_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = QuestionResponseSiteURLSource(interaction_id="interaction-1")
    policy = RequestPolicy(
        resolved_credentials=[SimpleNamespace(credential_id="cred_1", tested_url=None)],
        current_turn_named_credential_ids={"cred_1"},
        user_provided_site_urls=[_LOGIN_URL],
        user_site_url_sources={_LOGIN_URL: source},
    )
    ctx = SimpleNamespace(
        organization_id="org",
        request_policy=policy,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        org_credentials_for_turn=None,
        vault_login_uris_by_credential_id={},
        persisted_workflow_yaml="",
    )
    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.credential_fill._live_working_page_url",
            AsyncMock(return_value=_LOGIN_URL),
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.credential_fill._vault_named_sites",
            AsyncMock(return_value=[]),
        ),
        capture_logs() as logs,
    ):
        grant, error = await _credential_fill_origin_grant(ctx, "cred_1")

    assert error is None
    assert grant is not None
    event = next(log for log in logs if log.get("event") == "copilot credential fill grant")
    assert event["source_kind"] == "question_response"
    assert event["source_interaction_id"] == "interaction-1"
    assert "source_user_message" not in event
    assert "/login" not in str(event)

    with patch(
        "skyvern.forge.sdk.copilot.tools.credential_fill._live_working_page_url",
        AsyncMock(return_value="https://unrelated.example.net/login"),
    ):
        unrelated_grant, unrelated_error = await _credential_fill_origin_grant(ctx, "cred_1")
    assert unrelated_grant is None
    assert unrelated_error is not None


def test_malformed_persisted_question_record_cannot_supply_origin() -> None:
    payload = _make_error_narrative_payload("turn", None, "done")
    payload["questionInteractions"] = [
        {"interaction_id": "malformed", "status": "resolved", "response": {"text": _LOGIN_URL}}
    ]
    history = [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI, content="", narrative_payload=payload, created_at=datetime.now(UTC)
        )
    ]
    policy = RequestPolicy()
    _ground_user_provided_sites(policy, "", history)
    assert policy.user_provided_site_urls == []
