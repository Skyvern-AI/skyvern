"""Tests for SKY-11295: MINI_GOAL_TEMPLATE unwrapping at completion verification.

`unwrap_goal_fields` reduces wrapped goal fields to their mini goals plus one
shared big-goal context; the check-user-goal templates render that context as
an explicit context-only section.
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.block_goal_wrapping import compose_mini_goal, unwrap_goal_fields
from skyvern.forge.sdk.prompting import PromptEngine

MAIN_GOAL = "Open the example site, find the pricing page, and report the plan names"
MINI_GOAL = "Click the link that leads to the pricing page"
CRITERION_MINI = "The pricing page is visible with at least one plan listed"
TERMINATE_MINI = "The site shows a permanent maintenance page"


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


_CHECK_USER_GOAL_KWARGS: dict[str, Any] = {
    "navigation_goal": MINI_GOAL,
    "navigation_payload": "{}",
    "complete_criterion": None,
    "action_history": "[]",
    "new_elements_ids": None,
    "without_screenshots": False,
    "local_datetime": "2025-01-01T00:00:00",
    "elements": "<html></html>",
}
_CONTEXT_RULE = {
    "check-user-goal": "Do NOT require the larger objective to be complete",
    "check-user-goal-with-termination": "NEVER a reason to continue or to terminate",
}


@pytest.mark.parametrize("template_name", list(_CONTEXT_RULE))
def test_render_with_context_scopes_judgment_to_mini_goal(template_name: str) -> None:
    engine = PromptEngine(model="skyvern")
    kwargs = {**_CHECK_USER_GOAL_KWARGS, "terminate_criterion": None}

    rendered = engine.load_prompt(template_name, big_goal_context=MAIN_GOAL, **kwargs)
    assert MINI_GOAL in rendered
    assert MAIN_GOAL in rendered
    assert _CONTEXT_RULE[template_name] in rendered
    assert MAIN_GOAL not in engine.load_prompt(template_name, **kwargs)
