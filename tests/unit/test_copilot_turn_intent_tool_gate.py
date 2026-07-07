from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.agent import _mcp_tool_surface_for_turn, _native_tools_for_turn
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import (
    NATIVE_TOOLS,
    _authority_tool_error,
    _build_skyvern_mcp_overlays,
    _get_run_results,
    _turn_intent_tool_error,
    _update_workflow,
    get_skyvern_mcp_alias_map,
)
from skyvern.forge.sdk.copilot.turn_intent import (
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentMode,
)

# Tokens that must never appear in any LLM-visible string emitted by an
# authority gate. The previous prose path leaked all of these.
_LEAK_TOKENS = ("safe_reason_code", "`TurnIntent`", "TurnIntent classified")


def _ctx(
    turn_intent: TurnIntent,
    request_policy: RequestPolicy | None = None,
    *,
    pending_reconciliation_run_id: str | None = None,
    last_run_blocks_workflow_run_id: str | None = None,
    last_successful_run_blocks_workflow_run_id: str | None = None,
    tool_activity: list[dict] | None = None,
) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        turn_intent=turn_intent,
        request_policy=request_policy,
    )
    if pending_reconciliation_run_id is not None:
        ctx.pending_reconciliation_run_id = pending_reconciliation_run_id
    if last_run_blocks_workflow_run_id is not None:
        ctx.last_run_blocks_workflow_run_id = last_run_blocks_workflow_run_id
    if last_successful_run_blocks_workflow_run_id is not None:
        ctx.last_successful_run_blocks_workflow_run_id = last_successful_run_blocks_workflow_run_id
    if tool_activity is not None:
        ctx.tool_activity = tool_activity
    return ctx


def _assert_signal(
    signal: CopilotToolBlockerSignal | None,
    *,
    internal_reason_code: str,
    classifier_mode: str | None = None,
    blocked_tool: str,
) -> CopilotToolBlockerSignal:
    assert signal is not None
    assert signal.blocker_kind == "authority_denied"
    assert signal.internal_reason_code == internal_reason_code
    assert signal.blocked_tool == blocked_tool
    if classifier_mode is not None:
        assert signal.classifier_mode == classifier_mode
    for token in _LEAK_TOKENS:
        assert token not in signal.user_facing_reason
    return signal


@pytest.mark.parametrize(
    ("mode", "tool_name", "expected_reason"),
    [
        (TurnIntentMode.DOCS_ANSWER, "update_workflow", "turn_intent_no_mutation_update_blocked"),
        (TurnIntentMode.DOCS_ANSWER, "inspect_page_for_composition", "turn_intent_page_inspection_blocked"),
        (TurnIntentMode.DOCS_ANSWER, "list_credentials", "turn_intent_credential_metadata_blocked"),
        (TurnIntentMode.CLARIFY, "inspect_page_for_composition", "turn_intent_page_inspection_blocked"),
        (TurnIntentMode.CLARIFY, "update_and_run_blocks", "turn_intent_no_mutation_run_blocked"),
        (TurnIntentMode.REFUSE, "update_and_run_blocks", "turn_intent_no_mutation_run_blocked"),
    ],
)
def test_no_mutation_turn_intent_blocks_mutating_tools(
    mode: TurnIntentMode, tool_name: str, expected_reason: str
) -> None:
    intent = TurnIntent(
        mode=mode,
        authority=TurnIntentAuthority(
            may_update_workflow=False,
            may_run_blocks=False,
            requires_user_input=mode in {TurnIntentMode.CLARIFY, TurnIntentMode.REFUSE},
        ),
        missing_context_question="Which target should I use?" if mode == TurnIntentMode.CLARIFY else None,
    )

    ctx = _ctx(intent)
    signal = _turn_intent_tool_error(ctx, tool_name)
    _assert_signal(signal, internal_reason_code=expected_reason, classifier_mode=mode.value, blocked_tool=tool_name)


def _diagnose_intent() -> TurnIntent:
    return TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(
            may_update_workflow=False,
            may_run_blocks=False,
            may_read_run_context=True,
        ),
    )


@pytest.mark.parametrize(
    ("tool_name", "expected_reason"),
    [
        ("run_blocks_and_collect_debug", "turn_intent_no_mutation_run_blocked"),
        ("update_and_run_blocks", "turn_intent_no_mutation_run_blocked"),
        ("update_workflow", "turn_intent_no_mutation_update_blocked"),
        ("inspect_page_for_composition", "turn_intent_page_inspection_blocked"),
        ("list_credentials", "turn_intent_credential_metadata_blocked"),
    ],
)
def test_diagnose_denied_tool_soft_blocks_and_continues_to_diagnosis(tool_name: str, expected_reason: str) -> None:
    # A denied tool in a diagnose turn stays denied but must not terminate the turn: the blocker is a
    # non-terminating soft steer toward an evidence-grounded fix, covering every denied tool class.
    signal = _turn_intent_tool_error(_ctx(_diagnose_intent()), tool_name)
    assert signal is not None
    assert signal.internal_reason_code == expected_reason
    assert signal.blocked_tool == tool_name
    assert signal.classifier_mode == TurnIntentMode.DIAGNOSE.value
    # the load-bearing assertions: soft, non-terminating, steered at the run evidence + a confirmable fix
    assert signal.blocker_kind == "tool_error"
    assert signal.renders_final_reply is False
    assert signal.recovery_hint == "retry_with_different_tool"
    steer = signal.agent_steering_text.lower()
    assert "diagnose" in steer and "fix" in steer
    assert "i'll respond with the information i already have" not in signal.user_facing_reason.lower()
    for token in _LEAK_TOKENS:
        assert token not in signal.user_facing_reason


@pytest.mark.parametrize("tool_name", ["update_workflow", "update_and_run_blocks", "run_blocks_and_collect_debug"])
def test_diagnose_soft_block_still_denies_side_effect(tool_name: str) -> None:
    # A non-terminating soft block still returns a blocker, so the side-effecting tool is denied and never runs;
    # renders_final_reply governs only whether the turn ends, not whether the side effect runs.
    signal = _turn_intent_tool_error(_ctx(_diagnose_intent()), tool_name)
    assert signal is not None
    assert tool_name not in signal.cleared_by_tools


@pytest.mark.parametrize(
    ("mode", "authority_kwargs", "target_entities", "tool_name"),
    [
        pytest.param(
            TurnIntentMode.DRAFT_ONLY,
            {"may_update_workflow": True, "may_run_blocks": False},
            {},
            "update_workflow",
            id="draft_update_without_run_authority",
        ),
        pytest.param(
            TurnIntentMode.BUILD,
            {"may_update_workflow": True, "may_run_blocks": False},
            {},
            "inspect_page_for_composition",
            id="build_page_inspection_with_update_authority",
        ),
        pytest.param(
            TurnIntentMode.BUILD,
            {"may_update_workflow": True, "may_run_blocks": False},
            {},
            "list_credentials",
            id="credential_metadata_with_update_authority",
        ),
        pytest.param(
            TurnIntentMode.BUILD,
            {"may_update_workflow": True, "may_run_blocks": True},
            {},
            "update_and_run_blocks",
            id="build_update_and_run_authority",
        ),
        pytest.param(
            TurnIntentMode.EDIT,
            {"may_update_workflow": True, "may_run_blocks": True},
            {"workflow_change": ["add_invoice_download_step"]},
            "update_and_run_blocks",
            id="edit_with_target_context",
        ),
        pytest.param(
            TurnIntentMode.DIAGNOSE,
            {"may_update_workflow": False, "may_run_blocks": True, "may_read_run_context": True},
            {},
            "run_blocks_and_collect_debug",
            id="diagnose_run_with_retest_authority",
        ),
        pytest.param(
            TurnIntentMode.DIAGNOSE,
            {"may_update_workflow": False, "may_run_blocks": False, "may_read_run_context": True},
            {},
            "get_run_results",
            id="diagnose_allows_get_run_results_tool",
        ),
    ],
)
def test_turn_intent_gate_allows_authorized_tool(
    mode: TurnIntentMode,
    authority_kwargs: dict[str, bool],
    target_entities: dict[str, list[str]],
    tool_name: str,
) -> None:
    intent = TurnIntent(
        mode=mode,
        target_entities=target_entities,
        authority=TurnIntentAuthority(**authority_kwargs),
    )

    assert _turn_intent_tool_error(_ctx(intent), tool_name) is None


def test_draft_only_credential_code_policy_prunes_browser_tool_surface() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DRAFT_ONLY,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )
    policy = RequestPolicy(
        testing_intent="skip_test",
        allow_update_workflow=True,
        allow_run_blocks=False,
        allow_missing_credentials_in_draft=True,
    )

    alias_map, overlays = _mcp_tool_surface_for_turn(
        get_skyvern_mcp_alias_map(),
        _build_skyvern_mcp_overlays(),
        intent,
        policy,
    )
    native_names = {getattr(tool, "name", None) for tool in _native_tools_for_turn(list(NATIVE_TOOLS), intent, policy)}

    assert set(alias_map) == {"get_block_schema", "validate_block"}
    assert set(overlays) == {"get_block_schema", "validate_block"}
    for browser_tool in {"navigate_browser", "evaluate", "click", "type_text"}:
        assert browser_tool not in alias_map
        assert browser_tool not in overlays
    assert "update_workflow" in native_names
    assert "list_credentials" in native_names
    assert "fill_credential_field" not in native_names
    assert "discover_workflow_entrypoint" not in native_names
    assert "inspect_page_for_composition" not in native_names


def test_turn_intent_gate_blocks_draft_only_run_tools() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DRAFT_ONLY,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )

    signal = _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_run_blocked",
        classifier_mode="draft_only",
        blocked_tool="update_and_run_blocks",
    )
    assert signal is not None
    assert signal.recovery_hint == "retry_with_different_tool"
    assert "update_workflow" in signal.cleared_by_tools


def test_turn_intent_gate_blocks_edit_without_target_context() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )

    signal = _turn_intent_tool_error(_ctx(intent), "update_workflow")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_missing_edit_target",
        classifier_mode="edit",
        blocked_tool="update_workflow",
    )
    assert signal is not None
    assert signal.recovery_hint == "ask_user_clarifying"


def test_turn_intent_gate_blocks_edit_with_default_current_workflow_only() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow": ["current_workflow"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )

    signal = _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks")

    _assert_signal(
        signal,
        internal_reason_code="turn_intent_missing_edit_target",
        classifier_mode="edit",
        blocked_tool="update_and_run_blocks",
    )


def test_turn_intent_gate_blocks_edit_with_unresolved_label_reference() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={
            "workflow": ["current_workflow"],
            UNRESOLVED_BLOCK_REF_TARGET_ENTITY: ["WF_trigger_SSO_login", "update_card"],
        },
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "WF_trigger_SSO_login worked but update_card is not receiving browser state."
    ctx.workflow_yaml = """
title: Public SSO login cleanup
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: account_number
  blocks:
    - block_type: goto_url
      label: navigate_to_SSO
      url: https://the-internet.herokuapp.com/login
    - block_type: navigation
      label: block_placeholder
      navigation_goal: Confirm success.
"""

    signal = _turn_intent_tool_error(ctx, "update_and_run_blocks")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_unresolved_edit_target",
        classifier_mode="edit",
        blocked_tool="update_and_run_blocks",
    )
    assert signal is not None
    # The agent steering text still names the missing refs so the agent can ask
    # specifically; the user_facing_reason names them too.
    assert "WF_trigger_SSO_login" in signal.agent_steering_text
    assert "update_card" in signal.agent_steering_text
    assert "navigate_to_SSO" in signal.agent_steering_text
    assert "WF_trigger_SSO_login" in signal.user_facing_reason


def test_turn_intent_gate_does_not_scan_raw_user_message_for_snake_case_refs() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow_change": ["extract_last_name"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "Update the workflow so last_name is extracted as a required field."

    assert _turn_intent_tool_error(ctx, "update_and_run_blocks") is None


def test_turn_intent_gate_allows_edit_with_parameter_reference() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow_change": ["use_account_number_in_search"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "Update the current workflow so account_number is used in the search step."
    ctx.workflow_yaml = """
title: Existing
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: account_number
  blocks:
    - block_type: navigation
      label: search_account
      navigation_goal: Search for the account.
"""

    assert _turn_intent_tool_error(ctx, "update_and_run_blocks") is None


def test_turn_intent_gate_preserves_request_policy_update_skip_path() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )
    policy = RequestPolicy(
        allow_update_workflow=True,
        allow_run_blocks=False,
        allow_missing_credentials_in_draft=True,
        clarification_reason="workflow_credential_inputs_unbound",
    )

    assert _turn_intent_tool_error(_ctx(intent, policy), "update_and_run_blocks") is None


@pytest.mark.asyncio
async def test_update_workflow_stops_before_persisting_for_answer_only_intent() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DOCS_ANSWER,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )
    ctx = _ctx(intent)

    with patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app:
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": ctx.workflow_yaml}, ctx)

    assert result["ok"] is False
    # LLM-visible error is agent_steering_text — product-language imperative,
    # no internal vocabulary.
    for token in _LEAK_TOKENS:
        assert token not in result["error"]
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.classifier_mode == "docs_answer"
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()


@pytest.mark.asyncio
async def test_request_policy_refusal_wins_even_when_turn_intent_allows_update() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent, RequestPolicy(allow_update_workflow=False, allow_run_blocks=False))

    with patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app:
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": ctx.workflow_yaml}, ctx)

    assert result["ok"] is False
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "request_policy_blocks_update_workflow"
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()


def test_authority_dispatcher_stashes_request_policy_signal_when_both_block() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )
    policy = RequestPolicy(allow_update_workflow=False, allow_run_blocks=False)
    ctx = _ctx(intent, policy)

    payload = _authority_tool_error(ctx, "update_workflow")
    assert payload is not None
    assert ctx.blocker_signal is not None
    # Request policy precedence — turn-intent signal must not win the stash.
    assert ctx.blocker_signal.internal_reason_code == "request_policy_blocks_update_workflow"
    for token in _LEAK_TOKENS:
        assert token not in payload


def test_update_and_run_blocks_reports_both_blocked_authorities() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )
    signal = _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_no_mutation_run_blocked",
        classifier_mode="diagnose",
        blocked_tool="update_and_run_blocks",
    )


def test_get_run_results_routes_through_authority_dispatcher_but_request_policy_does_not_gate_it() -> None:
    """Pins the dispatcher routing: `get_run_results` flows through `_authority_tool_error` (not `_turn_intent_tool_error` directly), and current request-policy scope (`update_workflow` + `BLOCK_RUNNING_TOOLS`) does not include `get_run_results`, so the call passes through."""
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_read_run_context=True),
    )
    policy = RequestPolicy(allow_update_workflow=False, allow_run_blocks=False)
    ctx = _ctx(intent, policy)
    assert _authority_tool_error(ctx, "get_run_results") is None
    assert ctx.blocker_signal is None


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(TurnIntentMode.UNKNOWN, id="unknown_without_run_context"),
        pytest.param(TurnIntentMode.DOCS_ANSWER, id="docs_answer"),
    ],
)
def test_no_run_context_mode_blocks_get_run_results(mode: TurnIntentMode) -> None:
    intent = TurnIntent(
        mode=mode,
        authority=TurnIntentAuthority(),
    )

    signal = _turn_intent_tool_error(_ctx(intent), "get_run_results")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_context_read_blocked",
        classifier_mode=mode.value,
        blocked_tool="get_run_results",
    )


def test_docs_answer_blocks_get_run_results_even_with_read_flag() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DOCS_ANSWER,
        authority=TurnIntentAuthority(may_read_run_context=True),
    )

    signal = _turn_intent_tool_error(_ctx(intent), "get_run_results")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_context_read_blocked",
        classifier_mode="docs_answer",
        blocked_tool="get_run_results",
    )
    filtered = _native_tools_for_turn(list(NATIVE_TOOLS), intent)
    assert {getattr(tool, "name", None) for tool in filtered} == {tool.name for tool in NATIVE_TOOLS}


@pytest.mark.parametrize(
    ("mode", "authority_kwargs", "ctx_kwargs"),
    [
        pytest.param(
            TurnIntentMode.UNKNOWN,
            {},
            {"pending_reconciliation_run_id": "wr_pending_test"},
            id="pending_reconciliation_allows_read",
        ),
        pytest.param(
            TurnIntentMode.BUILD,
            {"may_update_workflow": True, "may_run_blocks": True},
            {"last_successful_run_blocks_workflow_run_id": "wr_completed_this_turn"},
            id="successful_run_blocks_allows_read",
        ),
        pytest.param(
            TurnIntentMode.BUILD,
            {"may_update_workflow": True, "may_run_blocks": True},
            {"last_run_blocks_workflow_run_id": "wr_failed_this_turn"},
            id="failed_run_blocks_allows_read",
        ),
    ],
)
def test_within_turn_override_allows_read(
    mode: TurnIntentMode,
    authority_kwargs: dict[str, bool],
    ctx_kwargs: dict[str, str],
) -> None:
    intent = TurnIntent(
        mode=mode,
        authority=TurnIntentAuthority(**authority_kwargs),
    )

    ctx = _ctx(intent, **ctx_kwargs)

    assert _turn_intent_tool_error(ctx, "get_run_results") is None


def test_within_turn_override_excluded_for_docs_answer() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DOCS_ANSWER,
        authority=TurnIntentAuthority(),
    )

    ctx = _ctx(intent, pending_reconciliation_run_id="wr_pending_test")
    signal = _turn_intent_tool_error(ctx, "get_run_results")
    _assert_signal(
        signal,
        internal_reason_code="turn_intent_context_read_blocked",
        classifier_mode="docs_answer",
        blocked_tool="get_run_results",
    )


def test_tool_activity_is_not_a_substitute_for_pending_reconciliation_run_id() -> None:
    # tool_activity entries are appended for every completed tool call,
    # including ones that failed the authority/loop gate before the run ever
    # started. The override must key only on pending_reconciliation_run_id,
    # which the watchdog sets only when a real run exited unfinalized.
    intent = TurnIntent(
        mode=TurnIntentMode.UNKNOWN,
        authority=TurnIntentAuthority(),
    )

    ctx = _ctx(
        intent,
        tool_activity=[{"tool": "run_blocks_and_collect_debug", "summary": "Failed: blocked"}],
    )

    assert _turn_intent_tool_error(ctx, "get_run_results") is not None


@pytest.mark.asyncio
async def test_get_run_results_defaults_to_successful_same_turn_run() -> None:
    ctx = _ctx(
        TurnIntent(mode=TurnIntentMode.BUILD, authority=TurnIntentAuthority()),
        last_successful_run_blocks_workflow_run_id="wr_completed_this_turn",
    )
    run = SimpleNamespace(workflow_run_id="wr_completed_this_turn", workflow_permanent_id="wfp-1", status="completed")

    with patch("skyvern.forge.sdk.copilot.tools.run_execution.app") as mock_app:
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=run)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(return_value=[])
        mock_app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id = AsyncMock()
        mock_app.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker = AsyncMock(return_value=False)

        result = await _get_run_results({}, ctx)

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr_completed_this_turn"
    mock_app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id.assert_not_called()


@pytest.mark.asyncio
async def test_get_run_results_defaults_to_latest_same_turn_run_when_no_success() -> None:
    ctx = _ctx(
        TurnIntent(mode=TurnIntentMode.BUILD, authority=TurnIntentAuthority()),
        last_run_blocks_workflow_run_id="wr_failed_this_turn",
    )
    run = SimpleNamespace(workflow_run_id="wr_failed_this_turn", workflow_permanent_id="wfp-1", status="canceled")

    with patch("skyvern.forge.sdk.copilot.tools.run_execution.app") as mock_app:
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=run)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(return_value=[])
        mock_app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id = AsyncMock()
        mock_app.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker = AsyncMock(return_value=False)

        result = await _get_run_results({}, ctx)

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr_failed_this_turn"
    mock_app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id.assert_not_called()


def test_recovery_diagnose_keeps_all_native_tools_registered() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(
            may_update_workflow=False,
            may_run_blocks=False,
            may_read_run_context=True,
        ),
    )

    filtered = _native_tools_for_turn(list(NATIVE_TOOLS), intent)
    names = {getattr(tool, "name", None) for tool in filtered}

    assert names == {tool.name for tool in NATIVE_TOOLS}


@pytest.mark.asyncio
async def test_get_run_results_rejects_explicit_run_from_other_workflow() -> None:
    ctx = _ctx(
        TurnIntent(mode=TurnIntentMode.DIAGNOSE, authority=TurnIntentAuthority(may_read_run_context=True)),
        pending_reconciliation_run_id="wr_other",
    )
    run = SimpleNamespace(workflow_run_id="wr_other", workflow_permanent_id="wfp-other", status="failed")

    with patch("skyvern.forge.sdk.copilot.tools.run_execution.app") as mock_app:
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=run)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock()
        result = await _get_run_results({"workflow_run_id": "wr_other"}, ctx)

    assert result == {"ok": False, "error": "Workflow run not found for this workflow: wr_other"}
    mock_app.DATABASE.observer.get_workflow_run_blocks.assert_not_called()


@pytest.mark.asyncio
async def test_get_run_results_uses_pending_reconciliation_run_when_id_omitted() -> None:
    ctx = _ctx(TurnIntent(mode=TurnIntentMode.UNKNOWN), pending_reconciliation_run_id="wr_pending")
    run = SimpleNamespace(workflow_run_id="wr_pending", workflow_permanent_id="wfp-1", status="failed")

    with patch("skyvern.forge.sdk.copilot.tools.run_execution.app") as mock_app:
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=run)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(return_value=[])
        mock_app.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker = AsyncMock(return_value=False)
        result = await _get_run_results({}, ctx)

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr_pending"
    mock_app.DATABASE.workflow_runs.get_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_pending",
        organization_id="org-1",
    )


@pytest.mark.asyncio
async def test_get_run_results_rejects_different_run_while_reconciliation_pending() -> None:
    ctx = _ctx(TurnIntent(mode=TurnIntentMode.UNKNOWN), pending_reconciliation_run_id="wr_pending")

    with patch("skyvern.forge.sdk.copilot.tools.run_execution.app") as mock_app:
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock()
        result = await _get_run_results({"workflow_run_id": "wr_other"}, ctx)

    assert result == {
        "ok": False,
        "error": "Run inspection is pending for wr_pending; call get_run_results with that workflow_run_id first.",
    }
    mock_app.DATABASE.workflow_runs.get_workflow_run.assert_not_called()


@pytest.mark.parametrize(
    ("policy", "expected_reason_code", "expected_recovery_hint", "expects_cleared_by_update"),
    [
        (
            RequestPolicy(allow_update_workflow=True, allow_run_blocks=False, testing_intent="skip_test"),
            "request_policy_blocks_run_blocks_skip_test",
            "retry_with_different_tool",
            True,
        ),
        (
            RequestPolicy(
                allow_update_workflow=True,
                allow_run_blocks=False,
                clarification_reason="workflow_credential_inputs_unbound",
            ),
            "request_policy_blocks_run_blocks_credential_unbound",
            "report_blocker_to_user",
            False,
        ),
        (
            RequestPolicy(allow_update_workflow=True, allow_run_blocks=False),
            "request_policy_blocks_run_blocks_generic",
            "ask_user_clarifying",
            False,
        ),
    ],
)
def test_request_policy_run_block_branches(
    policy: RequestPolicy,
    expected_reason_code: str,
    expected_recovery_hint: str,
    expects_cleared_by_update: bool,
) -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent, policy)
    payload = _authority_tool_error(ctx, "update_and_run_blocks")
    assert payload is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == expected_reason_code
    assert ctx.blocker_signal.recovery_hint == expected_recovery_hint
    if expects_cleared_by_update:
        assert "update_workflow" in ctx.blocker_signal.cleared_by_tools
    else:
        assert ctx.blocker_signal.cleared_by_tools == frozenset()
    for token in _LEAK_TOKENS:
        assert token not in payload


def test_cleared_by_tools_implies_retry_recovery_hint_convention() -> None:
    """Convention: a signal is sticky unless cleared_by_tools is non-empty,
    in which case the recovery_hint must be retry_with_different_tool."""
    intents = [
        TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        ),
        TurnIntent(
            mode=TurnIntentMode.DIAGNOSE,
            authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
        ),
    ]
    policies = [
        RequestPolicy(allow_update_workflow=True, allow_run_blocks=False, testing_intent="skip_test"),
        RequestPolicy(
            allow_update_workflow=True,
            allow_run_blocks=False,
            clarification_reason="workflow_credential_inputs_unbound",
        ),
        RequestPolicy(allow_update_workflow=False, allow_run_blocks=False),
    ]
    tool_names = ("update_workflow", "update_and_run_blocks", "run_blocks_and_collect_debug", "get_run_results")
    seen_any = False
    for intent in intents:
        for policy in policies:
            for tool in tool_names:
                ctx = _ctx(intent, policy)
                _authority_tool_error(ctx, tool)
                signal = ctx.blocker_signal
                if signal is None:
                    continue
                seen_any = True
                if signal.cleared_by_tools:
                    assert signal.recovery_hint == "retry_with_different_tool", (
                        f"signal with non-empty cleared_by_tools must use retry_with_different_tool, "
                        f"got {signal.recovery_hint!r} for {signal.internal_reason_code}"
                    )
    assert seen_any, "expected at least one signal in the convention sweep"
