"""Tests for the copilot mid-build credential pause-and-wait (SKY-12138).

Covers:

- ``credential_pause_reason`` fires only on the three typed mid-build signals
  and never on ``credential_prompt_reason``'s text-marker tier.
- ``maybe_credential_pause``'s waiter: connect mutates ``RequestPolicy`` and
  resolves, skip leaves the policy untouched, timeout/disconnect/no-cache
  degrade to None without sending a frame, an invalid/foreign credential id
  degrades rather than crashing, and CancelledError always propagates.
- ``run_with_enforcement`` loop integration: a typed signal at finalize sends
  exactly one ``credential_required`` frame and re-enters the loop with the
  resume message, instead of returning on the first finalize.
- Pause time is credited back against ``TOTAL_TIMEOUT_SECONDS`` so a slow
  pause doesn't trip the total-timeout on the resumed iteration.
- The one-pause-per-turn latch and kill-switch-off parity.
- The ``/workflow/copilot/credential-response`` route's validation and TTL.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import RunConfig
from fastapi import HTTPException, status

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.cache.base import NoopLock
from skyvern.forge.sdk.copilot import credential_pause as credential_pause_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import RequestPolicyGuardrailInputs, _derive_turn_intent_on_context
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_pause import (
    CredentialPauseRejection,
    CredentialPauseResolution,
    _encode_active_pause,
    credential_pause_active_key,
    credential_response_cache_key,
    encode_credential_response,
    maybe_credential_pause,
    preflight_credential_pause,
    resolve_credential_pause,
)
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    RepairRootCauseIdentity,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import (
    NUDGE_SENTINEL,
    _check_enforcement,
    _elapsed_run_seconds,
    run_with_enforcement,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CREDENTIAL_PROMPT_CLARIFICATION_REASONS,
    RequestPolicy,
    credential_prompt_reason,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode
from skyvern.forge.sdk.routes.workflow_copilot import (
    WorkflowCopilotCredentialResponseRequest,
    workflow_copilot_credential_response,
)
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType
from tests.unit.conftest import make_copilot_context


def _repair_contract(
    next_action: Any,
    failure_type: Any = DiagnosisFailureType.UNKNOWN,
    categories: tuple[str, ...] = ("CREDENTIAL_ERROR",),
) -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(
            suspected_failure_type=failure_type,
            root_cause_identity=RepairRootCauseIdentity(failure_categories=categories),
        ),
        repair_decision=RepairDecision(next_action=next_action),
        verification_result=VerificationResult(),
    )


def _make_credential(credential_id: str = "cred_1", name: str = "Example Login") -> Credential:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Credential(
        credential_id=credential_id,
        organization_id="org-1",
        name=name,
        vault_type=CredentialVaultType.SKYVERN,
        item_id="item_1",
        credential_type=CredentialType.PASSWORD,
        username="user@example.com",
        card_last4=None,
        card_brand=None,
        created_at=now,
        modified_at=now,
    )


class _FakeCache:
    """Minimal in-memory double of the ``get`` / ``set`` / ``get_lock`` surface of app.CACHE.

    ``is_shared = True`` by default (stands in for Redis); flip an instance's
    to False to simulate a LocalCache-shaped non-shared cache.
    """

    is_shared = True

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any, Any]] = []

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: Any = None) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex))

    def get_lock(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> NoopLock:
        return NoopLock(lock_name, blocking_timeout, timeout)


def _seed_active_pause(
    cache: _FakeCache,
    org: str,
    chat: str,
    turn: str,
    token: str,
    *,
    expires_at: datetime | None = None,
) -> None:
    """Write a pending active-pause record the way maybe_credential_pause would."""
    cache.store[credential_pause_active_key(org, chat, turn)] = _encode_active_pause(
        token, expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5))
    )


def _make_stream(*, disconnected: bool = False) -> MagicMock:
    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(return_value=disconnected)
    return stream


def _fake_result() -> MagicMock:
    result = MagicMock()
    result.final_output = "Done."
    result.new_items = []
    result.to_input_list.return_value = []
    return result


# ---------------------------------------------------------------------------
# 1 - credential_pause_reason detector
# ---------------------------------------------------------------------------


def test_reason_fires_on_skipped_unbound_credentials() -> None:
    ctx = SimpleNamespace(last_run_skipped_unbound_credentials=True)
    assert credential_pause_module.credential_pause_reason(ctx) == "workflow_credential_inputs_unbound"


def test_reason_ignores_skipped_unbound_credentials_when_user_explicitly_skipped_testing() -> None:
    """_request_policy_allows_update_and_skip_run (guardrails.py) skips the run for a
    skip_test-deferred credential too -- pausing here would contradict the same explicit
    request the credential_deferred_draft branch already excludes skip_test for."""
    policy = RequestPolicy(testing_intent="skip_test")
    ctx = SimpleNamespace(last_run_skipped_unbound_credentials=True, request_policy=policy)
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_fires_on_missing_credential_run_failure() -> None:
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=_repair_contract(
            RepairNextAction.ASK, DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
        ),
    )
    assert credential_pause_module.credential_pause_reason(ctx) == "missing_credential_run_failure"


def test_reason_does_not_fire_when_ask_is_for_a_different_failure_type() -> None:
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=_repair_contract(RepairNextAction.ASK, DiagnosisFailureType.UNKNOWN),
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_ignores_missing_credential_or_init_when_not_categorized_as_credential_error() -> None:
    """MISSING_CREDENTIAL_OR_INIT also covers param-binding/lookup failures; only the
    CREDENTIAL_ERROR category should preempt with a card that can't unblock those."""
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=_repair_contract(
            RepairNextAction.ASK,
            DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
            categories=("PARAMETER_BINDING_ERROR",),
        ),
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_fires_on_credential_deferred_draft() -> None:
    policy = RequestPolicy(credential_draft_deferred_explicitly=True)
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=None,
        request_policy=policy,
        update_workflow_called=True,
    )
    assert credential_pause_module.credential_pause_reason(ctx) == "credential_deferred_draft"


def test_reason_requires_update_workflow_called_for_deferred_draft() -> None:
    policy = RequestPolicy(credential_draft_deferred_explicitly=True)
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=None,
        request_policy=policy,
        update_workflow_called=False,
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_ignores_deferred_draft_when_user_explicitly_skipped_testing() -> None:
    """skip_test means the user already said not to run/verify this -- pausing to
    ask for a credential would contradict that explicit request."""
    policy = RequestPolicy(credential_draft_deferred_explicitly=True, testing_intent="skip_test")
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=None,
        request_policy=policy,
        update_workflow_called=True,
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_ignores_text_marker_tier_that_credential_prompt_reason_catches() -> None:
    """Pins the SKY-11988 false-positive lesson: no text-marker fallback here."""
    policy = RequestPolicy()
    final_text = "I couldn't test this. Please add the credential via the Credentials UI."

    # The sibling function DOES classify this via its text-marker tier.
    assert credential_prompt_reason(policy, final_text) == "assistant_directed"

    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=None,
        request_policy=policy,
        update_workflow_called=True,
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


def test_reason_none_when_no_signals_present() -> None:
    ctx = make_copilot_context()
    assert credential_pause_module.credential_pause_reason(ctx) is None


# ---------------------------------------------------------------------------
# 2 - maybe_credential_pause waiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_action_mutates_policy_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    credential = _make_credential()
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is not None
    assert ctx.credential_pause_outcome == "connected"
    assert ctx.request_policy.resolved_credentials == [credential]
    assert ctx.request_policy.allow_run_blocks is True
    assert ctx.request_policy.clarification_reason == "none"
    assert ctx.request_policy.allow_missing_credentials_in_draft is False
    assert ctx.request_policy.requires_user_clarification is False
    resume_text = resume_msgs[-1]["content"]
    assert resume_text.startswith(NUDGE_SENTINEL)
    assert "cred_1" in resume_text
    sent_types = [call.args[0].type for call in stream.send.await_args_list]
    assert sent_types == [WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED]


@pytest.mark.asyncio
async def test_connected_action_unlatches_test_after_update_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skipped run before the pause already stamped test_after_update_done=True;
    a connected resume must reset it so a reply that never re-runs the blocks still
    gets forced back through update_and_run_blocks by the post_update nudge."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()]))
        ),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    resume_msgs = await maybe_credential_pause(
        ctx,
        _fake_result(),
        _make_stream(),
        CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5),
    )

    assert resume_msgs is not None
    assert ctx.test_after_update_done is False


@pytest.mark.asyncio
async def test_skip_action_leaves_policy_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-2"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-2")] = encode_credential_response("skip", None)
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is not None
    assert ctx.credential_pause_outcome == "skipped"
    assert ctx.request_policy.resolved_credentials == []
    assert ctx.request_policy == RequestPolicy()
    assert "chose not to connect a credential now" in resume_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_skip_clears_stale_last_test_ok_from_the_diagnosed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing_credential_run_failure pause means the diagnosed run left
    last_test_ok=False; skip must clear it or the resumed reply is intercepted
    by the generic failed-test nudge instead of honoring the skip decision."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-skip-clears"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_test_ok = False
    ctx.test_after_update_done = True
    ctx.latest_diagnosis_repair_contract = _repair_contract(
        RepairNextAction.ASK, DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    )
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-skip-clears")] = encode_credential_response(
        "skip", None
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is not None
    assert ctx.last_test_ok is None
    assert _check_enforcement(ctx, result=None, config=config) is None


@pytest.mark.asyncio
async def test_timeout_returns_none_and_marks_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()  # never populated
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=0)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    assert ctx.credential_pause_outcome == "timeout"
    assert ctx.copilot_credential_pause_seconds >= 0.0


@pytest.mark.asyncio
async def test_response_racing_in_at_the_timeout_instant_is_rescued(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: resolve_credential_pause writes under a per-turn lock,
    but the timeout invalidate wrote the same active-pause key unlocked -- a
    response that lands in the same instant the waiter gives up would validate
    against the still-pending record (204 to the client) yet never get read,
    since the waiter already returned None. The invalidate must re-check under
    the same lock and use the response if one raced in, not silently drop it."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-race"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    async def fake_wait_races_in_a_response(response_key: str, ctx: Any, stream: Any, timeout_seconds: int) -> None:
        # Simulates resolve_credential_pause's lock-protected write landing in
        # the same instant the waiter's own poll loop concludes None.
        cache.store[response_key] = encode_credential_response("skip", None)
        return None

    monkeypatch.setattr(credential_pause_module, "_wait_for_credential_response", fake_wait_races_in_a_response)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is not None
    assert ctx.credential_pause_outcome == "skipped"


@pytest.mark.asyncio
async def test_client_disconnect_mid_wait_degrades_early(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()  # never populated
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    # Connected for the initial guard and the post-send re-check, gone by the first poll.
    stream.is_disconnected = AsyncMock(side_effect=[False, False, True])
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    start = time.monotonic()
    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)
    elapsed = time.monotonic() - start

    assert resume_msgs is None
    assert ctx.credential_pause_outcome == "timeout"
    assert elapsed < 1.0  # degraded on the first poll, not the full 30s timeout


@pytest.mark.asyncio
async def test_disconnect_racing_the_send_itself_declines_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: send() returns True even when the client is already
    gone (its own contract), so a disconnect that lands between the pre-send
    guard and delivery would otherwise wait out the full timeout for a card
    nobody ever saw, and land on 'timeout' instead of 'declined'."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-race-send"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    # Connected for the pre-send guard, gone by the time send() returns.
    stream.is_disconnected = AsyncMock(side_effect=[False, True])
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    start = time.monotonic()
    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)
    elapsed = time.monotonic() - start

    assert resume_msgs is None
    assert ctx.credential_pause_outcome == "declined"
    assert elapsed < 1.0  # never entered the wait loop


@pytest.mark.asyncio
async def test_already_disconnected_before_send_declines_without_sending_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predictor (credential_pause_would_fire) excludes the async-only
    disconnect check by design -- a client gone before the frame is even sent
    must latch and tag 'declined' (not 'timeout') so the caller can fall back
    to a normal nudge instead of treating this like a delivered-and-waited pause."""
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    stream = _make_stream(disconnected=True)
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    assert ctx.credential_pause_outcome == "declined"
    assert ctx.credential_pause_used is True
    stream.send.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_invalidates_the_active_pause_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disconnect degrades well before expires_at; a late POST after the tab
    reconnects must be rejected instead of silently writing an unread response."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-disconnect"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    stream.is_disconnected = AsyncMock(side_effect=[False, True])
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)
    assert resume_msgs is None

    frame = stream.send.await_args_list[0].args[0]
    with pytest.raises(CredentialPauseRejection) as excinfo:
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id="turn-disconnect",
            resume_token=frame.resume_token,
            action="skip",
            credential_id=None,
        )
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_no_cache_returns_none_without_sending_a_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app._inst, "CACHE", None, raising=False)
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_error_propagates_and_still_accumulates_pause_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-cancel"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    async def _raise_cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(credential_pause_module.asyncio, "sleep", _raise_cancelled)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    with pytest.raises(asyncio.CancelledError):
        await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert ctx.copilot_credential_pause_seconds > 0.0
    assert ctx.credential_pause_outcome is None  # neither timeout nor resolved path ran

    frame = stream.send.await_args_list[0].args[0]
    with pytest.raises(CredentialPauseRejection) as excinfo:
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id="turn-cancel",
            resume_token=frame.resume_token,
            action="skip",
            credential_id=None,
        )
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_unexpected_error_in_waiter_also_invalidates_the_active_pause_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not just CancelledError: any unexpected failure in the wait loop leaves the
    frame's resume token live client-side, so the record must be invalidated too."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-crash"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    async def _raise_runtime_error(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cache connection reset")

    monkeypatch.setattr(credential_pause_module.asyncio, "sleep", _raise_runtime_error)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    with pytest.raises(RuntimeError):
        await maybe_credential_pause(ctx, _fake_result(), stream, config)

    frame = stream.send.await_args_list[0].args[0]
    with pytest.raises(CredentialPauseRejection) as excinfo:
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id="turn-crash",
            resume_token=frame.resume_token,
            action="skip",
            credential_id=None,
        )
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_invalid_credential_id_degrades_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-3"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-3")] = encode_credential_response(
        "connected", "cred_foreign"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[]))),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    assert ctx.credential_pause_outcome == "timeout"
    assert ctx.request_policy.resolved_credentials == []


@pytest.mark.asyncio
async def test_full_round_trip_binds_frame_token_to_active_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: establishing the pause writes an active record + one-time token,
    the response endpoint resolves only with that token, and the resolved pause is
    consumed so a replay conflicts.
    """
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-rt"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    active_key = credential_pause_active_key("org-1", "chat-1", "turn-rt")

    async def _respond_using_frame_token() -> None:
        # Read the one-time token exactly as the FE would from the frame, then resolve.
        for _ in range(2000):
            raw = await cache.get(active_key)
            record = json.loads(raw) if raw else None
            if record and record.get("status") == "pending":
                await resolve_credential_pause(
                    cache,
                    organization_id="org-1",
                    workflow_copilot_chat_id="chat-1",
                    turn_id="turn-rt",
                    resume_token=record["resume_token"],
                    action="skip",
                    credential_id=None,
                )
                return
            await asyncio.sleep(0.001)

    responder = asyncio.ensure_future(_respond_using_frame_token())
    try:
        resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)
    finally:
        await responder

    assert resume_msgs is not None
    assert ctx.credential_pause_outcome == "skipped"
    frame = stream.send.await_args_list[0].args[0]
    assert frame.resume_token

    with pytest.raises(CredentialPauseRejection) as excinfo:
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id="turn-rt",
            resume_token=frame.resume_token,
            action="skip",
            credential_id=None,
        )
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT


# ---------------------------------------------------------------------------
# 5 - one-pause-per-turn latch / 6 - kill-switch-off parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latch_prevents_second_pause_this_turn() -> None:
    ctx = make_copilot_context()
    ctx.credential_pause_used = True
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_off_returns_none_without_touching_cache_or_stream() -> None:
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=False)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    stream.send.assert_not_awaited()
    stream.is_disconnected.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3 - run_with_enforcement loop integration (regression pin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_pauses_at_finalize_and_resumes_same_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: without the pause hook this returns on the first finalize."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-loop"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-loop")] = encode_credential_response(
        "skip", None
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    fake_result = _fake_result()
    calls: list[dict[str, Any]] = []

    def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return fake_result

    async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
        return None

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", fake_stream_to_sse)

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
        run_config=RunConfig(),
        copilot_config=config,
    )

    assert returned is fake_result
    assert len(calls) == 2, "second Runner invocation must happen after the pause resumes"
    second_call_input = calls[1]["input"]
    assert any(NUDGE_SENTINEL in item.get("content", "") for item in second_call_input)
    assert ctx.credential_pause_used is True
    frame_types = [call.args[0].type for call in stream.send.await_args_list]
    assert frame_types.count(WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED) == 1


@pytest.mark.asyncio
async def test_credential_pause_preempts_a_concurrent_synthesized_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: the finalize branch only tried the pause when nudge AND
    synthesized_msg were both None, so a reopened synthesized-block offer coinciding
    with a credential diagnosis sent the offer instead of the credential_required frame.
    """
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-offer-race"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-offer-race")] = encode_credential_response(
        "skip", None
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    fake_result = _fake_result()
    calls: list[dict[str, Any]] = []

    def fake_synthesized_offer(_ctx: Any) -> dict[str, Any] | None:
        # _check_enforcement runs after the Runner call, so `calls` already has
        # 1 entry on the pre-pause finalize check; gone by the post-resume check
        # (2 entries), so the test isolates the preemption itself.
        return {"role": "user", "content": "offer to add the reopened block"} if len(calls) <= 1 else None

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement._maybe_synthesized_block_offer_msg",
        fake_synthesized_offer,
    )

    def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return fake_result

    async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
        return None

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", fake_stream_to_sse)

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
        run_config=RunConfig(),
        copilot_config=config,
    )

    frame_types = [call.args[0].type for call in stream.send.await_args_list]
    assert frame_types.count(WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED) == 1
    # On old code the offer nudge wins the first check (pause only fires once the
    # offer stops recurring), costing an extra Runner call before the skip resume
    # text appears -- this pins that the pause wins on the very first check.
    assert len(calls) == 2
    second_call_input = calls[1]["input"]
    assert any("chose not to connect a credential now" in item.get("content", "") for item in second_call_input)


@pytest.mark.asyncio
async def test_declined_pause_falls_back_to_the_normal_nudge_instead_of_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails on old code: an already-disconnected stream made maybe_credential_pause
    return None with nothing sent, and run_with_enforcement finalized the turn as-is
    instead of the post_update nudge this iteration would otherwise have gotten."""
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.update_workflow_called = True
    ctx.test_after_update_done = False
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    stream = _make_stream(disconnected=True)
    fake_result = _fake_result()
    calls: list[dict[str, Any]] = []

    def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return fake_result

    async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
        return None

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", fake_stream_to_sse)

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

    await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
        run_config=RunConfig(),
        copilot_config=config,
    )

    assert ctx.credential_pause_outcome == "declined"
    stream.send.assert_not_called()
    # On old code this returns after the first call (nudge=None, pause declined,
    # synthesized_msg=None -> immediate finalize). The fallback must fire the
    # post_update nudge instead; MAX_POST_UPDATE_NUDGES allows retries before the
    # fake (which never "tests" anything) exhausts them, so >1 call is the claim.
    assert len(calls) > 1, "the post_update nudge must fire instead of an immediate finalize"
    second_call_input = calls[1]["input"]
    assert any("did not test it" in item.get("content", "") for item in second_call_input)


# ---------------------------------------------------------------------------
# 4 - timeout credit
# ---------------------------------------------------------------------------


def test_elapsed_run_seconds_subtracts_pause_time() -> None:
    ctx = SimpleNamespace(copilot_credential_pause_seconds=50.0)
    start_time = time.monotonic() - 60.0

    elapsed = _elapsed_run_seconds(ctx, start_time)

    assert 9.0 < elapsed < 11.0


@pytest.mark.asyncio
async def test_paused_loop_does_not_trip_total_timeout_on_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: a slow pause would consume the (tiny) real timeout budget."""
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.15)

    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-credit"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.2)

    async def _populate_after_first_poll() -> None:
        await asyncio.sleep(0.25)
        cache.store[credential_response_cache_key("org-1", "chat-1", "turn-credit")] = encode_credential_response(
            "skip", None
        )

    stream = _make_stream()
    fake_result = _fake_result()
    calls: list[dict[str, Any]] = []

    def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return fake_result

    async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
        return None

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", fake_stream_to_sse)

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    populate_task = asyncio.ensure_future(_populate_after_first_poll())
    try:
        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
            run_config=RunConfig(),
            copilot_config=config,
        )
    finally:
        await populate_task

    assert returned is fake_result
    assert len(calls) == 2, "pause time must be credited so the resumed iteration isn't timed out"
    assert ctx.copilot_total_timeout_exceeded is False


# ---------------------------------------------------------------------------
# 7 - /workflow/copilot/credential-response route
# ---------------------------------------------------------------------------


def _response_request(
    *,
    turn_id: str = "turn-1",
    chat_id: str = "chat-1",
    resume_token: str = "tok-1",
    action: str = "skip",
    credential_id: str | None = None,
) -> WorkflowCopilotCredentialResponseRequest:
    return WorkflowCopilotCredentialResponseRequest(
        turn_id=turn_id,
        workflow_copilot_chat_id=chat_id,
        resume_token=resume_token,
        action=action,  # type: ignore[arg-type]
        credential_id=credential_id,
    )


@pytest.mark.asyncio
async def test_route_503_when_cache_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app._inst, "CACHE", None, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)
    assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_route_204_writes_flag_for_skip_without_credential_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    result = await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)

    assert result is None
    expected_key = credential_response_cache_key("org-1", "chat-1", "turn-1")
    assert cache.store[expected_key] == encode_credential_response("skip", None)
    response_set = next(call for call in cache.set_calls if call[0] == expected_key)
    assert response_set[1] == encode_credential_response("skip", None)
    assert response_set[2] == timedelta(seconds=settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS + 300)


@pytest.mark.asyncio
async def test_route_422_when_connected_without_credential_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(_response_request(action="connected"), organization=organization)
    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_route_404_when_credential_unknown_or_foreign_org(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[]))),
    )
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(
            _response_request(action="connected", credential_id="cred_other_org"),
            organization=organization,
        )
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_route_204_writes_flag_for_connected_with_valid_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()]))
        ),
    )
    organization = SimpleNamespace(organization_id="org-1")

    result = await workflow_copilot_credential_response(
        _response_request(action="connected", credential_id="cred_1"),
        organization=organization,
    )

    assert result is None
    expected_key = credential_response_cache_key("org-1", "chat-1", "turn-1")
    assert cache.store[expected_key] == encode_credential_response("connected", "cred_1")


@pytest.mark.asyncio
async def test_route_404_when_no_active_pause_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid-looking token can't resolve a turn that was never paused."""
    cache = _FakeCache()  # no active-pause record seeded
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert credential_response_cache_key("org-1", "chat-1", "turn-1") not in cache.store


@pytest.mark.asyncio
async def test_route_403_when_resume_token_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked/guessed turn+chat id without the frame's token is rejected."""
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-real")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(
            _response_request(action="skip", resume_token="tok-wrong"),
            organization=organization,
        )
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert credential_response_cache_key("org-1", "chat-1", "turn-1") not in cache.store


@pytest.mark.asyncio
async def test_route_rejects_bad_token_before_looking_up_credential_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad token must not reach the credential-id DB lookup -- org auth alone
    can't authorize this turn, and doing the lookup first turns a wrong-token
    response into a small authenticated credential-id existence oracle."""
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-real")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    lookup = AsyncMock(return_value=[_make_credential(credential_id="cred_real")])
    monkeypatch.setattr(app, "DATABASE", SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=lookup)))
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(
            _response_request(action="connected", credential_id="cred_real", resume_token="tok-wrong"),
            organization=organization,
        )
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_route_404_when_chat_id_does_not_match_the_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause is keyed by chat+turn: a response with the right turn but a foreign chat misses it."""
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(
            _response_request(action="skip", chat_id="chat-foreign"),
            organization=organization,
        )
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_route_409_on_replay_after_first_accepted_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """First accepted response consumes the pause; a replay of the same POST is rejected."""
    cache = _FakeCache()
    _seed_active_pause(cache, "org-1", "chat-1", "turn-1", "tok-1")
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    first = await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)
    assert first is None

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_route_410_when_response_arrives_after_the_frame_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The record's Redis TTL outlives the wait window for grace on a slow poll,
    but a response after the deadline the frame told the client about must still
    be rejected -- the waiter already gave up and the turn finalized without it."""
    cache = _FakeCache()
    _seed_active_pause(
        cache, "org-1", "chat-1", "turn-1", "tok-1", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    monkeypatch.setattr(app._inst, "CACHE", cache, raising=False)
    organization = SimpleNamespace(organization_id="org-1")

    with pytest.raises(HTTPException) as excinfo:
        await workflow_copilot_credential_response(_response_request(action="skip"), organization=organization)
    assert excinfo.value.status_code == status.HTTP_410_GONE
    assert credential_response_cache_key("org-1", "chat-1", "turn-1") not in cache.store


@pytest.mark.asyncio
async def test_resolve_credential_pause_rejections_are_typed() -> None:
    """resolve_credential_pause raises CredentialPauseRejection (not HTTPException) so it stays route-agnostic."""
    cache = _FakeCache()

    with pytest.raises(CredentialPauseRejection) as excinfo:
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id="turn-1",
            resume_token="tok-1",
            action="skip",
            credential_id=None,
        )
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# A diagnosed missing-credential run failure must pre-empt the generic
# failed-test nudge, or the pause hook (gated on nudge is None) never runs on
# the turn's first credential ask.
# ---------------------------------------------------------------------------


def _failed_test_ctx() -> CopilotContext:
    """A real CopilotContext (all enforcement fields defaulted) with just the
    failed-test + missing-credential-diagnosis fields set, so _check_enforcement
    doesn't AttributeError on some unrelated field this hand-picked set omits.
    """
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_test_ok = False
    ctx.test_after_update_done = True
    ctx.latest_diagnosis_repair_contract = _repair_contract(
        RepairNextAction.ASK, DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    )
    ctx.request_policy = RequestPolicy()
    return ctx


def test_missing_credential_failure_suppresses_generic_failed_test_nudge_when_pause_enabled() -> None:
    """Fails on old code: the generic 'post_failed_test' nudge fires here instead."""
    ctx = _failed_test_ctx()
    config = CopilotConfig(credential_pause_enabled=True)

    nudge = _check_enforcement(ctx, result=None, config=config)

    assert nudge is None
    assert ctx.failed_test_nudge_count == 0


def test_missing_credential_failure_still_nudges_when_kill_switch_off() -> None:
    """Flag-off parity: unrelated to the fix, the generic nudge still fires as before."""
    ctx = _failed_test_ctx()
    config = CopilotConfig(credential_pause_enabled=False)

    nudge = _check_enforcement(ctx, result=None, config=config)

    assert nudge is not None
    assert ctx.failed_test_nudge_count == 1


def test_missing_credential_failure_nudges_normally_once_pause_already_used() -> None:
    ctx = _failed_test_ctx()
    ctx.credential_pause_used = True
    config = CopilotConfig(credential_pause_enabled=True)

    nudge = _check_enforcement(ctx, result=None, config=config)

    assert nudge is not None
    assert ctx.failed_test_nudge_count == 1


def _skipped_run_ctx() -> CopilotContext:
    """Defensive fixture: even if test_after_update_done were False after a
    skipped run (in the real runtime it isn't -- streaming_adapter's
    _update_enforcement_from_tool sets it True unconditionally for
    update_and_run_blocks/run_blocks_and_collect_debug regardless of skip;
    see test_workflow_credential_inputs_unbound_skip_does_not_nudge_post_update
    for the real-state pin), the pause must still pre-empt the post_update
    nudge, a second nudge that can race the pause hook.
    """
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.update_workflow_called = True
    ctx.test_after_update_done = False
    ctx.request_policy = RequestPolicy()
    return ctx


def test_skipped_run_suppresses_post_update_nudge_when_pause_enabled() -> None:
    """Fails on old code: the earlier 'post_update' nudge fires instead."""
    ctx = _skipped_run_ctx()
    config = CopilotConfig(credential_pause_enabled=True)

    nudge = _check_enforcement(ctx, result=None, config=config)

    assert nudge is None


def test_skipped_run_still_nudges_post_update_when_kill_switch_off() -> None:
    ctx = _skipped_run_ctx()
    config = CopilotConfig(credential_pause_enabled=False)

    nudge = _check_enforcement(ctx, result=None, config=config)

    assert nudge is not None
    assert "updated the workflow but did not test it" in nudge


def test_workflow_credential_inputs_unbound_skip_does_not_nudge_post_update() -> None:
    """Pins the real post-skip state (not the defensive _skipped_run_ctx fixture):
    _update_enforcement_from_tool sets test_after_update_done=True unconditionally
    for update_and_run_blocks, even on the credential-unbound skip branch, so the
    post_update nudge condition (not test_after_update_done) is already False by
    the time enforcement runs -- verified via a direct call to that function with
    the real skip_result shape from tools/__init__.py's skip branch."""
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.request_policy = RequestPolicy()
    config = CopilotConfig(credential_pause_enabled=True)

    assert _check_enforcement(ctx, result=None, config=config) is None

    ctx.credential_pause_used = True
    assert _check_enforcement(ctx, result=None, config=config) is None


@pytest.mark.asyncio
async def test_missing_credential_run_failure_pauses_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop-level pin for the same fix, through the real finalize seam."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-nudge-race"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_test_ok = False
    ctx.test_after_update_done = True
    ctx.latest_diagnosis_repair_contract = _repair_contract(
        RepairNextAction.ASK, DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    )
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-nudge-race")] = encode_credential_response(
        "skip", None
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    fake_result = _fake_result()
    calls: list[dict[str, Any]] = []

    def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return fake_result

    async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
        return None

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", fake_stream_to_sse)

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
        run_config=RunConfig(),
        copilot_config=config,
    )

    assert returned is fake_result
    # If maybe_credential_pause didn't clear the stale last_test_ok=False on
    # skip, the resumed reply would be intercepted by the failed-test nudge
    # for a 3rd Runner call instead of finalizing here.
    assert len(calls) == 2
    frame_types = [call.args[0].type for call in stream.send.await_args_list]
    assert frame_types.count(WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED) == 1


# ---------------------------------------------------------------------------
# last_run_skipped_unbound_credentials must reflect the MOST RECENT
# update_and_run_blocks call, not "ever skipped this turn" — otherwise a
# later successful call leaves a stale True and pauses an already-passing turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_run_skipped_flag_clears_on_a_later_successful_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: the flag stays True after the second, successful call."""
    ctx = make_copilot_context()
    ctx.turn_intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx.request_policy = RequestPolicy()

    workflow_yaml = "workflow_definition:\n  parameters: []\n  blocks:\n  - block_type: code\n    label: step_one\n"

    async def fake_prior_definition(update_ctx: Any) -> object:
        return None

    async def fake_update_workflow(payload: dict, update_ctx: Any, **kwargs: object) -> dict:
        workflow = SimpleNamespace(workflow_definition={"blocks": [{"label": "step_one"}]})
        update_ctx.last_workflow = workflow
        update_ctx.last_update_block_count = 1
        return {"ok": True, "_workflow": workflow, "data": {"block_count": 1}}

    async def fake_run_blocks(params: dict, run_ctx: Any, **kwargs: object) -> dict:
        return {"ok": True, "data": {"workflow_run_id": "wr-1", "overall_status": "completed", "blocks": []}}

    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_update_and_run_blocks_composition_evidence_precheck", lambda *a, **k: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", fake_prior_definition)
    monkeypatch.setattr(tools_module, "_update_workflow", fake_update_workflow)
    monkeypatch.setattr(tools_module, "_plan_frontier", lambda *args: (["step_one"], {}, "step_one"))
    monkeypatch.setattr(tools_module, "_frontier_run_size_error", lambda *args: None)
    monkeypatch.setattr(tools_module, "_run_blocks_and_collect_debug", fake_run_blocks)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "enqueue_screenshot_from_result", lambda *args, **kwargs: None)

    call_args = json.dumps({"workflow_yaml": workflow_yaml, "block_labels": ["step_one"], "parameters": {}})

    # Call 1: policy forces a skip (unbound credential).
    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: True)
    result_1 = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"), call_args
    )
    assert json.loads(result_1)["data"]["skip_reason"] == "workflow_credential_inputs_unbound"
    assert ctx.last_run_skipped_unbound_credentials is True

    # Call 2: credential now bound, policy allows the real run.
    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: False)
    result_2 = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"), call_args
    )
    assert json.loads(result_2)["ok"] is True
    assert ctx.last_run_skipped_unbound_credentials is False
    assert credential_pause_module.credential_pause_reason(ctx) is None


@pytest.mark.asyncio
async def test_last_run_skipped_flag_stays_false_when_update_workflow_fails_before_skip_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails on old code: the flag was set True from the policy check alone,
    before _update_workflow ever ran, so an unrelated authoring failure got
    misreported as a credential ask.
    """
    ctx = make_copilot_context()
    ctx.turn_intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx.request_policy = RequestPolicy()
    workflow_yaml = "workflow_definition:\n  parameters: []\n  blocks:\n  - block_type: code\n    label: step_one\n"

    async def fake_prior_definition(update_ctx: Any) -> object:
        return None

    async def failing_update_workflow(payload: dict, update_ctx: Any, **kwargs: object) -> dict:
        return {"ok": False, "error": "workflow_yaml is not valid: bad block reference"}

    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_update_and_run_blocks_composition_evidence_precheck", lambda *a, **k: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", fake_prior_definition)
    monkeypatch.setattr(tools_module, "_update_workflow", failing_update_workflow)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)
    # The policy would allow a skip if we got that far — but we never do.
    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: True)

    result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps({"workflow_yaml": workflow_yaml, "block_labels": ["step_one"], "parameters": {}}),
    )

    assert json.loads(result)["ok"] is False
    assert ctx.last_run_skipped_unbound_credentials is False
    assert credential_pause_module.credential_pause_reason(ctx) is None


# ---------------------------------------------------------------------------
# _copilot_seconds_remaining must credit pause time, or a long pause late in
# the turn FORBIDS the very test the connected credential was for
# (blockers.py's _late_block_running_call_signal).
# ---------------------------------------------------------------------------


def test_copilot_seconds_remaining_credits_pause_time() -> None:
    from skyvern.forge.sdk.copilot.tools._shared import _copilot_seconds_remaining

    ctx = SimpleNamespace(copilot_run_start_monotonic=time.monotonic() - 1000.0, copilot_credential_pause_seconds=300.0)

    remaining = _copilot_seconds_remaining(ctx)

    # Uncredited: 900 - 1000 = -100s (would forbid). Credited: 900 - 700 = 200s.
    assert remaining is not None
    assert remaining > 150.0


def test_late_block_running_call_signal_allows_call_after_credited_pause() -> None:
    """Fails on old code: a 300s pause 700s into the turn wrongly forbids the
    resumed test run because the wall-clock check never saw the pause credit."""
    from skyvern.forge.sdk.copilot.tools.blockers import _late_block_running_call_signal

    ctx = SimpleNamespace(
        copilot_run_start_monotonic=time.monotonic() - 1000.0,
        copilot_credential_pause_seconds=300.0,
        last_failed_workflow_yaml=None,
        last_good_workflow_yaml=None,
    )

    assert _late_block_running_call_signal(ctx, "update_and_run_blocks") is None


def test_late_block_running_call_signal_forbids_same_elapsed_without_pause_credit() -> None:
    """Sanity pair for the test above: the identical wall-clock elapsed DOES
    forbid the call when there was no pause to credit, proving the crediting
    -- not something else -- is what keeps the resumed call allowed."""
    from skyvern.forge.sdk.copilot.tools.blockers import _late_block_running_call_signal

    ctx = SimpleNamespace(
        copilot_run_start_monotonic=time.monotonic() - 1000.0,
        copilot_credential_pause_seconds=0.0,
        last_failed_workflow_yaml=None,
        last_good_workflow_yaml=None,
    )

    assert _late_block_running_call_signal(ctx, "update_and_run_blocks") is not None


# ---------------------------------------------------------------------------
# A client that can't render credential_required must never be paused for,
# or it stares at "Working..." until the timeout.
# ---------------------------------------------------------------------------


def test_would_fire_false_when_client_does_not_support_pause() -> None:
    ctx = make_copilot_context()
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    config = CopilotConfig(credential_pause_enabled=True)

    assert credential_pause_module.credential_pause_would_fire(ctx, config) is False


def test_would_fire_true_once_client_support_is_set() -> None:
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    config = CopilotConfig(credential_pause_enabled=True)

    assert credential_pause_module.credential_pause_would_fire(ctx, config) is True


@pytest.mark.asyncio
async def test_unsupported_client_never_pauses_even_with_everything_else_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails on old code: no client-support guard existed, so a pre-12139 FE
    build (or the env flag on with an unwired client) got paused and the FE
    silently dropped the unknown frame -- a guaranteed ~timeout-length hang."""
    ctx = make_copilot_context()
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    # ctx.client_supports_credential_pause left at its False default.

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    stream.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# A successful connect must clear credential_draft_deferred_explicitly, or
# credential_prompt_reason() keeps stamping credentialPrompt right next to
# credentialPause: connected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_clears_credential_draft_deferred_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: the flag survives a successful connect, so the FE
    would see a contradictory connect-CTA right after the user connected."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-connect-clear"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy(credential_draft_deferred_explicitly=True)

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-connect-clear")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    credential = _make_credential()
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert ctx.request_policy.credential_draft_deferred_explicitly is False
    assert credential_prompt_reason(ctx.request_policy, "any final text") is None


@pytest.mark.asyncio
async def test_skip_leaves_credential_draft_deferred_explicitly_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip/timeout correctly keep the flag -- only a successful connect clears it."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-skip-keep"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy(credential_draft_deferred_explicitly=True)

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-skip-keep")] = encode_credential_response(
        "skip", None
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert ctx.request_policy.credential_draft_deferred_explicitly is True


# ---------------------------------------------------------------------------
# The poller must resolve before checking disconnect, or an already-arrived
# response (connect-then-refresh) is discarded as a timeout instead of being
# read.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_resolves_before_checking_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: is_disconnected() was checked first in the poll loop,
    so a response that arrives during the sleep window is discarded as a
    timeout once the client has since disconnected."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-race"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.1)

    async def _populate_during_sleep() -> None:
        await asyncio.sleep(0.02)
        cache.store[credential_response_cache_key("org-1", "chat-1", "turn-race")] = encode_credential_response(
            "skip", None
        )

    stream = MagicMock()
    stream.send = AsyncMock(return_value=True)
    # Connected for the top-level guard and the post-send re-check; "gone" for
    # every poll-loop check after.
    stream.is_disconnected = AsyncMock(side_effect=[False, False] + [True] * 10)
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    populate_task = asyncio.ensure_future(_populate_during_sleep())
    try:
        resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)
    finally:
        await populate_task

    assert resume_msgs is not None
    assert ctx.credential_pause_outcome == "skipped"


# ---------------------------------------------------------------------------
# The pause must gate on a SHARED cache, not merely a non-None one -- a
# same-process-only cache (LocalCache) guarantees a hang in a multi-worker
# deployment since the poller and the POST can land on different workers.
# ---------------------------------------------------------------------------


def test_local_cache_is_not_shared() -> None:
    from skyvern.forge.sdk.cache.local import LocalCache

    assert LocalCache().is_shared is False


@pytest.mark.asyncio
async def test_non_shared_cache_never_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: `cache is None` was the only cache guard, so a
    same-process-only cache (LocalCache in a multi-worker OSS deployment)
    passed it and produced a guaranteed hang."""
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    cache.is_shared = False
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is None
    stream.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# CredentialPauseResolution sanity
# ---------------------------------------------------------------------------


def test_credential_pause_resolution_defaults_credential_to_none() -> None:
    resolution = CredentialPauseResolution(action="skip")
    assert resolution.credential is None


def _blocked_login_policy() -> RequestPolicy:
    """A policy shaped the way build_request_policy leaves an unresolved login ask."""
    return RequestPolicy(
        login_intent=True,
        clarification_reason="login_credentials_unresolved",
        clarification_question="Connect a saved credential to sign in.",
        requires_user_clarification=True,
        user_response_policy="ask_clarification",
        allow_update_workflow=False,
        allow_run_blocks=False,
    )


def _preflight_ctx() -> CopilotContext:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.request_policy = _blocked_login_policy()
    return ctx


def test_reason_fires_on_unresolved_login_credentials() -> None:
    ctx = make_copilot_context()
    ctx.request_policy = _blocked_login_policy()

    assert credential_pause_module.credential_pause_reason(ctx) == "login_credentials_unresolved"


def test_reason_does_not_fire_for_a_login_ask_that_resolved_to_a_credential() -> None:
    ctx = make_copilot_context()
    ctx.request_policy = RequestPolicy(login_intent=True, clarification_reason="none")

    assert credential_pause_module.credential_pause_reason(ctx) is None


@pytest.mark.asyncio
async def test_preflight_connect_binds_the_credential_and_clears_the_clarification_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _preflight_ctx()
    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    credential = _make_credential()
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resolution = await preflight_credential_pause(ctx, stream, config)

    assert resolution is not None and resolution.action == "connected"
    assert ctx.credential_pause_outcome == "connected"
    policy = ctx.request_policy
    assert policy.resolved_credentials == [credential]
    assert policy.allow_run_blocks is True
    assert policy.clarification_reason == "none"
    assert policy.user_response_policy == "proceed"
    assert policy.allow_update_workflow is True
    assert policy.requires_user_clarification is False
    sent_types = [call.args[0].type for call in stream.send.await_args_list]
    assert sent_types == [WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED]


@pytest.mark.asyncio
async def test_preflight_connect_preserves_an_explicit_defer_authoring_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _preflight_ctx()
    ctx.request_policy.authoring_intent = "defer_authoring"
    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    credential = _make_credential()
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resolution = await preflight_credential_pause(ctx, _make_stream(), config)

    assert resolution is not None and resolution.action == "connected"
    policy = ctx.request_policy
    assert policy.user_response_policy == "proceed"
    assert policy.resolved_credentials == [credential]
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False


def _preflight_policy_inputs() -> RequestPolicyGuardrailInputs:
    return RequestPolicyGuardrailInputs(
        user_message="Log in to https://portal.example.com/login and download the invoices.",
        workflow_yaml="",
        chat_history_text="",
        chat_history_messages=[],
        global_llm_context="",
        organization_id="org-1",
        request_policy_handler=None,
        turn_intent_handler=None,
    )


@pytest.mark.asyncio
async def test_preflight_connect_re_derives_run_authority_onto_the_turn_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _preflight_ctx()
    policy_inputs = _preflight_policy_inputs()
    _derive_turn_intent_on_context(ctx, ctx.request_policy, policy_inputs)
    assert ctx.turn_intent.mode is TurnIntentMode.CLARIFY
    assert ctx.turn_intent.authority.may_update_workflow is False

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[_make_credential()]))
        ),
    )
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resolution = await preflight_credential_pause(ctx, _make_stream(), config)
    assert resolution is not None and resolution.action == "connected"
    _derive_turn_intent_on_context(ctx, ctx.request_policy, policy_inputs)

    assert ctx.request_policy.clarification_reason == "none"
    assert ctx.turn_intent.mode is not TurnIntentMode.CLARIFY
    assert ctx.turn_intent.authority.may_update_workflow is True
    assert ctx.turn_intent.authority.may_run_blocks is True


@pytest.mark.asyncio
async def test_preflight_skip_clears_the_reason_so_the_declined_turn_shows_no_card_cta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale login_credentials_unresolved would still route through
    CREDENTIAL_PROMPT_CLARIFICATION_REASONS and stamp a credential CTA on the
    reply for a turn the user explicitly declined."""
    ctx = _preflight_ctx()
    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response("skip", None)
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resolution = await preflight_credential_pause(ctx, stream, config)

    assert resolution is not None and resolution.action == "skip"
    policy = ctx.request_policy
    assert policy.clarification_reason == "none"
    assert policy.clarification_reason not in CREDENTIAL_PROMPT_CLARIFICATION_REASONS
    assert policy.requires_user_clarification is False
    assert policy.allow_missing_credentials_in_draft is True
    assert policy.user_response_policy == "proceed"
    assert policy.allow_update_workflow is True


@pytest.mark.asyncio
async def test_preflight_timeout_leaves_the_clarification_block_for_the_terminal_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _preflight_ctx()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", _FakeCache(), raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=0)

    resolution = await preflight_credential_pause(ctx, stream, config)

    assert resolution is None
    assert ctx.credential_pause_outcome == "timeout"
    policy = ctx.request_policy
    assert policy.user_response_policy == "ask_clarification"
    assert policy.clarification_reason == "login_credentials_unresolved"


@pytest.mark.asyncio
async def test_preflight_declines_when_the_client_cannot_render_the_card() -> None:
    ctx = _preflight_ctx()
    ctx.client_supports_credential_pause = False
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    assert await preflight_credential_pause(ctx, stream, config) is None
    stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_consumes_the_one_pause_per_turn_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _preflight_ctx()
    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response("skip", None)
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    await preflight_credential_pause(ctx, stream, config)
    assert ctx.credential_pause_used is True

    ctx.request_policy = _blocked_login_policy()
    assert await maybe_credential_pause(ctx, _fake_result(), stream, config) is None
    assert len(stream.send.await_args_list) == 1
