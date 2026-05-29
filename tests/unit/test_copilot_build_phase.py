"""Unit tests for the copilot ``BuildPhase`` machinery."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.build_phase import (
    DISCOVERY_PERMITTED_PHASES,
    MUTATION_PERMITTED_PHASES,
    BuildPhase,
    _phase_tool_error,
    _yaml_has_target_url,
    advance_to_composing,
    advance_to_discovering,
    advance_to_testing,
    initial_build_phase,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode


class _Ctx:
    """Lightweight ctx stand-in — the helpers only read/write a small surface."""

    def __init__(self, phase: BuildPhase = BuildPhase.COMPOSING) -> None:
        self.build_phase = phase
        self.discovery_started_monotonic: float | None = None
        self.workflow_permanent_id = "wpid_test"


def _ti(mode: TurnIntentMode) -> TurnIntent:
    return TurnIntent(mode=mode)


# ---------------- initial_build_phase ----------------


@pytest.mark.parametrize(
    "mode,user_message,agent_message,workflow_yaml,expected",
    [
        # Explicitly-non-build modes return the COMPOSING sentinel.
        (TurnIntentMode.EDIT, "no url", "no url", "", BuildPhase.COMPOSING),
        (TurnIntentMode.DOCS_ANSWER, "what is a workflow?", "what is a workflow?", "", BuildPhase.COMPOSING),
        (TurnIntentMode.DIAGNOSE, "the run failed", "the run failed", "", BuildPhase.COMPOSING),
        (TurnIntentMode.CLARIFY, "anything", "anything", "", BuildPhase.COMPOSING),
        (TurnIntentMode.REFUSE, "anything", "anything", "", BuildPhase.COMPOSING),
        # BUILD with URL in latest user_message -> COMPOSING.
        (TurnIntentMode.BUILD, "go to https://example.com/login", "", "", BuildPhase.COMPOSING),
        # BUILD with URL only in rewritten agent message (request-policy continuation) -> COMPOSING.
        (TurnIntentMode.BUILD, "and download it", "earlier: go to https://example.com/file", "", BuildPhase.COMPOSING),
        # BUILD with no URL anywhere -> INITIAL.
        (TurnIntentMode.BUILD, "go to example", "go to example", "", BuildPhase.INITIAL),
        # DRAFT_ONLY behaves like BUILD for phase init.
        (
            TurnIntentMode.DRAFT_ONLY,
            "draft a workflow for example.com",
            "draft a workflow for example.com",
            "",
            BuildPhase.INITIAL,
        ),
        # DRAFT_ONLY with URL -> COMPOSING.
        (TurnIntentMode.DRAFT_ONLY, "draft a workflow for https://example.com", "", "", BuildPhase.COMPOSING),
        # UNKNOWN with no URL also enters INITIAL — a fresh "go to X" turn
        # with an empty-blocks scaffold workflow flips `has_workflow=True`
        # in the keyword classifier and suppresses NEW_BROWSER_TASK_TERMS,
        # producing UNKNOWN. Discovery is still the right next step there.
        (TurnIntentMode.UNKNOWN, "go to example and find sortable tables", "", "", BuildPhase.INITIAL),
        # UNKNOWN with a URL in the message -> COMPOSING (URL signal wins).
        (TurnIntentMode.UNKNOWN, "go to https://example.com/x", "", "", BuildPhase.COMPOSING),
    ],
)
def test_initial_build_phase_returns_expected(
    mode: TurnIntentMode,
    user_message: str,
    agent_message: str,
    workflow_yaml: str,
    expected: BuildPhase,
) -> None:
    assert initial_build_phase(_ti(mode), user_message, agent_message, workflow_yaml) == expected


def test_initial_build_phase_yaml_goto_url_unlocks_composing() -> None:
    yaml_text = """
title: T
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open
      url: https://example.com/page
"""
    assert (
        initial_build_phase(_ti(TurnIntentMode.BUILD), "do that thing", "do that thing", yaml_text)
        == BuildPhase.COMPOSING
    )


def test_initial_build_phase_yaml_with_empty_url_does_not_unlock() -> None:
    yaml_text = """
title: T
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open
      url: ""
"""
    assert initial_build_phase(_ti(TurnIntentMode.BUILD), "do thing", "do thing", yaml_text) == BuildPhase.INITIAL


def test_initial_build_phase_none_turn_intent_acts_like_unknown_mode() -> None:
    # turn_intent=None is treated as if mode is UNKNOWN — eligible for INITIAL
    # when no URL signal exists. The phase gate then blocks mutation until
    # discovery resolves an entrypoint or the model ASK_QUESTIONs.
    assert initial_build_phase(None, "go to example", "go to example", "") == BuildPhase.INITIAL
    # URL present -> COMPOSING regardless of None turn_intent.
    assert initial_build_phase(None, "go to https://example.com", "", "") == BuildPhase.COMPOSING


def test_initial_build_phase_yaml_malformed_falls_back_to_no_url() -> None:
    assert initial_build_phase(_ti(TurnIntentMode.BUILD), "open it", "open it", "[ unbalanced") == BuildPhase.INITIAL


# ---------------- _yaml_has_target_url ----------------


def test_yaml_has_target_url_detects_navigation_block_url() -> None:
    yaml_text = """
title: T
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: visit
      url: https://example.com/x
      navigation_goal: visit it
"""
    assert _yaml_has_target_url(yaml_text) is True


def test_yaml_has_target_url_ignores_non_navigation_blocks() -> None:
    yaml_text = """
title: T
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: c
      code: "x = 1"
"""
    assert _yaml_has_target_url(yaml_text) is False


def test_yaml_has_target_url_handles_empty_and_none() -> None:
    assert _yaml_has_target_url(None) is False
    assert _yaml_has_target_url("") is False


# ---------------- transition helpers ----------------


def test_advance_to_discovering_only_from_initial() -> None:
    ctx = _Ctx(BuildPhase.INITIAL)
    advance_to_discovering(ctx)
    assert ctx.build_phase == BuildPhase.DISCOVERING
    assert ctx.discovery_started_monotonic is not None


def test_advance_to_discovering_rejects_other_phases() -> None:
    for phase in (BuildPhase.DISCOVERING, BuildPhase.COMPOSING, BuildPhase.TESTING):
        ctx = _Ctx(phase)
        with pytest.raises(ValueError):
            advance_to_discovering(ctx)


def test_advance_to_composing_accepts_initial_and_discovering() -> None:
    for phase in (BuildPhase.INITIAL, BuildPhase.DISCOVERING):
        ctx = _Ctx(phase)
        advance_to_composing(ctx, reason="test")
        assert ctx.build_phase == BuildPhase.COMPOSING


def test_advance_to_composing_rejects_from_composing_or_testing() -> None:
    for phase in (BuildPhase.COMPOSING, BuildPhase.TESTING):
        ctx = _Ctx(phase)
        with pytest.raises(ValueError):
            advance_to_composing(ctx, reason="test")


def test_advance_to_testing_only_from_composing() -> None:
    ctx = _Ctx(BuildPhase.COMPOSING)
    advance_to_testing(ctx)
    assert ctx.build_phase == BuildPhase.TESTING


def test_advance_to_testing_is_noop_from_testing() -> None:
    ctx = _Ctx(BuildPhase.TESTING)
    advance_to_testing(ctx)
    assert ctx.build_phase == BuildPhase.TESTING


def test_advance_to_testing_rejects_pre_composition_phases() -> None:
    for phase in (BuildPhase.INITIAL, BuildPhase.DISCOVERING):
        ctx = _Ctx(phase)
        with pytest.raises(ValueError):
            advance_to_testing(ctx)


def test_phase_sets_are_disjoint() -> None:
    assert DISCOVERY_PERMITTED_PHASES.isdisjoint(MUTATION_PERMITTED_PHASES)


# ---------------- _phase_tool_error ----------------


@pytest.mark.parametrize(
    "tool_name,phase,should_block",
    [
        # Discovery tool: allowed in INITIAL/DISCOVERING, blocked in COMPOSING/TESTING.
        ("discover_workflow_entrypoint", BuildPhase.INITIAL, False),
        ("discover_workflow_entrypoint", BuildPhase.DISCOVERING, False),
        ("discover_workflow_entrypoint", BuildPhase.COMPOSING, True),
        ("discover_workflow_entrypoint", BuildPhase.TESTING, True),
        # Browser primitives: blocked in INITIAL/DISCOVERING, allowed in COMPOSING/TESTING.
        ("navigate_browser", BuildPhase.INITIAL, True),
        ("navigate_browser", BuildPhase.DISCOVERING, True),
        ("navigate_browser", BuildPhase.COMPOSING, False),
        ("evaluate", BuildPhase.INITIAL, True),
        ("type_text", BuildPhase.DISCOVERING, True),
        ("click", BuildPhase.COMPOSING, False),
        # Mutation tools: blocked in INITIAL/DISCOVERING, allowed in COMPOSING/TESTING.
        ("update_workflow", BuildPhase.INITIAL, True),
        ("update_workflow", BuildPhase.DISCOVERING, True),
        ("update_workflow", BuildPhase.COMPOSING, False),
        ("update_and_run_blocks", BuildPhase.INITIAL, True),
        ("run_blocks_and_collect_debug", BuildPhase.DISCOVERING, True),
        # Unknown tool name -> no error.
        ("list_credentials", BuildPhase.INITIAL, False),
        ("list_credentials", BuildPhase.COMPOSING, False),
    ],
)
def test_phase_tool_error_matrix(tool_name: str, phase: BuildPhase, should_block: bool) -> None:
    ctx = _Ctx(phase)
    error = _phase_tool_error(ctx, tool_name)
    if should_block:
        assert error is not None
        assert "safe_reason_code=" in error
    else:
        assert error is None


def test_phase_tool_error_returns_none_when_phase_attr_missing() -> None:
    class _NoPhase:
        pass

    assert _phase_tool_error(_NoPhase(), "navigate_browser") is None


@pytest.mark.parametrize(
    "tool_name,phase,expected_reason_code,expected_recovery_hint,cleared_by",
    [
        (
            "discover_workflow_entrypoint",
            BuildPhase.COMPOSING,
            "build_phase_discovery_disallowed_post_compose",
            "retry_with_different_tool",
            frozenset({"update_workflow", "update_and_run_blocks"}),
        ),
        (
            "navigate_browser",
            BuildPhase.INITIAL,
            "build_phase_browser_blocked_pre_compose",
            "ask_user_clarifying",
            frozenset({"discover_workflow_entrypoint", "update_workflow", "update_and_run_blocks"}),
        ),
        (
            "update_workflow",
            BuildPhase.INITIAL,
            "build_phase_mutation_blocked_pre_compose",
            "ask_user_clarifying",
            frozenset({"discover_workflow_entrypoint", "update_workflow", "update_and_run_blocks"}),
        ),
    ],
)
def test_phase_blocker_signal_returns_structured_signal(
    tool_name: str,
    phase: BuildPhase,
    expected_reason_code: str,
    expected_recovery_hint: str,
    cleared_by: frozenset[str],
) -> None:
    from skyvern.forge.sdk.copilot.blocker_signal import _LEAK_DENY_TOKENS
    from skyvern.forge.sdk.copilot.build_phase import _phase_blocker_signal

    ctx = _Ctx(phase)
    signal = _phase_blocker_signal(ctx, tool_name)
    assert signal is not None
    assert signal.blocker_kind == "phase_gated"
    assert signal.internal_reason_code == expected_reason_code
    assert signal.recovery_hint == expected_recovery_hint
    assert signal.cleared_by_tools == cleared_by
    assert signal.blocked_tool == tool_name
    for token in _LEAK_DENY_TOKENS:
        assert token.lower() not in signal.user_facing_reason.lower()


def test_phase_blocker_signal_returns_none_when_phase_attr_missing() -> None:
    from skyvern.forge.sdk.copilot.build_phase import _phase_blocker_signal

    class _NoPhase:
        pass

    assert _phase_blocker_signal(_NoPhase(), "navigate_browser") is None
