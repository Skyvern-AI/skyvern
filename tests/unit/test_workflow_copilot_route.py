"""End-to-end route tests for workflow_copilot_chat_post.

Covers the three scenarios the debated plan requires:

1. Flag off -> old-copilot path runs, new-copilot is not reached.
2. Flag on, successful turn -> new-copilot handler runs and does not
   trigger the restore-on-error branch.
3. Flag on, mid-stream failure -> ``_restore_workflow_definition`` is
   awaited so a half-persisted draft is rolled back.

These tests exercise the dispatcher and stream-handler wiring in
``skyvern/forge/sdk/routes/workflow_copilot.py`` without reaching a
real database -- all DB / LLM / agent surfaces are patched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.routes.workflow_copilot import COPILOT_V2_FLAG_KEY, workflow_copilot_chat_post
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatRequest,
    WorkflowCopilotStreamErrorUpdate,
    WorkflowCopilotStreamResponseUpdate,
)


def _make_chat_request() -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-request",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        message="Please update it",
        workflow_yaml="title: Example",
    )


def _install_fake_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Capture the stream handler that the route hands to EventSourceStream."""
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create(request: object, handler: object, ping_interval: int = 10) -> object:
        del request, ping_interval
        captured["handler"] = handler
        return sentinel

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.FastAPIEventSourceStream.create",
        fake_create,
    )
    captured["sentinel"] = sentinel
    return captured


def _install_mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: bool | None = None,
    side_effect: BaseException | None = None,
) -> AsyncMock:
    mock_provider = AsyncMock()
    if side_effect is not None:
        mock_provider.is_feature_enabled_cached.side_effect = side_effect
    else:
        mock_provider.is_feature_enabled_cached.return_value = return_value
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", mock_provider)
    return mock_provider


@pytest.mark.asyncio
async def test_flag_off_dispatches_to_old_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off -> workflow_copilot_chat_post must use the old-copilot stream handler.

    We verify by patching _new_copilot_chat_post to something that would
    raise if called, then confirming the old path was used instead.
    """
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    # force_stub_app's _LazyNamespace auto-creates truthy AsyncMocks for any attribute
    # access, so the provider needs an explicit False stub to keep this test accurate.
    _install_mock_provider(monkeypatch, return_value=False)

    new_copilot_mock = AsyncMock(side_effect=AssertionError("new-copilot path must not run when flag is off"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = _install_fake_create(monkeypatch)

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_dispatches_to_new_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on -> workflow_copilot_chat_post delegates to _new_copilot_chat_post."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()


def _setup_new_copilot_mocks(
    monkeypatch: pytest.MonkeyPatch,
    chat: SimpleNamespace,
    original_workflow: SimpleNamespace,
    agent_result: SimpleNamespace,
) -> AsyncMock:
    """Wire up everything the new-copilot stream handler touches.

    Returns the restore-on-error mock so callers can assert on it.
    """

    async def fake_llm_handler(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.get_llm_handler_for_prompt_type",
        fake_llm_handler,
    )

    restore_mock = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._restore_workflow_definition",
        restore_mock,
    )

    run_agent_mock = AsyncMock(return_value=agent_result)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.run_copilot_agent",
        run_agent_mock,
    )

    # DB surfaces: the new-copilot handler reaches the repository directly via
    # app.DATABASE.workflow_params.*  and app.DATABASE.workflows.*  -- mock
    # those attribute chains.
    app.DATABASE.workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 14, tzinfo=timezone.utc))
        ),
    )
    app.DATABASE.workflows = SimpleNamespace(
        get_workflow_by_permanent_id=AsyncMock(return_value=original_workflow),
    )
    app.DATABASE.observer = SimpleNamespace(
        get_workflow_run_blocks=AsyncMock(return_value=[]),
    )
    app.AGENT_FUNCTION.get_copilot_security_rules = MagicMock(return_value="")

    return restore_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto_accept", "workflow_was_persisted", "has_valid_proposal", "expect_restore"),
    [
        # auto_accept + valid proposal => keep the DB write (frontend applied it)
        (True, True, True, False),
        # auto_accept + no proposal (SKY-9143) => restore the unverified mid-turn write
        (True, True, False, True),
        # Nothing was persisted => nothing to restore regardless of other flags
        (False, False, False, False),
        # Normal mid-stream disconnect with a persisted draft and no proposal => restore
        (False, True, False, True),
        # Normal mid-stream disconnect with a persisted draft and a valid proposal =>
        # still restore, user accepts via the panel to re-apply
        (False, True, True, True),
    ],
)
async def test_flag_on_mid_stream_disconnect_restores_when_persisted_and_not_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    workflow_was_persisted: bool,
    has_valid_proposal: bool,
    expect_restore: bool,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = _install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=auto_accept,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump"]) if has_valid_proposal else None
    if proposal is not None:
        proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=False,
        unvalidated=False,
    )

    restore_mock = _setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    # First call (before agent loop) -> False, second call (after agent loop) -> True
    # simulates a mid-stream client disconnect after the agent returned.
    stream.is_disconnected = AsyncMock(side_effect=[False, True])

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    if expect_restore:
        restore_mock.assert_awaited_once()
    else:
        restore_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "auto_accept",
        "workflow_was_persisted",
        "has_valid_proposal",
        "prior_proposal",
        "clear_proposed_flag",
        "expect_clear_call",
    ),
    [
        # Restore-and-clear: persisted draft, no proposal, stale prior.
        (False, True, False, {"workflow_id": "stale"}, False, True),
        # Restore fires but nothing stale to clear.
        (False, True, False, None, False, False),
        # Chat-only turn with no clear flag must not touch a prior proposal.
        (False, False, False, {"workflow_id": "stale"}, False, False),
        # New valid proposal stores via the if-branch, not the clear-branch.
        (False, True, True, {"workflow_id": "stale"}, False, False),
        # auto_accept=True default paths: nothing to write.
        (True, True, False, None, False, False),
        (True, True, True, {"workflow_id": "stale"}, False, False),
        # Agent ran run_blocks (persisted=True) then ASK_QUESTIONed with the
        # clear flag set: restore AND clear fire in the same turn.
        (False, True, False, {"workflow_id": "stale"}, True, True),
        # auto_accept=True restore-driven clear: a stale proposal that survived
        # an auto-accept toggle gets nulled when the assistant invalidates it.
        (True, True, False, {"workflow_id": "stale"}, False, True),
        # The clear flag nulls the stale proposal under both auto_accept values
        # even when nothing was persisted this turn.
        (False, False, False, {"workflow_id": "stale"}, True, True),
        (True, False, False, {"workflow_id": "stale"}, True, True),
        # No prior proposal => no DB write even when the clear flag is set.
        (False, False, False, None, True, False),
        (True, False, False, None, True, False),
        # auto_accept=True turn with a stale UNVALIDATED proposal clears it
        # via the third elif (no clear flag, no restore needed).
        (True, False, False, {"workflow_id": "stale", "_copilot_unvalidated": True}, False, True),
        (True, True, True, {"workflow_id": "stale", "_copilot_unvalidated": True}, False, True),
    ],
)
async def test_proposed_workflow_cleared_on_restore(
    monkeypatch: pytest.MonkeyPatch,
    auto_accept: bool,
    workflow_was_persisted: bool,
    has_valid_proposal: bool,
    prior_proposal: dict | None,
    clear_proposed_flag: bool,
    expect_clear_call: bool,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = _install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=prior_proposal,
        auto_accept=auto_accept,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump"]) if has_valid_proposal else None
    if proposal is not None:
        proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="done",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml=None,
        workflow_was_persisted=workflow_was_persisted,
        clear_proposed_workflow=clear_proposed_flag,
        unvalidated=False,
    )

    _setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    clear_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is None]

    if expect_clear_call:
        assert len(clear_calls) == 1, f"expected a proposed_workflow=None clear, got {update_calls!r}"
    else:
        assert not clear_calls, f"did not expect a clear call, got {update_calls!r}"

    # The FE's auto_accept code path reads the SSE payload, not
    # chat.proposed_workflow, so the payload must mirror agent_result.
    response_frames = [
        call.args[0]
        for call in stream.send.await_args_list
        if isinstance(call.args[0], WorkflowCopilotStreamResponseUpdate)
    ]
    assert len(response_frames) == 1, f"expected exactly one RESPONSE frame, got {response_frames!r}"
    expected_payload_workflow = proposal.model_dump.return_value if has_valid_proposal else None
    assert response_frames[0].updated_workflow == expected_payload_workflow


@pytest.mark.asyncio
async def test_unvalidated_timeout_wip_overrides_auto_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)

    captured = _install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=True,
    )
    original_workflow = SimpleNamespace(
        workflow_id="wf-canonical",
        title="Original",
        description="Original description",
        workflow_definition=None,
    )
    proposal = MagicMock(spec=["model_dump"])
    proposal.model_dump.return_value = {"workflow_id": "wf-canonical"}
    agent_result = SimpleNamespace(
        user_response="I ran out of time before I could finish testing.",
        updated_workflow=proposal,
        global_llm_context=None,
        workflow_yaml="title: WIP",
        workflow_was_persisted=True,
        clear_proposed_workflow=False,
        unvalidated=True,
        total_tokens=42,
        response_type="REPLY",
    )

    restore_mock = _setup_new_copilot_mocks(monkeypatch, chat, original_workflow, agent_result)

    request = MagicMock()
    request.headers = {"x-api-key": "sk-test-key"}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    sent_frames: list[object] = []
    stream = MagicMock()

    async def capture_send(payload: object) -> bool:
        sent_frames.append(payload)
        return True

    stream.send = capture_send
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    restore_mock.assert_awaited_once()

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    proposal_writes = [c for c in update_calls if c.kwargs.get("proposed_workflow") is not None]
    assert len(proposal_writes) == 1
    proposed_data = proposal_writes[0].kwargs["proposed_workflow"]
    assert proposed_data.get("_copilot_unvalidated") is True
    assert proposed_data.get("_copilot_yaml") == "title: WIP"

    response_frame = next(
        (f for f in sent_frames if getattr(f, "type", None) and str(f.type).endswith("response")),
        None,
    )
    assert response_frame is not None
    assert getattr(response_frame, "unvalidated", False) is True
    assert not [f for f in sent_frames if isinstance(f, WorkflowCopilotStreamErrorUpdate)]


@pytest.mark.asyncio
async def test_env_on_short_circuits_posthog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var True must skip the PostHog check entirely and route to v2."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", True)
    mock_provider = _install_mock_provider(
        monkeypatch,
        side_effect=AssertionError("PostHog must not be consulted when env var is True"),
    )

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()
    mock_provider.is_feature_enabled_cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_off_posthog_on_uses_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var False + PostHog True -> v2 path, with org_id as distinct_id."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    mock_provider = _install_mock_provider(monkeypatch, return_value=True)

    sentinel = object()
    new_copilot_mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-abc")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is sentinel
    new_copilot_mock.assert_awaited_once()
    mock_provider.is_feature_enabled_cached.assert_awaited_once_with(
        COPILOT_V2_FLAG_KEY,
        distinct_id="org-abc",
        properties={"organization_id": "org-abc"},
    )


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider errors (PostHog down, DB hiccup) must not break the endpoint."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, side_effect=RuntimeError("posthog unreachable"))

    new_copilot_mock = AsyncMock(side_effect=AssertionError("v2 path must not run when provider errors"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = _install_fake_create(monkeypatch)

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_off_posthog_off_uses_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var False + PostHog False -> legacy stream handler, v2 not reached."""
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    mock_provider = _install_mock_provider(monkeypatch, return_value=False)

    new_copilot_mock = AsyncMock(side_effect=AssertionError("v2 path must not run when both gates are off"))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._new_copilot_chat_post",
        new_copilot_mock,
    )

    captured = _install_fake_create(monkeypatch)

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)

    assert response is captured["sentinel"]
    new_copilot_mock.assert_not_awaited()
    mock_provider.is_feature_enabled_cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_path_persists_copilot_yaml_on_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy V1 path stashes _copilot_yaml so /apply-proposed-workflow can re-create the version.

    Regression for #10568 + SKY-9206: Accept on the frontend now hits
    /apply-proposed-workflow, which 400s when _copilot_yaml is missing from the
    proposal. V2 sets it (line 921); V1 must too or non-V2 users can never accept.
    """
    monkeypatch.setattr(settings, "ENABLE_WORKFLOW_COPILOT_V2", False)
    _install_mock_provider(monkeypatch, return_value=False)

    captured = _install_fake_create(monkeypatch)

    chat = SimpleNamespace(
        workflow_copilot_chat_id="chat-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        proposed_workflow=None,
        auto_accept=False,
    )

    proposal = MagicMock(spec=["model_dump"])
    proposal.model_dump.return_value = {"workflow_id": "wf-canonical", "title": "Updated"}

    workflow_yaml = "title: Updated\nworkflow_definition:\n  blocks: []\n"

    copilot_call_llm_mock = AsyncMock(return_value=("ok", proposal, None, workflow_yaml))
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot.copilot_call_llm",
        copilot_call_llm_mock,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.routes.workflow_copilot._get_debug_run_info",
        AsyncMock(return_value=None),
    )

    app.DATABASE.workflow_params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        get_workflow_copilot_chat_messages=AsyncMock(return_value=[]),
        update_workflow_copilot_chat=AsyncMock(),
        create_workflow_copilot_chat_message=AsyncMock(
            return_value=SimpleNamespace(created_at=datetime(2026, 4, 28, tzinfo=timezone.utc))
        ),
    )

    request = MagicMock()
    request.headers = {}
    organization = SimpleNamespace(organization_id="org-1")

    response = await workflow_copilot_chat_post(request, _make_chat_request(), organization)
    assert response is captured["sentinel"]

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=False)

    handler = captured["handler"]
    assert callable(handler)
    await handler(stream)

    update_calls = app.DATABASE.workflow_params.update_workflow_copilot_chat.await_args_list
    persist_calls = [c for c in update_calls if c.kwargs.get("proposed_workflow") is not None]
    assert len(persist_calls) == 1, f"expected exactly one proposed_workflow persist, got {update_calls!r}"

    persisted = persist_calls[0].kwargs["proposed_workflow"]
    assert isinstance(persisted, dict)
    assert persisted.get("_copilot_yaml") == workflow_yaml, (
        "legacy V1 path must stash the LLM-emitted YAML on the proposal so "
        "/apply-proposed-workflow can re-create the workflow version"
    )
