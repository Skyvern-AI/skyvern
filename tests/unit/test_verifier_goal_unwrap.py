"""Tests for SKY-11295: MINI_GOAL_TEMPLATE unwrapping at completion verification.

`unwrap_goal_fields` reduces wrapped goal fields to their mini goals plus one
shared big-goal context; the check-user-goal templates render that context as
an explicit context-only section. The no-wrap render must stay byte-identical
to the golden controls (the hot path for unwrapped production traffic).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.block_goal_wrapping import compose_mini_goal, unwrap_goal_fields
from skyvern.forge.sdk.prompting import PromptEngine

GOLDEN_DIR = Path(__file__).parent / "golden_prompts"

MAIN_GOAL = "Open the example site, find the pricing page, and report the plan names"
MINI_GOAL = "Click the link that leads to the pricing page"
CRITERION_MINI = "The pricing page is visible with at least one plan listed"
TERMINATE_MINI = "The site shows a permanent maintenance page"

_CHECK_USER_GOAL_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload": "{}",
    "complete_criterion": None,
    "action_history": "[]",
    "new_elements_ids": None,
    "without_screenshots": False,
    "local_datetime": "2025-01-01T00:00:00",
    "elements": "<html></html>",
}
_CHECK_USER_GOAL_WITH_TERMINATION_KWARGS: dict[str, Any] = {
    **_CHECK_USER_GOAL_KWARGS,
    "terminate_criterion": None,
}
_TEMPLATE_KWARGS: dict[str, dict[str, Any]] = {
    "check-user-goal": _CHECK_USER_GOAL_KWARGS,
    "check-user-goal-with-termination": _CHECK_USER_GOAL_WITH_TERMINATION_KWARGS,
}


@pytest.fixture
def prompt_engine() -> PromptEngine:
    return PromptEngine(model="skyvern")


def test_unwrapped_fields_pass_through_untouched() -> None:
    result = unwrap_goal_fields("plain goal", None, "")
    assert result.navigation_goal == "plain goal"
    assert result.complete_criterion is None
    assert result.terminate_criterion == ""
    assert result.big_goal_context is None


def test_wrapped_navigation_goal_yields_mini_and_context() -> None:
    result = unwrap_goal_fields(compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL))
    assert result.navigation_goal == MINI_GOAL
    assert result.complete_criterion is None
    assert result.terminate_criterion is None
    assert result.big_goal_context == MAIN_GOAL


def test_all_three_wrapped_fields_share_one_context() -> None:
    result = unwrap_goal_fields(
        compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL),
        compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=CRITERION_MINI),
        compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=TERMINATE_MINI),
    )
    assert result.navigation_goal == MINI_GOAL
    assert result.complete_criterion == CRITERION_MINI
    assert result.terminate_criterion == TERMINATE_MINI
    assert result.big_goal_context == MAIN_GOAL


def test_mixed_wrap_takes_context_from_first_wrapped_field() -> None:
    result = unwrap_goal_fields(
        "plain goal",
        compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=CRITERION_MINI),
    )
    assert result.navigation_goal == "plain goal"
    assert result.complete_criterion == CRITERION_MINI
    assert result.big_goal_context == MAIN_GOAL


def test_spaced_fence_wrapped_goal_unwraps() -> None:
    spaced_fence_wrapped_goal = textwrap.dedent(
        f"""
        Achieve the following mini goal and once it's achieved, complete:
        ` ` `{MINI_GOAL}` ` `

        This mini goal is part of the big goal the user wants to achieve and use the big goal as context to achieve the mini goal:
        ` ` `{MAIN_GOAL}` ` `
        """
    ).strip()
    result = unwrap_goal_fields(spaced_fence_wrapped_goal)
    assert result.navigation_goal == MINI_GOAL
    assert result.big_goal_context == MAIN_GOAL


def test_multiline_goals_survive_unwrap() -> None:
    multiline_mini = "Fill the form:\n- name\n- email\nThen submit it"
    multiline_main = "Register a new account.\nThen verify the confirmation email arrived."
    result = unwrap_goal_fields(compose_mini_goal(main_goal=multiline_main, mini_goal=multiline_mini))
    assert result.navigation_goal == multiline_mini
    assert result.big_goal_context == multiline_main


def test_prose_mentioning_mini_goal_is_not_unwrapped() -> None:
    prose = "Achieve the following mini goal and once it's achieved, complete: submit the form"
    result = unwrap_goal_fields(prose)
    assert result.navigation_goal == prose
    assert result.big_goal_context is None


def test_masked_placeholder_survives_unwrap_verbatim() -> None:
    mini_with_secret = "Log in with username SECRET_username_1 and password SECRET_password_1"
    result = unwrap_goal_fields(compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=mini_with_secret))
    assert result.navigation_goal == mini_with_secret


@pytest.mark.parametrize("template_name", list(_TEMPLATE_KWARGS))
def test_render_without_context_is_byte_identical_to_golden(prompt_engine: PromptEngine, template_name: str) -> None:
    # Explicit big_goal_context=None must render exactly like the pre-SKY-11295
    # template (the unset-variable leg is covered by the existing golden test).
    golden = (GOLDEN_DIR / f"{template_name}.control.txt").read_text()
    assert prompt_engine.load_prompt(template_name, big_goal_context=None, **_TEMPLATE_KWARGS[template_name]) == golden


@pytest.mark.parametrize("template_name", list(_TEMPLATE_KWARGS))
def test_render_with_context_scopes_judgment_to_mini_goal(prompt_engine: PromptEngine, template_name: str) -> None:
    kwargs = {**_TEMPLATE_KWARGS[template_name], "navigation_goal": MINI_GOAL}
    rendered = prompt_engine.load_prompt(
        template_name, big_goal_context=MAIN_GOAL, action_history_evidence=True, **kwargs
    )
    assert MINI_GOAL in rendered
    assert MAIN_GOAL in rendered
    assert "The user goal above is one step of a larger objective" in rendered
    assert "Achieve the following mini goal" not in rendered
    # Compound heal goals: the evidence shortcut must demand every described action.
    assert "every described action (including each 'Then:' step)" in rendered
    assert "never after only the first of several actions" in rendered
    if template_name == "check-user-goal":
        assert "Do NOT require the larger objective to be complete" in rendered
    else:
        assert "NEVER a reason to continue or to terminate" in rendered


@pytest.mark.parametrize("template_name", list(_TEMPLATE_KWARGS))
def test_history_evidence_instruction_renders_without_context(prompt_engine: PromptEngine, template_name: str) -> None:
    # Bare-prompt heals: history evidence must not depend on the goal being wrapped.
    kwargs = {**_TEMPLATE_KWARGS[template_name], "navigation_goal": MINI_GOAL}
    rendered = prompt_engine.load_prompt(template_name, action_history_evidence=True, **kwargs)
    assert "every described action (including each 'Then:' step)" in rendered
    assert "The user goal above is one step of a larger objective" not in rendered
    without = prompt_engine.load_prompt(template_name, big_goal_context=MAIN_GOAL, **kwargs)
    assert "every described action" not in without


@pytest.mark.parametrize("template_name", list(_TEMPLATE_KWARGS))
def test_history_evidence_stays_subordinate_to_complete_criterion(
    prompt_engine: PromptEngine, template_name: str
) -> None:
    kwargs = {**_TEMPLATE_KWARGS[template_name], "navigation_goal": MINI_GOAL, "complete_criterion": CRITERION_MINI}
    rendered = prompt_engine.load_prompt(template_name, action_history_evidence=True, **kwargs)
    assert "The complete criterion still governs" in rendered
    without_criterion = {**_TEMPLATE_KWARGS[template_name], "navigation_goal": MINI_GOAL}
    rendered_no_criterion = prompt_engine.load_prompt(template_name, action_history_evidence=True, **without_criterion)
    assert "The complete criterion still governs" not in rendered_no_criterion


def test_render_with_context_and_complete_criterion_mentions_criterion(prompt_engine: PromptEngine) -> None:
    kwargs = {
        **_CHECK_USER_GOAL_WITH_TERMINATION_KWARGS,
        "navigation_goal": MINI_GOAL,
        "complete_criterion": CRITERION_MINI,
    }
    rendered = prompt_engine.load_prompt("check-user-goal-with-termination", big_goal_context=MAIN_GOAL, **kwargs)
    assert "as soon as that step is done according to the complete criterion" in rendered
