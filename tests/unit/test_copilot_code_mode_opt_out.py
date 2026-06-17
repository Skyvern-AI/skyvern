from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.turn_outcome import (
    derive_copilot_code_mode_diagnostics,
    with_copilot_code_mode_metadata,
)
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.routes.workflow_copilot import (
    COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON,
    _build_recoverable_route_agent_result,
    _capture_copilot_code_mode_opt_out,
    _effective_copilot_composer_mode,
    _reason_category_for_copilot_code_mode_opt_out,
    _resolve_copilot_code_available,
    _should_emit_copilot_code_mode_opt_out,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


def _request(mode: str | None, code_block: bool | None) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid-1",
        workflow_id="wf-1",
        workflow_copilot_chat_id="chat-1",
        message="message",
        workflow_yaml="title: Example",
        mode=mode,
        code_block=code_block,
    )


def _outcome(
    *,
    mode: str | None,
    code_available: bool = True,
    last_code_build_failed: bool = False,
    repair_ceiling_hit: bool = False,
    pending_capability: str | None = None,
    turn_id: str | None = "prior-turn",
) -> TurnOutcome:
    return TurnOutcome(
        response_kind=ResponseKind.BUILD,
        copilot_effective_mode=mode,
        copilot_code_available=code_available,
        copilot_last_code_build_failed=last_code_build_failed,
        copilot_repair_ceiling_hit=repair_ceiling_hit,
        copilot_pending_capability=pending_capability,
        copilot_turn_id=turn_id,
    )


@pytest.mark.parametrize(
    ("mode", "code_block", "uses_v2", "code_mode_fallback", "expected"),
    [
        ("ask", None, False, False, "ask"),
        ("build", None, True, True, "build"),
        ("build", False, True, True, "build"),
        ("build", True, True, False, "code"),
        (None, True, True, False, "code"),
        (None, False, True, True, "build"),
        (None, None, True, False, "build"),
        (None, None, True, True, "code"),
        (None, None, False, True, "ask"),
    ],
)
def test_effective_copilot_composer_mode(
    mode: str | None, code_block: bool | None, uses_v2: bool, code_mode_fallback: bool, expected: str
) -> None:
    assert (
        _effective_copilot_composer_mode(
            _request(mode, code_block),
            uses_v2=uses_v2,
            code_mode_fallback=code_mode_fallback,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("prior", "to_mode", "current_code_available", "expected"),
    [
        (_outcome(mode="code"), "build", True, True),
        (_outcome(mode="code"), "ask", False, True),
        (_outcome(mode="build", code_available=True), "ask", False, True),
        (_outcome(mode="build", code_available=False), "ask", True, True),
        (_outcome(mode="build", code_available=False), "ask", False, False),
        (_outcome(mode="code"), "code", True, False),
        (_outcome(mode="build", code_available=True), "build", True, False),
        (_outcome(mode="ask", code_available=True), "build", True, False),
        (None, "ask", True, False),
        (_outcome(mode=None, code_available=True), "ask", True, False),
    ],
)
def test_should_emit_copilot_code_mode_opt_out_transitions(
    prior: TurnOutcome | None,
    to_mode: str,
    current_code_available: bool,
    expected: bool,
) -> None:
    assert (
        _should_emit_copilot_code_mode_opt_out(
            prior_turn_outcome=prior,
            to_mode=to_mode,
            current_code_available=current_code_available,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("prior", "expected"),
    [
        (_outcome(mode="code", last_code_build_failed=True, pending_capability="capability"), "failure"),
        (_outcome(mode="code", repair_ceiling_hit=True, pending_capability="capability"), "failure"),
        (
            TurnOutcome(
                response_kind=ResponseKind.RECOVER,
                copilot_effective_mode="code",
                terminal_reason=COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON,
                copilot_pending_capability="capability",
            ),
            "failure",
        ),
        (_outcome(mode="code", pending_capability="capability"), "missing_capability"),
        (_outcome(mode="code"), "confusion"),
    ],
)
def test_reason_category_for_copilot_code_mode_opt_out(prior: TurnOutcome, expected: str) -> None:
    assert _reason_category_for_copilot_code_mode_opt_out(prior) == expected


def test_capture_copilot_code_mode_opt_out_uses_chat_id_as_distinct_id(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = MagicMock()
    monkeypatch.setattr(workflow_copilot_route.analytics, "capture", capture)

    prior = _outcome(
        mode="code",
        last_code_build_failed=True,
        repair_ceiling_hit=False,
        pending_capability="credential-typed code synthesis",
        turn_id="turn-prior",
    )

    _capture_copilot_code_mode_opt_out(
        prior_turn_outcome=prior,
        to_mode="ask",
        current_code_available=True,
        workflow_copilot_chat_id="chat-123",
        workflow_permanent_id="wpid-123",
        organization_id="org-123",
        turn_id="turn-current",
    )

    capture.assert_called_once_with(
        "copilot_code_mode_opt_out",
        data={
            "from_mode": "code",
            "to_mode": "ask",
            "reason_category": "failure",
            "last_code_build_failed": True,
            "repair_ceiling_hit": False,
            "pending_capability": "credential-typed code synthesis",
            "org_id": "org-123",
            "workflow_permanent_id": "wpid-123",
            "workflow_copilot_chat_id": "chat-123",
            "turn_id": "turn-current",
            "prior_turn_id": "turn-prior",
        },
        distinct_id="chat-123",
    )


def test_capture_copilot_code_mode_opt_out_skips_non_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = MagicMock()
    monkeypatch.setattr(workflow_copilot_route.analytics, "capture", capture)

    _capture_copilot_code_mode_opt_out(
        prior_turn_outcome=_outcome(mode="build", code_available=False),
        to_mode="ask",
        current_code_available=False,
        workflow_copilot_chat_id="chat-123",
        workflow_permanent_id="wpid-123",
        organization_id="org-123",
        turn_id="turn-current",
    )

    capture.assert_not_called()


def test_build_recoverable_route_agent_result_sets_failure_turn_outcome() -> None:
    agent_result, failure = _build_recoverable_route_agent_result(
        RuntimeError("boom"),
        workflow_modified=False,
        clear_proposed_workflow=False,
        global_llm_context=None,
        turn_id="turn-error",
        turn_index=2,
    )

    assert agent_result.turn_outcome is not None
    assert agent_result.turn_outcome.response_kind is ResponseKind.RECOVER
    assert agent_result.turn_outcome.reason_code == failure.failure_kind
    assert agent_result.turn_outcome.terminal_reason == COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON
    assert _reason_category_for_copilot_code_mode_opt_out(agent_result.turn_outcome) == "failure"


@pytest.mark.asyncio
async def test_resolve_copilot_code_available_uses_access_and_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    agent_function = SimpleNamespace(has_code_block_access=AsyncMock(), get_copilot_config_for_request=AsyncMock())
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    agent_function.has_code_block_access.return_value = False
    assert await _resolve_copilot_code_available("org-1", _request("build", False)) is False
    agent_function.get_copilot_config_for_request.assert_not_awaited()

    agent_function.has_code_block_access.return_value = True
    agent_function.get_copilot_config_for_request.return_value = config
    assert await _resolve_copilot_code_available("org-1", _request("ask", None)) is True


def test_with_copilot_code_mode_metadata_preserves_turn_outcome_fields() -> None:
    outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        reason_code="request_policy_clarification",
        terminal_reason="terminal",
    )

    updated = with_copilot_code_mode_metadata(
        outcome,
        effective_mode="build",
        code_available=True,
        turn_id="turn-123",
    )

    assert updated.response_kind == ResponseKind.CLARIFY
    assert updated.reason_code == "request_policy_clarification"
    assert updated.terminal_reason == "terminal"
    assert updated.copilot_effective_mode == "build"
    assert updated.copilot_code_available is True
    assert updated.copilot_turn_id == "turn-123"


def test_derive_copilot_code_mode_diagnostics_uses_context_state() -> None:
    ctx = type("Ctx", (), {})()
    ctx.last_test_ok = False
    ctx.last_failed_workflow_yaml = None
    ctx.code_native_pending_capability = "credential-typed code synthesis"
    ctx.turn_halt = type("Halt", (), {"kind": type("Kind", (), {"value": "repair_ceiling_reached"})()})()

    diagnostics = derive_copilot_code_mode_diagnostics(ctx)

    assert diagnostics == {
        "copilot_last_code_build_failed": True,
        "copilot_repair_ceiling_hit": True,
        "copilot_pending_capability": "credential-typed code synthesis",
    }
