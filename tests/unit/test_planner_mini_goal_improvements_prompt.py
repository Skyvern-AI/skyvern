from skyvern.forge.prompts import prompt_engine

CRITERION_FIELD = '"complete_criterion"'


def _render_planner(planner_mini_goal_improvements: bool) -> str:
    return prompt_engine.load_prompt(
        "task_v2",
        current_url="https://example.com",
        elements="",
        user_goal="do the thing",
        task_history=[],
        local_datetime="2026-01-01T00:00:00",
        prior_required_subgoals=None,
        iterations_remaining=None,
        step_budget=10,
        planner_mini_goal_improvements=planner_mini_goal_improvements,
    )


def test_planner_offers_the_complete_criterion_field_only_when_mini_goal_improvements_are_enabled() -> None:
    assert CRITERION_FIELD in _render_planner(planner_mini_goal_improvements=True)
    assert CRITERION_FIELD not in _render_planner(planner_mini_goal_improvements=False)
