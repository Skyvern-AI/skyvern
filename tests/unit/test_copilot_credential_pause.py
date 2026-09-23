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
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import RunConfig
from fastapi import HTTPException, status
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.cache.base import NoopLock
from skyvern.forge.sdk.copilot import credential_pause as credential_pause_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import _finalize_result_with_blocker_override
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_ORIGIN_RECOVERY_DECLINED_REASON_CODE,
    CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE,
    CopilotToolBlockerSignal,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import (
    AgentResult,
    ApprovedCredential,
    CopilotContext,
    StructuredContext,
    record_approved_credentials_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.credential_pause import (
    CredentialPauseRejection,
    CredentialPauseResolution,
    _encode_active_pause,
    await_pending_credential_pause,
    credential_pause_active_key,
    credential_response_cache_key,
    encode_credential_response,
    maybe_credential_pause,
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
    _elapsed_run_seconds,
    enforcement_decision,
    run_with_enforcement,
)
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    _seed_prior_approved_credentials,
    credential_prompt_reason,
)
from skyvern.forge.sdk.copilot.runtime import CredentialOriginRecovery, CredentialOriginRecoveryState
from skyvern.forge.sdk.copilot.tools import credential_fill as credential_fill_module
from skyvern.forge.sdk.copilot.tools._shared import TOTAL_TIMEOUT_SECONDS, _copilot_seconds_remaining
from skyvern.forge.sdk.copilot.tools.credential_fill import _request_credential
from skyvern.forge.sdk.copilot.tools.guardrails import _authority_tool_error
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.core.event_source_stream import EventSourceStream
from skyvern.forge.sdk.routes.workflow_copilot import (
    WorkflowCopilotCredentialResponseRequest,
    workflow_copilot_credential_response,
)
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotCredentialRequiredUpdate,
    WorkflowCopilotStreamMessageType,
)
from tests.unit.conftest import make_copilot_context
from tests.unit.copilot_test_helpers import wire_credential_vault


@pytest.fixture(autouse=True)
def _sequential_resume_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each card gets tok-1, tok-2, ... so a test can pre-seed the answer for the card it expects."""
    tokens = (f"tok-{n}" for n in count(1))
    monkeypatch.setattr(credential_pause_module, "_new_resume_token", lambda: next(tokens))


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


def test_reason_does_not_let_testing_intent_override_concrete_skipped_run() -> None:
    """A classifier-produced testing label cannot erase a concrete credential boundary fact."""
    policy = RequestPolicy(testing_intent="skip_test")
    ctx = SimpleNamespace(last_run_skipped_unbound_credentials=True, request_policy=policy)
    assert credential_pause_module.credential_pause_reason(ctx) == "workflow_credential_inputs_unbound"


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


def test_reason_does_not_fire_from_generic_deferred_draft_policy() -> None:
    policy = RequestPolicy(credential_draft_deferred_explicitly=True)
    ctx = SimpleNamespace(
        last_run_skipped_unbound_credentials=False,
        latest_diagnosis_repair_contract=None,
        request_policy=policy,
        update_workflow_called=True,
    )
    assert credential_pause_module.credential_pause_reason(ctx) is None


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
async def test_a_run_that_hits_a_login_still_gets_a_card_after_an_unanswered_tool_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-card budget is spent when the tool asks on a guess. A run that then hits a real login
    has evidence the guess did not, so the guess must not cost the turn its real card."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy(
        credential_ask_login_page_urls=["https://old.example.com/login"],
        login_page_urls=["https://actual.example.com/login"],
    )
    # State left by a tool ask the user skipped.
    ctx.credential_pause_used = True
    ctx.credential_pause_reaskable_by_run = True

    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        "connected", "cred_1", "tok-1"
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
    assert ctx.credential_pause_outcome == "connected"
    assert ctx.request_policy.live_page_admitted_urls == {"cred_1": "https://actual.example.com/login"}
    carried = record_approved_credentials_in_global_llm_context(ctx, None)
    assert StructuredContext.from_json_str(carried).approved_credentials == [
        ApprovedCredential(credential_id="cred_1", admitted_url="https://actual.example.com/login")
    ]
    # Handed back once only, so the turn cannot loop on repeated cards.
    assert ctx.credential_pause_reaskable_by_run is False


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
        "connected", "cred_1", "tok-1"
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
        "connected", "cred_1", "tok-1"
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
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-2")] = encode_credential_response(
        "skip", None, "tok-1"
    )
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
        "skip", None, "tok-1"
    )
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)

    stream = _make_stream()
    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)

    resume_msgs = await maybe_credential_pause(ctx, _fake_result(), stream, config)

    assert resume_msgs is not None
    assert ctx.last_test_ok is None
    assert enforcement_decision(ctx, result=None, config=config) is None


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

    async def fake_wait_races_in_a_response(
        response_key: str, ctx: CopilotContext, stream: EventSourceStream, timeout_seconds: int, resume_token: str
    ) -> None:
        # Simulates resolve_credential_pause's lock-protected write landing in
        # the same instant the waiter's own poll loop concludes None.
        cache.store[response_key] = encode_credential_response("skip", None, "tok-1")
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
    """The predictor (credential_pause_transport_ready) excludes the async-only
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
        "connected", "cred_foreign", "tok-1"
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
        "skip", None, "tok-1"
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


def test_elapsed_run_seconds_subtracts_pause_time() -> None:
    ctx = SimpleNamespace(copilot_credential_pause_seconds=50.0)
    start_time = time.monotonic() - 60.0

    elapsed = _elapsed_run_seconds(ctx, start_time)

    assert 9.0 < elapsed < 11.0


@pytest.mark.asyncio
async def test_paused_loop_does_not_trip_total_timeout_on_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on old code: a slow pause would consume the real timeout budget.

    Four values must stay ordered: non-pause work < TOTAL_TIMEOUT_SECONDS < pause <
    credential_pause_timeout_seconds. Non-pause work was measured at ~2s on a cold CI shard
    (whichever test imports the agent SDK first pays for it), so a sub-second budget only
    ever passed on a warm runner. The pause must still exceed the budget or the assertion
    proves nothing, and must stay under the pause timeout or it resolves as a timeout
    instead of a response.
    """
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 4.0)

    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-credit"
    ctx.client_supports_credential_pause = True
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()

    cache = _FakeCache()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.5)

    async def _populate_after_first_poll() -> None:
        await asyncio.sleep(4.5)
        cache.store[credential_response_cache_key("org-1", "chat-1", "turn-credit")] = encode_credential_response(
            "skip", None, "tok-1"
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

    config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=30)

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
    assert cache.store[expected_key] == encode_credential_response("skip", None, "tok-1")
    response_set = next(call for call in cache.set_calls if call[0] == expected_key)
    assert response_set[1] == encode_credential_response("skip", None, "tok-1")
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
    assert cache.store[expected_key] == encode_credential_response("connected", "cred_1", "tok-1")


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

    assert enforcement_decision(ctx, result=None, config=config) is None

    ctx.credential_pause_used = True
    assert enforcement_decision(ctx, result=None, config=config) is None


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
        "skip", None, "tok-1"
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


def _skip_flag_workflow_yaml() -> str:
    return "workflow_definition:\n  parameters: []\n  blocks:\n  - block_type: code\n    label: step_one\n"


def _skip_flag_ctx() -> CopilotContext:
    ctx = make_copilot_context()
    ctx.request_policy = RequestPolicy()
    return ctx


async def _no_prior_definition(update_ctx: CopilotContext) -> object:
    return None


# ---------------------------------------------------------------------------
# _copilot_seconds_remaining must credit pause time, or a long pause late in
# the turn FORBIDS the very test the connected credential was for
# (blockers.py's _late_block_running_call_signal).
# ---------------------------------------------------------------------------


def test_copilot_seconds_remaining_credits_pause_time() -> None:
    ctx = SimpleNamespace(copilot_run_start_monotonic=time.monotonic() - 1000.0, copilot_credential_pause_seconds=300.0)

    remaining = _copilot_seconds_remaining(ctx)

    # Uncredited, the budget would be spent against 1000s rather than the 700s of counted work.
    assert remaining is not None
    assert remaining == pytest.approx(TOTAL_TIMEOUT_SECONDS - 700.0, abs=1.0)


# ---------------------------------------------------------------------------
# A client that can't render credential_required must never be paused for,
# or it stares at "Working..." until the timeout.
# ---------------------------------------------------------------------------


def test_transport_not_ready_when_client_does_not_support_pause() -> None:
    ctx = make_copilot_context()
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    config = CopilotConfig(credential_pause_enabled=True)

    assert credential_pause_module.credential_pause_transport_ready(ctx, config) is False


def test_transport_ready_once_client_support_is_set() -> None:
    ctx = make_copilot_context()
    ctx.client_supports_credential_pause = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy = RequestPolicy()
    config = CopilotConfig(credential_pause_enabled=True)

    assert credential_pause_module.credential_pause_transport_ready(ctx, config) is True


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
        "connected", "cred_1", "tok-1"
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
        "skip", None, "tok-1"
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
            "skip", None, "tok-1"
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


def _ask_origin_policy(*ask_login_page_urls: str) -> RequestPolicy:
    return RequestPolicy(credential_ask_login_page_urls=list(ask_login_page_urls))


def _named_site_policy(site_url: str = "https://portal.example.com/login") -> RequestPolicy:
    return RequestPolicy(user_provided_site_urls=[site_url])


def _tool_ctx(monkeypatch: pytest.MonkeyPatch, cache: _FakeCache | None = None) -> CopilotContext:
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    ctx.request_policy = _named_site_policy()
    ctx.stream = _make_stream()
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache or _FakeCache(), raising=False)
    monkeypatch.setattr(credential_pause_module, "CREDENTIAL_RESPONSE_POLL_SECONDS", 0.01)
    return ctx


async def _ask(ctx: CopilotContext, login_page_url: str = "https://portal.example.com/login") -> dict[str, Any]:
    return await _request_credential(login_page_url, "This site needs a sign-in.", ctx)


def test_connected_resume_stamps_the_single_ask_origin() -> None:
    policy = _ask_origin_policy("https://portal.example.com/login")

    credential_pause_module._apply_connected_credential_to_policy(make_copilot_context(), policy, _make_credential())

    assert policy.live_page_admitted_urls == {"cred_1": "https://portal.example.com/login"}


def test_connected_resume_stamps_nothing_when_the_ask_spans_several_origins() -> None:
    policy = _ask_origin_policy("https://portal.example.com/login", "https://other.example.com/login")

    credential_pause_module._apply_connected_credential_to_policy(make_copilot_context(), policy, _make_credential())

    assert policy.live_page_admitted_urls == {}


def test_connected_resume_names_the_picked_credential_superseding_an_earlier_mention() -> None:
    """The fill seam's which-credential check is set equality, so a card answer must replace an
    earlier prose mention, not join it."""
    policy = _ask_origin_policy("https://portal.example.com/login")
    policy.current_turn_named_credential_ids = {"cred_earlier"}

    credential_pause_module._apply_connected_credential_to_policy(make_copilot_context(), policy, _make_credential())

    assert policy.current_turn_named_credential_ids == {"cred_1"}


def test_connected_resume_does_not_duplicate_an_already_resolved_credential() -> None:
    policy = _ask_origin_policy("https://portal.example.com/login")
    credential = _make_credential()
    policy.resolved_credentials = [credential]

    credential_pause_module._apply_connected_credential_to_policy(make_copilot_context(), policy, credential)

    assert [resolved.credential_id for resolved in policy.resolved_credentials] == ["cred_1"]


@pytest.mark.parametrize(
    "login_urls",
    [[], ["https://portal.example.com/login"], ["https://portal.example.com/login", "https://other.example.com/login"]],
)
def test_a_card_connected_credential_keeps_its_durable_cross_turn_approval(login_urls: list[str]) -> None:
    ctx = make_copilot_context()
    policy = _ask_origin_policy(*login_urls)
    ctx.request_policy = policy

    credential_pause_module._apply_connected_credential_to_policy(ctx, policy, _make_credential())
    carried = record_approved_credentials_in_global_llm_context(ctx, None)

    assert [record.credential_id for record in StructuredContext.from_json_str(carried).approved_credentials] == [
        "cred_1"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_origin", [None, "", "https://old.example.com/login"])
@pytest.mark.parametrize(
    "login_url",
    [
        "https://portal.example.com/login",
        "https://portal.example.com/login?code=synthetic-code&state=synthetic-state#synthetic-token",
    ],
)
async def test_card_selection_keeps_its_origin_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, previous_origin: str | None, login_url: str
) -> None:
    ctx = make_copilot_context()
    ctx.request_policy = _ask_origin_policy(login_url)
    ctx.request_policy.persisted_workflow_credential_ids = {"cred_1"}
    previous = StructuredContext()
    if previous_origin is not None:
        previous.approved_credentials = [ApprovedCredential(credential_id="cred_1", admitted_url=previous_origin)]
    credential_pause_module._apply_connected_credential_to_policy(ctx, ctx.request_policy, _make_credential())
    carried = record_approved_credentials_in_global_llm_context(ctx, previous.to_json_str())
    assert carried is not None
    records = StructuredContext.from_json_str(carried).approved_credentials
    assert records == [ApprovedCredential(credential_id="cred_1", admitted_url="https://portal.example.com/login")]

    next_policy = RequestPolicy()
    _stub_credential_lookup(monkeypatch, _make_credential())
    await _seed_prior_approved_credentials(next_policy, organization_id="org-1", global_llm_context=carried)
    next_ctx = make_copilot_context()
    next_ctx.request_policy = next_policy
    next_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    grant, error = await tools_module.credential_fill._credential_fill_origin_grant(next_ctx, "cred_1")
    assert error is None
    assert grant is not None
    assert tools_module.credential_fill._within_grant("https://portal.example.com/password", grant)
    assert not tools_module.credential_fill._within_grant("https://elsewhere.example.com/login", grant)
    assert not tools_module.credential_fill._within_grant("https://old.example.com/login", grant)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "login_url",
    [
        r"https://trusted.example\@evil.example/login",
        "https://synthetic-user:synthetic-password@portal.example.com/login",
        "https://portal.example.com:invalid/login",
    ],
)
async def test_invalid_card_origin_cannot_create_cross_turn_approval(
    monkeypatch: pytest.MonkeyPatch, login_url: str
) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    _stub_credential_lookup(monkeypatch, _make_credential())
    result = await _ask(ctx, login_url)
    assert result["ok"] is False
    assert ctx.credential_pause_connected_credential_id is None

    # Retained state must also fail closed if it predates validation at the card boundary.
    ctx.request_policy = _ask_origin_policy(login_url)
    credential_pause_module._apply_connected_credential_to_policy(ctx, ctx.request_policy, _make_credential())
    carried = record_approved_credentials_in_global_llm_context(ctx, None)
    assert StructuredContext.from_json_str(carried).approved_credentials == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_named", [False, True])
async def test_saved_workflow_resolution_requires_user_selection_for_chat_approval(
    monkeypatch: pytest.MonkeyPatch, user_named: bool
) -> None:
    ctx = make_copilot_context()
    ctx.request_policy = RequestPolicy(
        resolved_credentials=[_make_credential()],
        persisted_workflow_credential_ids={"cred_1"},
        current_turn_named_credential_ids={"cred_1"} if user_named else set(),
    )
    carried = record_approved_credentials_in_global_llm_context(ctx, None)
    next_policy = RequestPolicy()
    _stub_credential_lookup(monkeypatch, _make_credential())
    await _seed_prior_approved_credentials(next_policy, organization_id="org-1", global_llm_context=carried)
    assert [credential.credential_id for credential in next_policy.resolved_credentials] == (
        ["cred_1"] if user_named else []
    )


@pytest.mark.asyncio
async def test_card_approval_preserves_explicit_port_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_context()
    ctx.request_policy = _ask_origin_policy("https://portal.example.com:0/login?code=synthetic-code")
    credential_pause_module._apply_connected_credential_to_policy(ctx, ctx.request_policy, _make_credential())
    carried = record_approved_credentials_in_global_llm_context(ctx, None)
    next_policy = RequestPolicy()
    _stub_credential_lookup(monkeypatch, _make_credential())
    await _seed_prior_approved_credentials(next_policy, organization_id="org-1", global_llm_context=carried)
    next_ctx = make_copilot_context()
    next_ctx.request_policy = next_policy
    next_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    grant, error = await tools_module.credential_fill._credential_fill_origin_grant(next_ctx, "cred_1")
    assert error is None
    assert grant is not None
    assert tools_module.credential_fill._within_grant("https://portal.example.com:0/password", grant)
    assert not tools_module.credential_fill._within_grant("https://portal.example.com/password", grant)


@pytest.mark.parametrize("already_approved", [False, True])
def test_page_only_admission_does_not_become_durable_approval(already_approved: bool) -> None:
    ctx = make_copilot_context()
    ctx.request_policy = RequestPolicy(
        resolved_credentials=[_make_credential()],
        live_page_admitted_urls={"cred_1": "https://portal.example.com/login"},
    )
    previous = StructuredContext()
    if already_approved:
        previous.approved_credentials = [
            ApprovedCredential(credential_id="cred_1", admitted_url="https://old.example.com/login")
        ]
    carried = record_approved_credentials_in_global_llm_context(ctx, previous.to_json_str())
    assert StructuredContext.from_json_str(carried).approved_credentials == previous.approved_credentials


def _answered_cache(action: str, credential_id: str | None = None) -> _FakeCache:
    cache = _FakeCache()
    cache.store[credential_response_cache_key("org-1", "chat-1", "turn-1")] = encode_credential_response(
        action, credential_id, "tok-1"
    )
    return cache


def _stub_credential_lookup(
    monkeypatch: pytest.MonkeyPatch, credential: Credential, vault_row: AsyncMock | None = None
) -> None:
    monkeypatch.setattr(
        credential_pause_module.app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(
                get_credentials_by_ids=AsyncMock(return_value=[credential]),
                get_credential=vault_row or AsyncMock(return_value=None),
            )
        ),
    )


@pytest.mark.asyncio
async def test_the_tool_call_raises_the_card_and_returns_the_connected_credential_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails without the tool: nothing pre-run raises the card, so the turn finalizes with a prose
    ask and no frame. Fails again if the answer is not returned as this call's own typed result."""
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    _stub_credential_lookup(monkeypatch, _make_credential())

    result = await _ask(ctx)

    assert result["status"] == "connected"
    assert result["credential_id"] == "cred_1"
    frame = ctx.stream.send.await_args_list[0].args[0]
    assert frame.type is WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED
    assert frame.reason == "login_credentials_unresolved"
    assert frame.login_page_urls == ["https://portal.example.com/login"]
    assert frame.message == "This site needs a sign-in."
    assert ctx.request_policy.live_page_admitted_urls == {"cred_1": "https://portal.example.com/login"}
    assert ctx.request_policy.current_turn_named_credential_ids == {"cred_1"}
    assert ctx.request_policy.allow_run_blocks is True


@pytest.mark.asyncio
async def test_a_skipped_card_returns_skipped_and_spends_the_one_ask_per_turn_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("skip"))

    first = await _ask(ctx)
    second = await _ask(ctx)

    assert first["status"] == "skipped"
    assert ctx.credential_pause_outcome == "skipped"
    assert second["status"] == "already_asked"
    assert second["outcome"] == "skipped"
    assert ctx.stream.send.await_count == 1


@pytest.mark.asyncio
async def test_an_unanswered_card_returns_a_typed_outcome_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user can walk away from an open card; the model must get a typed arm it can act on
    instead of a failed call it would retry or narrate as a breakage."""
    ctx = _tool_ctx(monkeypatch)
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=0)

    result = await _ask(ctx)

    assert result["ok"] is True
    assert result["status"] == "unanswered"
    assert result["outcome"] == "timeout"
    assert result["next"]
    ctx.stream.send.assert_awaited()


@pytest.mark.asyncio
async def test_the_tool_offers_a_discovered_site_and_binds_only_the_users_card_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    _stub_credential_lookup(monkeypatch, _make_credential())

    result = await _ask(ctx, "https://elsewhere.example.net/login")

    assert result["status"] == "connected"
    frame = ctx.stream.send.await_args_list[0].args[0]
    assert frame.login_page_urls == ["https://elsewhere.example.net/login"]
    assert ctx.request_policy.live_page_admitted_urls == {"cred_1": "https://elsewhere.example.net/login"}
    assert ctx.request_policy.current_turn_named_credential_ids == {"cred_1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("login_url", ["", "javascript:alert(1)", "not-a-url"])
async def test_the_card_requires_a_real_sign_in_url(monkeypatch: pytest.MonkeyPatch, login_url: str) -> None:
    ctx = _tool_ctx(monkeypatch)
    result = await _ask(ctx, login_url)
    assert result["ok"] is False
    ctx.stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_tool_reports_unavailable_with_a_prose_fallback_when_the_card_cannot_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _tool_ctx(monkeypatch)
    ctx.client_supports_credential_pause = False

    result = await _ask(ctx)

    assert result["status"] == "unavailable"
    assert "Credentials page" in result["fallback"]
    assert ctx.request_policy.credential_ask_login_page_urls == []
    ctx.stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_tool_reports_unavailable_rather_than_raising_when_no_cache_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with no cache at all must degrade, not fault the turn on an attribute of None."""
    ctx = _tool_ctx(monkeypatch)
    monkeypatch.setattr(app._inst, "CACHE", None, raising=False)

    result = await _ask(ctx)

    assert result["status"] == "unavailable"
    ctx.stream.send.assert_not_awaited()


class _SlowAnswerCache(_FakeCache):
    """Answers the card only after ``delay_seconds``, standing in for a user who takes a moment."""

    def __init__(self, response_key: str, delay_seconds: float, action: str = "skip") -> None:
        super().__init__()
        self._response_key = response_key
        self._action = action
        self._ready_at = time.monotonic() + delay_seconds

    async def get(self, key: str) -> Any:
        if key == self._response_key and time.monotonic() >= self._ready_at:
            return encode_credential_response(self._action, "cred_1" if self._action == "connected" else None, "tok-1")
        return await super().get(key)


@pytest.mark.asyncio
async def test_cancelling_the_turn_while_the_card_is_open_stays_a_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suspended deadline is handed back on the way out; a failure to reschedule it must not
    replace the cancellation with a RuntimeError the enforcement loop does not handle."""
    ctx = _tool_ctx(
        monkeypatch,
        _SlowAnswerCache(credential_response_cache_key("org-1", "chat-1", "turn-1"), 30),
    )

    async def ask_under_deadline() -> None:
        async with asyncio.timeout(30) as deadline:
            ctx.model_stream_deadline = deadline
            await _ask(ctx)

    task = asyncio.create_task(ask_under_deadline())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.copilot_credential_pause_seconds > 0


_RUN_TOOLS_GATED_ON_AN_OPEN_ASK = [
    ("run_blocks_and_collect_debug", {"block_labels": ["login"], "parameters": {}}),
    (
        "edit_block_and_run",
        {
            "label": "login",
            "expected_code": "old",
            "replacement_code": "new",
            "block_labels": ["login"],
            "parameters": {},
        },
    ),
    ("update_and_run_blocks", {"workflow_yaml": "title: draft", "block_labels": ["login"], "parameters": {}}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "arguments"), _RUN_TOOLS_GATED_ON_AN_OPEN_ASK)
async def test_a_run_tool_called_alongside_the_ask_waits_for_the_user_to_answer(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """The provider issues sibling tool calls in one model response, so without this wait the run
    starts against the pre-card policy and the user answers a login the run already went without."""
    ctx = _tool_ctx(
        monkeypatch,
        _SlowAnswerCache(credential_response_cache_key("org-1", "chat-1", "turn-1"), 0.25, "connected"),
    )
    _stub_credential_lookup(monkeypatch, _make_credential())
    ctx.turn_origin = TurnOrigin.runtime_self_heal
    tool = {
        "run_blocks_and_collect_debug": tools_module.run_blocks_tool,
        "edit_block_and_run": tools_module.edit_block_and_run_tool,
        "update_and_run_blocks": tools_module.update_and_run_blocks_tool,
    }[tool_name]
    finished: list[str] = []

    async def ask() -> None:
        await _ask(ctx)
        finished.append("ask")

    async def run() -> None:
        await tool.on_invoke_tool(SimpleNamespace(context=ctx, tool_name=tool_name), json.dumps(arguments))
        finished.append("run")

    await asyncio.gather(ask(), run())

    assert finished == ["ask", "run"]
    assert ctx.credential_pause_outcome == "connected"


async def _announce_ask(ctx: CopilotContext) -> None:
    response = SimpleNamespace(output=[SimpleNamespace(name="request_credential")])
    await CopilotRunHooks(ctx).on_llm_end(SimpleNamespace(context=ctx), MagicMock(), response)  # type: ignore[arg-type]


async def _invoke_ask_tool(ctx: CopilotContext, login_page_url: str) -> None:
    await tools_module.request_credential_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="request_credential"),  # type: ignore[arg-type]
        json.dumps({"login_page_url": login_page_url, "reason": "This site needs a sign-in."}),
    )


def test_the_ask_tool_is_registered_in_the_production_tool_set() -> None:
    """Every other test here invokes the tool object directly, so unregistering it would take the
    whole capability away without reddening one of them."""
    assert "request_credential" in {tool.name for tool in tools_module.NATIVE_TOOLS}


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "arguments"), _RUN_TOOLS_GATED_ON_AN_OPEN_ASK)
async def test_a_run_tool_scheduled_ahead_of_the_ask_in_one_response_still_waits(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Tool calls in one model response run as concurrent tasks in an order the model picks, so a
    gate armed only inside the ask's own coroutine is passed by whichever sibling is scheduled first."""
    ctx = _tool_ctx(
        monkeypatch,
        _SlowAnswerCache(credential_response_cache_key("org-1", "chat-1", "turn-1"), 0.25, "connected"),
    )
    _stub_credential_lookup(monkeypatch, _make_credential())
    await _announce_ask(ctx)
    finished: list[str] = []

    tool = {
        "run_blocks_and_collect_debug": tools_module.run_blocks_tool,
        "edit_block_and_run": tools_module.edit_block_and_run_tool,
        "update_and_run_blocks": tools_module.update_and_run_blocks_tool,
    }[tool_name]

    async def run() -> None:
        await tool.on_invoke_tool(SimpleNamespace(context=ctx, tool_name=tool_name), json.dumps(arguments))  # type: ignore[arg-type]
        finished.append("run")

    async def ask() -> None:
        await _invoke_ask_tool(ctx, "https://portal.example.com/login")
        finished.append("ask")

    await asyncio.gather(run(), ask())

    # Without this the ask refuses instantly and the ordering is proven around a card that never
    # opened, which is the one shape the wrapper's release runs in.
    assert ctx.credential_pause_outcome == "connected"
    assert finished == ["ask", "run"]


@pytest.mark.asyncio
async def test_an_announced_ask_that_never_reaches_the_card_still_releases_the_waiting_run_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arming on the response means the gate closes before the ask can refuse, so every exit of the
    tool has to open it again or the sibling waits out the whole turn."""
    ctx = _tool_ctx(monkeypatch)
    await _announce_ask(ctx)

    async def ask() -> None:
        await _invoke_ask_tool(ctx, "not-a-url")

    await asyncio.wait_for(asyncio.gather(await_pending_credential_pause(ctx), ask()), timeout=5)

    assert ctx.request_policy.credential_ask_login_page_urls == []


def test_the_card_message_is_capped_and_stripped_before_the_user_sees_it() -> None:
    """`reason` is model-authored and leads the card, above the hostname. An unbounded one can push
    the site the credential is actually for off the viewport."""
    shouted = 'Connect your bank    login\n"now"' + "!" * 500

    safe = credential_pause_module.defang_card_text(shouted)

    assert len(safe) <= 200
    assert "\n" not in safe
    assert '"' not in safe


@pytest.mark.asyncio
async def test_a_repeat_ask_in_a_later_response_releases_the_gate_it_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """already_asked means a card was raised somewhere this turn, not that one is open now. A later
    response arms a fresh gate, so refusing to release on that exit parks every sibling on an Event
    no one owns until the turn times out."""
    ctx = _tool_ctx(monkeypatch)
    ctx.credential_pause_used = True

    await _announce_ask(ctx)
    await _invoke_ask_tool(ctx, "https://portal.example.com/login")

    await asyncio.wait_for(await_pending_credential_pause(ctx), timeout=1)


def _redacted_secret_policy(*site_urls: str) -> RequestPolicy:
    policy = RequestPolicy(user_provided_site_urls=list(site_urls))
    policy.apply_raw_secret_redacted_draft()
    return policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("site_urls", "handling", "model_url"),
    [
        ((), "redacted_draft", "https://portal.example.com/login"),
        (("https://portal.example.com/login",), "redacted_draft", "https://elsewhere.example.net/login"),
        (("https://portal.example.com/login",), "block", "https://portal.example.com/login"),
    ],
    ids=["no_user_url", "model_only_url", "blocked_turn"],
)
async def test_a_raw_secret_turn_opens_no_card_without_a_site_the_user_gave(
    monkeypatch: pytest.MonkeyPatch, site_urls: tuple[str, ...], handling: str, model_url: str
) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    ctx.request_policy = _redacted_secret_policy(*site_urls)
    ctx.request_policy.raw_secret_handling = handling

    result = await _ask(ctx, model_url)

    assert result["ok"] is False
    ctx.stream.send.assert_not_awaited()
    assert ctx.credential_pause_used is False
    assert ctx.request_policy.credential_ask_login_page_urls == []


@pytest.mark.asyncio
async def test_a_raw_secret_turn_opens_no_authenticator_card(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    ctx.request_policy = _redacted_secret_policy("https://portal.example.com/login")
    ctx.request_policy.resolved_credentials = [credential]

    result = await _request_credential("https://portal.example.com/login", "Needs 2FA.", ctx, "cred_1")

    assert result["ok"] is False
    ctx.stream.send.assert_not_awaited()
    assert ctx.credential_totp_update_asked is False


_CardAnswer = tuple[Literal["connected", "skip"], str | None]


def _answer_each_card(
    cache: _FakeCache, answers: list[_CardAnswer]
) -> Callable[[WorkflowCopilotCredentialRequiredUpdate], Awaitable[bool]]:
    """Answer each card through the resume route's own writer as it is sent."""
    pending = list(answers)

    async def send(card: WorkflowCopilotCredentialRequiredUpdate) -> bool:
        action, credential_id = pending.pop(0)
        await resolve_credential_pause(
            cache,
            organization_id="org-1",
            workflow_copilot_chat_id="chat-1",
            turn_id=card.turn_id,
            resume_token=card.resume_token,
            action=action,
            credential_id=credential_id,
        )
        return True

    return send


async def _call_ask_tool(ctx: CopilotContext, **arguments: object) -> dict[str, Any]:
    raw = await tools_module.request_credential_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="request_credential"),  # type: ignore[arg-type]
        json.dumps({"login_page_url": "https://portal.example.com/login", "reason": "Needs 2FA.", **arguments}),
    )
    return json.loads(raw)


def _sent_cards(ctx: CopilotContext) -> list[WorkflowCopilotCredentialRequiredUpdate]:
    return [call.args[0] for call in ctx.stream.send.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("totp_after_save", "expected_status"),
    [(None, "saved_without_authenticator"), ("fake-seed", "authenticator_added")],
)
async def test_a_credential_with_no_authenticator_raises_an_update_card_naming_it(
    monkeypatch: pytest.MonkeyPatch, totp_after_save: str | None, expected_status: str
) -> None:
    """The update answer grants nothing, and success is read from the saved record, not from the answer."""
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    app.CREDENTIAL_VAULT_SERVICES[CredentialVaultType.SKYVERN].get_credential_item.side_effect = [
        SimpleNamespace(name="authtest simple", credential=PasswordCredential(username="u", password="p", totp=None)),
        SimpleNamespace(
            name="authtest simple", credential=PasswordCredential(username="u", password="p", totp=totp_after_save)
        ),
    ]
    ctx.request_policy.resolved_credentials = [credential]
    ctx.request_policy.live_page_admitted_urls = {"cred_1": "https://app.example.org/signin"}
    ctx.request_policy.credential_ask_login_page_urls = ["https://app.example.org/signin"]
    ctx.request_policy.allow_run_blocks = False
    policy_before = deepcopy(ctx.request_policy)
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_1")]))

    result = await _call_ask_tool(ctx, credential_id="cred_1")

    [card] = _sent_cards(ctx)
    assert (card.reason, card.credential_refs, card.login_page_urls) == (
        "credential_missing_totp",
        ["cred_1"],
        ["https://portal.example.com/login"],
    )
    assert (result["status"], result["credential_id"]) == (expected_status, "cred_1")
    assert ctx.request_policy == policy_before
    assert ctx.credential_pause_outcome is None


@pytest.mark.asyncio
async def test_an_update_card_answered_with_another_credential_is_not_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    ctx.request_policy.resolved_credentials = [credential]
    policy_before = deepcopy(ctx.request_policy)
    app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=[_make_credential("cred_2")])
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_2")]))

    result = await _call_ask_tool(ctx, credential_id="cred_1")

    assert result["status"] == "unanswered"
    assert "credential_id" not in result
    assert ctx.request_policy == policy_before


@pytest.mark.asyncio
async def test_an_update_ask_waits_out_a_card_already_on_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _tool_ctx(monkeypatch)
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    ctx.request_policy.resolved_credentials = [credential]
    ctx.credential_ask_in_flight = True

    result = await _request_credential("https://portal.example.com/login", "Needs 2FA.", ctx, "cred_1")

    assert result["status"] == "already_asked"
    ctx.stream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_update_card_after_a_pick_waits_for_its_own_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both cards share the turn's response key, so the update card must not read the pick's answer."""
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_1"), ("skip", None)]))

    picked = await _call_ask_tool(ctx)
    ctx.last_test_ok = False
    update = await _call_ask_tool(ctx, credential_id="cred_1")
    repeat = await _call_ask_tool(ctx, credential_id="cred_1")

    assert [picked["status"], update["status"], repeat["status"]] == ["connected", "skipped", "already_asked"]
    assert "outcome" not in repeat
    assert [card.reason for card in _sent_cards(ctx)] == ["login_credentials_unresolved", "credential_missing_totp"]
    assert (ctx.credential_pause_outcome, ctx.credential_pause_connected_credential_id) == ("connected", "cred_1")
    assert ctx.credential_pause_reaskable_by_run is False
    assert ctx.last_test_ok is False


@pytest.mark.asyncio
async def test_an_update_ask_leaves_the_pick_budget_for_a_later_card(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    credential = wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp=None))
    ctx.request_policy.resolved_credentials = [credential]
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("skip", None), ("connected", "cred_1")]))

    update = await _call_ask_tool(ctx, credential_id="cred_1")
    run_card_ready = credential_pause_module.credential_pause_transport_ready(ctx, ctx.copilot_config)
    picked = await _call_ask_tool(ctx)

    assert [update["status"], picked["status"]] == ["skipped", "connected"]
    assert run_card_ready is True
    assert [card.reason for card in _sent_cards(ctx)] == ["credential_missing_totp", "login_credentials_unresolved"]


@pytest.mark.asyncio
async def test_an_answer_without_a_resume_token_resolves_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    key = credential_response_cache_key("org-1", "chat-1", "turn-1")
    cache.store[key] = json.dumps({"action": "connected", "credential_id": "cred_1"})
    monkeypatch.setattr(credential_pause_module.app._inst, "CACHE", cache, raising=False)

    assert await credential_pause_module._try_resolve_credential_response(key, "org-1", "tok-1") == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "status"),
    [("skip", "skipped"), ("timeout", "unanswered"), ("unsupported_client", "unavailable")],
)
async def test_a_raw_secret_card_keeps_the_typed_non_connect_outcomes(
    monkeypatch: pytest.MonkeyPatch, answer: str, status: str
) -> None:
    ctx = _tool_ctx(monkeypatch, _answered_cache("skip") if answer == "skip" else None)
    ctx.request_policy = _redacted_secret_policy("https://portal.example.com/login")
    if answer == "timeout":
        ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=0)
    if answer == "unsupported_client":
        ctx.client_supports_credential_pause = False

    result = await _ask(ctx)

    assert result["status"] == status
    assert ctx.request_policy.allow_run_blocks is False
    assert ctx.request_policy.current_turn_named_credential_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secrets", "resolved", "expected"),
    [
        pytest.param(
            PasswordCredential(username="u", password="p", totp="fake-seed"),
            True,
            {"ok": True, "status": "has_code_method", "method": "authenticator"},
            id="authenticator",
        ),
        pytest.param(
            PasswordCredential(
                username="u", password="p", totp=None, totp_type=TotpType.EMAIL, totp_identifier="otp@example.com"
            ),
            True,
            {"ok": True, "status": "has_code_method", "method": "email_or_text"},
            id="email-otp",
        ),
        pytest.param(
            PasswordCredential(username="u", password="p", totp=None),
            False,
            {"ok": False},
            id="not-resolved-for-this-request",
        ),
    ],
)
async def test_no_update_card_for_a_credential_with_a_code_method_or_outside_the_request(
    monkeypatch: pytest.MonkeyPatch, secrets: PasswordCredential, resolved: bool, expected: dict[str, Any]
) -> None:
    ctx = _tool_ctx(monkeypatch)
    credential = wire_credential_vault(monkeypatch, secrets)
    ctx.request_policy.resolved_credentials = [credential] if resolved else []

    result = await _call_ask_tool(ctx, credential_id="cred_1")

    assert {key: result[key] for key in expected} == expected
    ctx.stream.send.assert_not_awaited()


_IDP_ORIGIN = "https://idp.example.test"


_PENDING = CredentialOriginRecovery(_IDP_ORIGIN, "pending", refused_credential_id="cred_service")


def _idp_credential(credential_id: str = "cred_idp") -> Credential:
    return _make_credential(credential_id, "Provider Login").model_copy(update={"tested_url": f"{_IDP_ORIGIN}/login"})


def _recovery_ctx(monkeypatch: pytest.MonkeyPatch, cache: _FakeCache) -> CopilotContext:
    ctx = _tool_ctx(monkeypatch, cache)
    ctx.credential_pause_used = True
    ctx.credential_pause_outcome = "connected"
    ctx.credential_origin_recovery = _PENDING
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Ask for a login on the provider.",
        user_facing_reason="The sign-in continues on another site.",
        recovery_hint="ask_user_clarifying",
        internal_reason_code=CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE,
        renders_final_reply=False,
    )
    return ctx


def _assert_declined(ctx: CopilotContext, result: dict[str, Any], status: str) -> None:
    assert result["status"] == status
    assert ctx.credential_origin_recovery == replace(_PENDING, state="declined")
    declined = ctx.blocker_signal
    assert declined.internal_reason_code == CREDENTIAL_ORIGIN_RECOVERY_DECLINED_REASON_CODE
    assert CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE not in {
        signal.internal_reason_code for signal in ctx.tool_blocker_signals
    }
    assert declined.preserves_workflow_draft is True
    for tool_name in ("update_workflow", "run_blocks_and_collect_debug", "edit_block_and_run", "update_and_run_blocks"):
        assert _authority_tool_error(ctx, tool_name) is None
    models_own_report = AgentResult(user_response="Ran it; it failed.", updated_workflow=None, global_llm_context=None)
    assert _finalize_result_with_blocker_override(ctx, models_own_report) is models_own_report
    assert _IDP_ORIGIN in result["next"]  # nosemgrep: incomplete-url-substring-sanitization


@pytest.mark.asyncio
async def test_origin_recovery_gets_one_card_after_an_earlier_card_and_clears_on_a_same_origin_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", "cred_idp"))
    _stub_credential_lookup(monkeypatch, _idp_credential())

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login?state=opaque-state#fragment")

    assert result["status"] == "connected"
    assert result["credential_id"] == "cred_idp"
    ctx.stream.send.assert_awaited_once()
    card = ctx.stream.send.await_args.args[0]
    assert card.login_page_urls == [f"{_IDP_ORIGIN}/login"]
    assert ctx.request_policy.live_page_admitted_urls["cred_idp"] == f"{_IDP_ORIGIN}/login"
    assert ctx.credential_origin_recovery is None
    assert ctx.blocker_signal is None

    ctx.credential_origin_recovery = _PENDING
    again = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    ctx.stream.send.assert_awaited_once()
    _assert_declined(ctx, again, "already_asked")


@pytest.mark.asyncio
async def test_a_connected_credential_whose_own_site_differs_is_not_rebound_to_the_recovery_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    ctx.request_policy.current_turn_named_credential_ids = {"cred_1"}
    service = _make_credential().model_copy(update={"tested_url": "https://portal.example.com/login"})
    _stub_credential_lookup(monkeypatch, service)

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    ctx.stream.send.assert_awaited_once()
    assert "cred_1" not in ctx.request_policy.live_page_admitted_urls
    assert ctx.credential_pause_connected_credential_id is None
    assert ctx.credential_pause_outcome == "not_admitted"
    _assert_declined(ctx, result, "connected_other_site")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connected", "vault_row"),
    [
        (_idp_credential("cred_service"), None),
        (_make_credential("cred_blank"), None),
        (_idp_credential(), AsyncMock(side_effect=RuntimeError("vault unavailable"))),
    ],
    ids=["refused_credential", "no_site_evidence", "vault_read_error"],
)
async def test_origin_recovery_admits_no_credential_it_cannot_place_on_the_provider(
    monkeypatch: pytest.MonkeyPatch, connected: Credential, vault_row: AsyncMock | None
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", connected.credential_id))
    _stub_credential_lookup(monkeypatch, connected, vault_row)

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    assert connected.credential_id not in ctx.request_policy.live_page_admitted_urls
    assert ctx.credential_pause_outcome == "not_admitted"
    _assert_declined(ctx, result, "connected_other_site")


@pytest.mark.asyncio
async def test_a_recovery_connect_keeps_the_named_service_login_approved_beside_the_provider_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", "cred_idp"))
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    service = _make_credential("cred_service").model_copy(update={"tested_url": "https://portal.example.com/login"})
    policy = ctx.request_policy
    policy.resolved_credentials = [service]
    policy.current_turn_named_credential_ids = {"cred_service"}
    policy.persisted_workflow_credential_ids = ["cred_service"]
    _stub_credential_lookup(monkeypatch, _idp_credential())

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    assert result["status"] == "connected"
    assert credential_fill_module._request_settled_credential(policy, "cred_idp")
    grants = {
        credential_id: (await credential_fill_module._credential_fill_origin_grant(ctx, credential_id))[0]
        for credential_id in ("cred_service", "cred_idp")
    }
    assert grants["cred_service"].intended_url == "https://portal.example.com/login"
    assert grants["cred_idp"].intended_url == f"{_IDP_ORIGIN}/login"
    approved = StructuredContext.from_json_str(record_approved_credentials_in_global_llm_context(ctx, None))
    assert {record.credential_id for record in approved.approved_credentials} == {"cred_service", "cred_idp"}


@pytest.mark.asyncio
async def test_a_card_that_raises_declines_origin_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _recovery_ctx(monkeypatch, _FakeCache())
    monkeypatch.setattr(
        credential_fill_module, "request_credential_pause", AsyncMock(side_effect=RuntimeError("stream closed"))
    )

    with pytest.raises(RuntimeError, match="stream closed"):
        await _ask(ctx, f"{_IDP_ORIGIN}/login")

    assert ctx.credential_origin_recovery == replace(_PENDING, state="declined")
    assert ctx.blocker_signal.internal_reason_code == CREDENTIAL_ORIGIN_RECOVERY_DECLINED_REASON_CODE


@pytest.mark.asyncio
async def test_a_connect_that_resolves_no_credential_declines_origin_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _recovery_ctx(monkeypatch, _FakeCache())
    monkeypatch.setattr(
        credential_fill_module,
        "request_credential_pause",
        AsyncMock(return_value=CredentialPauseResolution(action="connected")),
    )

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    _assert_declined(ctx, result, "connected_unresolved")


@pytest.mark.asyncio
async def test_origin_recovery_declines_when_the_card_cannot_be_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _recovery_ctx(monkeypatch, _FakeCache())
    ctx.client_supports_credential_pause = False

    with capture_logs() as logs:
        result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    assert "copilot_credential_card_unavailable" in {entry["event"] for entry in logs}
    ctx.stream.send.assert_not_awaited()
    assert ctx.credential_pause_used is True
    _assert_declined(ctx, result, "unavailable")


@pytest.mark.asyncio
async def test_a_card_for_another_origin_leaves_origin_recovery_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    ctx.credential_pause_used = False
    _stub_credential_lookup(monkeypatch, _make_credential())

    result = await _ask(ctx, "https://portal.example.com/login")

    assert result["status"] == "connected"
    assert ctx.credential_origin_recovery == _PENDING
    assert ctx.blocker_signal.internal_reason_code == CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE


@pytest.mark.asyncio
async def test_origin_recovery_raises_no_second_card_while_one_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    ctx = _recovery_ctx(monkeypatch, cache)
    _stub_credential_lookup(monkeypatch, _idp_credential())
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_idp")]))
    ctx.credential_ask_in_flight = True

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    assert result["status"] == "already_asked"
    ctx.stream.send.assert_not_awaited()
    assert ctx.credential_origin_recovery == _PENDING
    assert _IDP_ORIGIN not in ctx.credential_origin_recovery_carded


@pytest.mark.asyncio
async def test_an_authenticator_update_ask_neither_spends_nor_resolves_origin_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    ctx = _recovery_ctx(monkeypatch, cache)
    service = wire_credential_vault(
        monkeypatch, PasswordCredential(username="u", password="p", totp=None), credential_id="cred_service"
    )
    ctx.request_policy.resolved_credentials = [service]
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("skip", None)]))

    await _request_credential(f"{_IDP_ORIGIN}/login", "Add 2FA.", ctx, credential_id="cred_service")

    ctx.stream.send.assert_awaited_once()
    assert ctx.credential_origin_recovery == _PENDING
    assert _IDP_ORIGIN not in ctx.credential_origin_recovery_carded


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "status"), [("skip", "skipped"), (None, "unanswered")])
async def test_a_declined_origin_recovery_ends_the_turn_naming_the_missing_login(
    monkeypatch: pytest.MonkeyPatch, answer: str | None, status: str
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache(answer) if answer else _FakeCache())
    ctx.copilot_config = CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=1)

    result = await _ask(ctx, f"{_IDP_ORIGIN}/login")

    ctx.stream.send.assert_awaited_once()
    _assert_declined(ctx, result, status)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "declined"])
async def test_a_run_derived_pause_does_not_reopen_the_card_during_origin_recovery(
    monkeypatch: pytest.MonkeyPatch, state: CredentialOriginRecoveryState
) -> None:
    ctx = _recovery_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    ctx.credential_origin_recovery = replace(_PENDING, state=state)
    ctx.credential_pause_reaskable_by_run = True
    ctx.last_run_skipped_unbound_credentials = True
    ctx.request_policy.login_page_urls = [f"{_IDP_ORIGIN}/login"]
    _stub_credential_lookup(monkeypatch, _make_credential())

    resume = await maybe_credential_pause(ctx, _fake_result(), ctx.stream, ctx.copilot_config)

    assert resume is None
    ctx.stream.send.assert_not_awaited()
    assert "cred_1" not in ctx.request_policy.live_page_admitted_urls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_status"),
    [(("connected", "cred_1"), "updated"), (("skip", None), "skipped")],
)
async def test_a_site_rejected_credential_gets_an_update_card_after_a_pick_even_with_an_authenticator(
    monkeypatch: pytest.MonkeyPatch, answer: _CardAnswer, expected_status: str
) -> None:
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp="wrong-seed"))
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_1"), answer]))

    picked = await _call_ask_tool(ctx)
    policy_before = deepcopy(ctx.request_policy)
    update = await _call_ask_tool(ctx, credential_id="cred_1", rejected_by_site=True)

    assert (picked["status"], update["status"]) == ("connected", expected_status)
    assert [(card.reason, card.credential_refs) for card in _sent_cards(ctx)][1] == (
        "credential_rejected_by_site",
        ["cred_1"],
    )
    assert ctx.request_policy == policy_before
    assert ctx.credential_pause_outcome == "connected"
    assert (await _call_ask_tool(ctx, credential_id="cred_1", rejected_by_site=True))["status"] == "already_asked"


@pytest.mark.asyncio
async def test_a_saved_workflow_binding_the_chat_never_named_can_open_the_update_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    ctx = _tool_ctx(monkeypatch, cache)
    wire_credential_vault(monkeypatch, PasswordCredential(username="u", password="p", totp="wrong-seed"))
    ctx.request_policy.resolved_credentials = []
    ctx.request_policy.persisted_workflow_credential_ids = {"cred_1"}
    ctx.stream.send = AsyncMock(side_effect=_answer_each_card(cache, [("connected", "cred_1")]))

    update = await _call_ask_tool(ctx, credential_id="cred_1", rejected_by_site=True)

    assert update["status"] == "updated"
    assert [card.reason for card in _sent_cards(ctx)] == ["credential_rejected_by_site"]


_TERMINAL_SENTINEL = "Sentinel-Pw-7731"


@pytest.mark.asyncio
async def test_the_finalize_seam_opens_no_card_on_a_redacted_secret_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model owns the card on a raw-secret turn through request_credential; the finalizer never sequences it."""
    ctx = make_copilot_context()
    ctx.organization_id = "org-1"
    ctx.turn_id = "turn-1"
    ctx.workflow_copilot_chat_id = "chat-1"
    ctx.client_supports_credential_pause = True
    ctx.request_policy = _redacted_secret_policy("https://portal.example.com/login")
    ctx.last_run_skipped_unbound_credentials = True
    monkeypatch.setattr(
        credential_pause_module.app._inst, "CACHE", _answered_cache("connected", "cred_1"), raising=False
    )
    stream = _make_stream()

    resume = await maybe_credential_pause(
        ctx, _fake_result(), stream, CopilotConfig(credential_pause_enabled=True, credential_pause_timeout_seconds=5)
    )

    assert resume is None
    stream.send.assert_not_awaited()
    assert ctx.request_policy.allow_run_blocks is False


@pytest.mark.parametrize(
    ("user_url", "origin"),
    [
        (
            f"https://portal.example.com:8443/{_TERMINAL_SENTINEL}?p={_TERMINAL_SENTINEL}",
            "https://portal.example.com:8443",
        ),
        ("http://[::1]:8900/login", "http://[::1]:8900"),
        ("https://[2001:db8::1]/sign-in", "https://[2001:db8::1]"),
        ("www.portal.example.com/login", "https://www.portal.example.com"),
        (f"https://ops:{_TERMINAL_SENTINEL}@portal.example.com/login", ""),
    ],
    ids=["path_and_query", "ipv6_loopback_port", "ipv6", "schemeless", "userinfo"],
)
def test_the_raw_secret_card_sees_only_the_origin(user_url: str, origin: str) -> None:
    assert credential_pause_module.raw_secret_card_origin(user_url) == origin
