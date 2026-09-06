from __future__ import annotations

from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from skyvern.config import CodeBlockMode, settings
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.config import (
    BlockAuthoringPolicy,
    CopilotConfig,
    block_authoring_policy_for_request,
    block_authoring_policy_from_code_only_mode,
    download_scout_act_required_for_policy,
)
from skyvern.forge.sdk.copilot.turn_outcome import (
    derive_copilot_code_mode_diagnostics,
    with_copilot_code_mode_metadata,
)
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.routes.workflow_copilot import (
    COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON,
    _build_recoverable_route_agent_result,
    _capture_copilot_code_mode_opt_out,
    _effective_copilot_build_mode,
    _reason_category_for_copilot_code_mode_opt_out,
    _resolve_copilot_request_config,
    _should_emit_copilot_code_mode_opt_out,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice, ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


def _request(mode: Literal["build"] | None, code_block: bool | None) -> WorkflowCopilotChatRequest:
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
    pending_capability: str | None = None,
    turn_id: str | None = "prior-turn",
) -> TurnOutcome:
    return TurnOutcome(
        response_kind=ResponseKind.BUILD,
        copilot_effective_mode=mode,
        copilot_code_available=code_available,
        copilot_last_code_build_failed=last_code_build_failed,
        copilot_pending_capability=pending_capability,
        copilot_turn_id=turn_id,
    )


@pytest.mark.parametrize(
    ("mode", "code_block", "code_mode_fallback", "expected"),
    [
        ("build", None, True, "code"),
        ("build", False, True, "build"),
        ("build", True, False, "code"),
        (None, True, False, "code"),
        (None, False, True, "build"),
        (None, None, False, "build"),
        (None, None, True, "code"),
    ],
)
def test_effective_copilot_build_mode(
    mode: str | None, code_block: bool | None, code_mode_fallback: bool, expected: str
) -> None:
    assert (
        _effective_copilot_build_mode(
            _request(mode, code_block),
            code_mode_fallback=code_mode_fallback,
        )
        == expected
    )


def test_chat_request_rejects_removed_ask_mode() -> None:
    with pytest.raises(ValidationError):
        WorkflowCopilotChatRequest.model_validate(
            {
                "workflow_permanent_id": "wpid-1",
                "workflow_id": "wf-1",
                "workflow_copilot_chat_id": "chat-1",
                "message": "message",
                "workflow_yaml": "title: Example",
                "mode": "ask",
            }
        )


@pytest.mark.parametrize(
    ("prior", "to_mode", "expected"),
    [
        (_outcome(mode="code"), "build", True),
        (_outcome(mode="code"), "code", False),
        (_outcome(mode="build", code_available=True), "build", False),
        (_outcome(mode="ask", code_available=True), "build", False),
        (None, "build", False),
        (_outcome(mode=None, code_available=True), "build", False),
    ],
)
def test_should_emit_copilot_code_mode_opt_out_transitions(
    prior: TurnOutcome | None,
    to_mode: str,
    expected: bool,
) -> None:
    assert (
        _should_emit_copilot_code_mode_opt_out(
            prior_turn_outcome=prior,
            to_mode=to_mode,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("prior", "expected"),
    [
        (_outcome(mode="code", last_code_build_failed=True, pending_capability="capability"), "failure"),
        (_outcome(mode="code", last_code_build_failed=True, pending_capability="capability"), "failure"),
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
        pending_capability="credential-typed code synthesis",
        turn_id="turn-prior",
    )

    _capture_copilot_code_mode_opt_out(
        prior_turn_outcome=prior,
        to_mode="build",
        workflow_copilot_chat_id="chat-123",
        workflow_permanent_id="wpid-123",
        organization_id="org-123",
        turn_id="turn-current",
    )

    capture.assert_called_once_with(
        "copilot_code_mode_opt_out",
        data={
            "from_mode": "code",
            "to_mode": "build",
            "reason_category": "failure",
            "last_code_build_failed": True,
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
        to_mode="build",
        workflow_copilot_chat_id="chat-123",
        workflow_permanent_id="wpid-123",
        organization_id="org-123",
        turn_id="turn-current",
    )

    capture.assert_not_called()


def test_build_recoverable_route_agent_result_sets_failure_turn_outcome() -> None:
    choices = [
        ConnectedAccountChoice(
            connection_id="goac_1",
            name="Google Sheets",
            state="active",
            email_address="first@example.test",
        )
    ]
    agent_result, failure = _build_recoverable_route_agent_result(
        RuntimeError("boom"),
        workflow_modified=False,
        clear_proposed_workflow=False,
        global_llm_context=None,
        turn_id="turn-error",
        turn_index=2,
        prior_turn_outcome=TurnOutcome(
            response_kind=ResponseKind.CLARIFY,
            connected_account_choices=choices,
        ),
    )

    assert agent_result.turn_outcome is not None
    assert agent_result.turn_outcome.response_kind is ResponseKind.RECOVER
    assert agent_result.turn_outcome.reason_code == failure.failure_kind
    assert agent_result.turn_outcome.terminal_reason == COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON
    assert agent_result.turn_outcome.connected_account_choices == choices
    assert agent_result.narrative_payload is not None
    assert agent_result.narrative_payload["connectedAccountChoices"] == [
        choice.model_dump(mode="json") for choice in choices
    ]
    assert _reason_category_for_copilot_code_mode_opt_out(agent_result.turn_outcome) == "failure"


@pytest.mark.asyncio
async def test_resolve_copilot_request_config_uses_single_resolved_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CopilotConfig(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_block_available=True,
        effective_code_block_mode=True,
    )
    agent_function = SimpleNamespace(get_copilot_config_for_request=AsyncMock(return_value=config))
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    resolved = await _resolve_copilot_request_config("org-1", _request("build", None))

    assert resolved is config
    agent_function.get_copilot_config_for_request.assert_awaited_once_with("org-1", code_block_mode=None)


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
    assert updated.copilot_runtime == "agent"
    assert updated.copilot_effective_mode == "build"
    assert updated.copilot_code_available is True
    assert updated.copilot_turn_id == "turn-123"


def test_derive_copilot_code_mode_diagnostics_uses_context_state() -> None:
    ctx = SimpleNamespace(
        last_test_ok=False,
        last_failed_workflow_yaml=None,
        code_native_pending_capability="credential-typed code synthesis",
        turn_halt=SimpleNamespace(kind=SimpleNamespace(value="loop_detected")),
    )

    assert derive_copilot_code_mode_diagnostics(ctx) == {
        "copilot_last_code_build_failed": True,
        "copilot_pending_capability": "credential-typed code synthesis",
    }


def test_derive_copilot_code_mode_diagnostics_on_a_clean_turn() -> None:
    ctx = SimpleNamespace(
        last_test_ok=True,
        last_failed_workflow_yaml=None,
        code_native_pending_capability=None,
        turn_halt=None,
    )

    assert derive_copilot_code_mode_diagnostics(ctx) == {
        "copilot_last_code_build_failed": False,
        "copilot_pending_capability": None,
    }


def test_copilot_config_defaults_to_standard_policy() -> None:
    assert CopilotConfig().block_authoring_policy == BlockAuthoringPolicy.STANDARD


def test_code_block_settings_helper_selects_policy() -> None:
    assert block_authoring_policy_from_code_only_mode(True) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
    assert block_authoring_policy_from_code_only_mode(False) == BlockAuthoringPolicy.STANDARD


def test_build_with_unspecified_code_mode_is_non_code() -> None:
    assert block_authoring_policy_for_request(None) == BlockAuthoringPolicy.TASK_V3_PURE


def test_build_with_explicit_code_opt_out_is_non_code() -> None:
    assert block_authoring_policy_for_request(False) == BlockAuthoringPolicy.TASK_V3_PURE


def test_build_with_explicit_code_selection_is_code_first() -> None:
    assert block_authoring_policy_for_request(True) == BlockAuthoringPolicy.CODE_ONLY_BROWSER


def test_download_scout_act_requirement_follows_code_only_policy() -> None:
    assert download_scout_act_required_for_policy(BlockAuthoringPolicy.CODE_ONLY_BROWSER) is True
    assert download_scout_act_required_for_policy("code_only_browser") is True
    assert download_scout_act_required_for_policy(None) is False
    assert download_scout_act_required_for_policy(BlockAuthoringPolicy.STANDARD) is False


def test_base_agent_function_honors_code_block_mode_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

    config = AgentFunction().get_copilot_config()

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER


@pytest.mark.asyncio
async def test_base_agent_function_request_config_requires_explicit_code_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

    config = await AgentFunction().get_copilot_config_for_request("o_test")

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.TASK_V3_PURE
    assert config.code_block_available is False
    assert config.effective_code_block_mode is False


@pytest.mark.asyncio
async def test_request_config_snapshots_entitlement_once_for_explicit_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = AgentFunction()
    access = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_function, "has_code_block_access", access)

    config = await agent_function.get_copilot_config_for_request("o_test", code_block_mode=False)

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.TASK_V3_PURE
    assert config.code_block_available is True
    assert config.effective_code_block_mode is False
    access.assert_awaited_once_with("o_test")


@pytest.mark.asyncio
async def test_base_agent_function_request_config_honors_code_block_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)
    monkeypatch.setattr(settings, "CODE_BLOCK_MODE", CodeBlockMode.disabled)

    config = await AgentFunction().get_copilot_config_for_request(
        "o_test",
        code_block_mode=True,
    )

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.TASK_V3_PURE


@pytest.mark.asyncio
async def test_base_agent_function_explicit_code_mode_uses_available_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", False)
    monkeypatch.setattr(settings, "CODE_BLOCK_MODE", CodeBlockMode.enabled)

    config = await AgentFunction().get_copilot_config_for_request(
        "o_test",
        code_block_mode=True,
    )

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER


@pytest.mark.asyncio
async def test_base_agent_function_build_without_code_access_selects_task_v3_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", False)

    config = await AgentFunction().get_copilot_config_for_request(
        "o_test",
        code_block_mode=None,
    )

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.TASK_V3_PURE


@pytest.mark.asyncio
async def test_base_request_config_preserves_get_copilot_config_override() -> None:
    agent_function = AgentFunction()
    delegated_config = CopilotConfig()
    agent_function.get_copilot_config = MagicMock(return_value=delegated_config)  # type: ignore[method-assign]

    config = await agent_function.get_copilot_config_for_request(
        "o_test",
        code_block_mode=False,
    )

    assert config is delegated_config
    assert config.block_authoring_policy == BlockAuthoringPolicy.TASK_V3_PURE
    agent_function.get_copilot_config.assert_called_once_with(False)
