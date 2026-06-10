from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import (
    _LEAK_DENY_TOKENS,
    BlockerKind,
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
    build_llm_tool_error_payload,
    clear_blocker_signal_for_reason_codes,
    maybe_clear_blocker_signal_on_tool_success,
    stash_blocker_signal,
    to_trace_data,
)


def _make(
    *,
    kind: BlockerKind = "authority_denied",
    cleared_by_tools: frozenset[str] = frozenset(),
    internal_reason_code: str = "some_reason",
    blocked_tool: str = "update_workflow",
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=kind,
        agent_steering_text="Take this recovery step.",
        user_facing_reason="I couldn't do that on this turn.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=cleared_by_tools,
        internal_reason_code=internal_reason_code,
        blocked_tool=blocked_tool,
        classifier_mode="docs_answer",
    )


def test_model_round_trips_through_validate() -> None:
    signal = _make()
    restored = CopilotToolBlockerSignal.model_validate(signal.model_dump())
    assert restored == signal


def test_build_llm_payload_is_agent_steering_text_only() -> None:
    signal = _make()
    payload = build_llm_tool_error_payload(signal)
    assert payload == signal.agent_steering_text
    assert "recovery_hint" not in payload
    assert signal.internal_reason_code is not None and signal.internal_reason_code not in payload
    assert "docs_answer" not in payload


def test_to_trace_data_surfaces_internal_fields() -> None:
    signal = _make(internal_reason_code="r1", cleared_by_tools=frozenset({"a", "b"}))
    trace = to_trace_data(signal)
    assert trace["internal_reason_code"] == "r1"
    assert trace["blocker_kind"] == "authority_denied"
    assert trace["classifier_mode"] == "docs_answer"
    assert trace["cleared_by_tools"] == ["a", "b"]
    assert trace["renders_final_reply"] is True
    assert trace["extra"] == {}


def test_to_trace_data_namespaces_extra_so_it_cannot_shadow_explicit_fields() -> None:
    signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="x",
        user_facing_reason="y",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="r1",
        # A producer that stuffs ``blocker_kind`` into ``extra`` must not
        # silently shadow the canonical top-level field.
        extra={"blocker_kind": "evil", "custom_metric": 7},
    )
    trace = to_trace_data(signal)
    assert trace["blocker_kind"] == "authority_denied"
    assert trace["extra"] == {"blocker_kind": "evil", "custom_metric": 7}


@pytest.mark.parametrize("token", _LEAK_DENY_TOKENS)
def test_assert_clean_raises_on_each_deny_token(token: str) -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text(f"prefix {token} suffix")


def test_assert_clean_raises_on_blocked_tool_substring() -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("calling get_run_results was wrong", blocked_tool="get_run_results")


def test_assert_clean_passes_normal_product_language() -> None:
    assert_clean_user_facing_text("I couldn't complete that on this turn.")


def test_assert_clean_raises_on_internal_budget_vocabulary() -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("The run exceeded the 6s per-tool-call budget while still making progress.")


def test_assert_clean_raises_on_raw_run_id() -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("Run ID: wr_538438176486379954. Outcome is uncertain.")


def test_assert_clean_allows_prose_mentioning_runs_without_ids() -> None:
    assert_clean_user_facing_text("The last run didn't finish; I stopped without claiming results.")


class _Ctx:
    blocker_signal: CopilotToolBlockerSignal | None = None
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None = None
    tool_blocker_signals: list[CopilotToolBlockerSignal]

    def __init__(self) -> None:
        self.tool_blocker_signals = []


def test_maybe_clear_on_tool_success_clears_when_in_cleared_by_tools() -> None:
    ctx = _Ctx()
    ctx.blocker_signal = _make(cleared_by_tools=frozenset({"update_workflow"}))
    maybe_clear_blocker_signal_on_tool_success(ctx, "update_workflow")
    assert ctx.blocker_signal is None


def test_maybe_clear_on_tool_success_no_match_keeps_signal() -> None:
    ctx = _Ctx()
    signal = _make(cleared_by_tools=frozenset({"update_workflow"}))
    ctx.blocker_signal = signal
    maybe_clear_blocker_signal_on_tool_success(ctx, "list_credentials")
    assert ctx.blocker_signal is signal


def test_maybe_clear_on_tool_success_empty_set_keeps_signal() -> None:
    ctx = _Ctx()
    signal = _make()
    ctx.blocker_signal = signal
    maybe_clear_blocker_signal_on_tool_success(ctx, "update_workflow")
    assert ctx.blocker_signal is signal


def test_maybe_clear_on_tool_success_clears_consecutive_tool_loop_after_progress() -> None:
    ctx = _Ctx()
    ctx.blocker_signal = _make(kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool")
    maybe_clear_blocker_signal_on_tool_success(ctx, "get_browser_screenshot")
    assert ctx.blocker_signal is None


def test_maybe_clear_on_tool_success_clears_loop_after_workflow_progress() -> None:
    ctx = _Ctx()
    ctx.blocker_signal = _make(kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")
    maybe_clear_blocker_signal_on_tool_success(ctx, "update_and_run_blocks")
    assert ctx.blocker_signal is None


def test_maybe_clear_on_tool_success_keeps_loop_for_metadata_only_success() -> None:
    ctx = _Ctx()
    signal = _make(kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")
    ctx.blocker_signal = signal
    maybe_clear_blocker_signal_on_tool_success(ctx, "list_credentials")
    assert ctx.blocker_signal is signal


def test_clear_for_reason_codes_matches() -> None:
    ctx = _Ctx()
    ctx.blocker_signal = _make(internal_reason_code="tool_error_pending_reconciliation_no_input")
    clear_blocker_signal_for_reason_codes(ctx, frozenset({"tool_error_pending_reconciliation_no_input"}))
    assert ctx.blocker_signal is None


def test_clear_for_reason_codes_no_match() -> None:
    ctx = _Ctx()
    signal = _make(internal_reason_code="loop_detected_generic")
    ctx.blocker_signal = signal
    clear_blocker_signal_for_reason_codes(ctx, frozenset({"tool_error_pending_reconciliation_no_input"}))
    assert ctx.blocker_signal is signal


def test_clear_helpers_ignore_non_signal_values_on_ctx() -> None:
    """Defensive: structurally satisfying the Protocol with a non-signal value
    (e.g. a stray attribute set by another subsystem) must not be cleared."""
    ctx = _Ctx()
    ctx.blocker_signal = "not a signal"  # type: ignore[assignment]
    maybe_clear_blocker_signal_on_tool_success(ctx, "update_workflow")
    assert ctx.blocker_signal == "not a signal"
    clear_blocker_signal_for_reason_codes(ctx, frozenset({"some_reason"}))
    assert ctx.blocker_signal == "not a signal"


def test_deny_list_is_case_insensitive() -> None:
    """A future template that drops or flips casing on a leak phrase must
    still be caught — agent prompts have used both ``Do NOT`` and ``do not``."""
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("Do not run that step again")
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("DO NOT RUN that step again")
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("safe_REASON_code=foo")


def test_deny_list_narrow_imperatives_do_not_false_positive() -> None:
    """Plain ``do not`` followed by non-imperative copy is legitimate
    product language and must not trip the guard."""
    assert_clean_user_facing_text("I'm sorry, do not worry — I'll try again.")
    assert_clean_user_facing_text("Please do not hesitate to share more context.")


def test_model_validator_rejects_leaky_user_facing_at_construction() -> None:
    with pytest.raises(ValueError):
        CopilotToolBlockerSignal(
            blocker_kind="authority_denied",
            agent_steering_text="agent steering — anything goes here",
            user_facing_reason="DO NOT RUN this — talk to user first",
            recovery_hint="report_blocker_to_user",
        )


def test_model_validator_rejects_blocked_tool_name_in_user_facing() -> None:
    with pytest.raises(ValueError):
        CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="x",
            user_facing_reason="couldn't call get_run_results on this turn",
            recovery_hint="report_blocker_to_user",
            blocked_tool="get_run_results",
        )


def test_model_validator_blocked_tool_check_is_case_insensitive() -> None:
    with pytest.raises(ValueError):
        CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="x",
            user_facing_reason="I had to call Update_Workflow",
            recovery_hint="report_blocker_to_user",
            blocked_tool="update_workflow",
        )


def test_extra_is_mapping_proxy_after_construction() -> None:
    from types import MappingProxyType

    signal = _make()
    assert isinstance(signal.extra, MappingProxyType)
    with pytest.raises(TypeError):
        signal.extra["k"] = "v"  # type: ignore[index]


def test_extra_default_does_not_alias_across_instances() -> None:
    a = _make()
    b = _make()
    assert a.extra is not b.extra


def test_stash_blocker_signal_first_wins_returns_llm_payload() -> None:
    ctx = _Ctx()
    first = _make(internal_reason_code="first")
    payload = stash_blocker_signal(ctx, first)
    assert payload == first.agent_steering_text
    assert ctx.blocker_signal is first
    assert ctx.latest_tool_blocker_signal is first
    assert ctx.tool_blocker_signals == [first]

    second = _make(internal_reason_code="second")
    payload2 = stash_blocker_signal(ctx, second)
    assert payload2 == second.agent_steering_text  # LLM payload is the current signal's
    assert ctx.blocker_signal is first  # stash is sticky
    assert ctx.latest_tool_blocker_signal is second
    assert ctx.tool_blocker_signals == [first, second]


def test_stash_blocker_signal_active_terminal_replaces_per_tool_budget() -> None:
    ctx = _Ctx()
    budget = _make(internal_reason_code="tool_error_per_tool_budget_rerun")
    active_terminal = _make(internal_reason_code="tool_error_active_run_terminal_evidence")

    stash_blocker_signal(ctx, budget)
    payload = stash_blocker_signal(ctx, active_terminal)

    assert payload == active_terminal.agent_steering_text
    assert ctx.blocker_signal is active_terminal
    assert ctx.latest_tool_blocker_signal is active_terminal
    assert ctx.tool_blocker_signals == [budget, active_terminal]


def test_agent_context_and_copilot_context_blocker_signal_defaults_match() -> None:
    """The field is declared on both AgentContext (parent) and CopilotContext
    (child) per the field-shadowing convention. Default values must stay in
    sync so callers reading via the AgentContext annotation see the same
    initial state as callers reading via CopilotContext."""
    from types import SimpleNamespace

    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.copilot.runtime import AgentContext

    agent_ctx = AgentContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    copilot_ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert agent_ctx.blocker_signal is None
    assert copilot_ctx.blocker_signal is None
    assert agent_ctx.blocker_signal == copilot_ctx.blocker_signal
    assert agent_ctx.latest_tool_blocker_signal is None
    assert copilot_ctx.latest_tool_blocker_signal is None
    assert agent_ctx.latest_tool_blocker_signal == copilot_ctx.latest_tool_blocker_signal
    assert agent_ctx.tool_blocker_signals == []
    assert copilot_ctx.tool_blocker_signals == []
