import os

from jinja2 import Environment, FileSystemLoader

from skyvern.forge.prompts import prompt_engine

UMBRELLA_MARK = "How to write each mini goal"
STEP_BUDGET_MARK = "Step budget per mini goal"
CRITERION_FIELD = '"complete_criterion"'


def _render(
    planner_mini_goal_improvements: bool = False,
    step_budget: int = 10,
) -> str:
    return prompt_engine.load_prompt(
        "task_v2",
        current_url="https://example.com",
        elements="",
        user_goal="do the thing",
        task_history=[],
        local_datetime="2026-01-01T00:00:00",
        prior_required_subgoals=None,
        iterations_remaining=None,
        step_budget=step_budget,
        planner_mini_goal_improvements=planner_mini_goal_improvements,
    )


def test_umbrella_block_rendered_when_enabled() -> None:
    rendered = _render(planner_mini_goal_improvements=True, step_budget=10)
    assert UMBRELLA_MARK in rendered
    assert "at most 10 steps" in rendered
    # specificity: the direct-action example (type the date vs operate the calendar) is present
    assert "type `2026-12-02`" in rendered
    # lever B: the completion-criterion field is offered in the planner output schema
    assert CRITERION_FIELD in rendered


def test_step_budget_number_interpolated_in_umbrella() -> None:
    assert "at most 7 steps" in _render(planner_mini_goal_improvements=True, step_budget=7)


def test_nothing_rendered_when_umbrella_disabled() -> None:
    rendered = _render(planner_mini_goal_improvements=False)
    assert UMBRELLA_MARK not in rendered
    assert STEP_BUDGET_MARK not in rendered
    assert CRITERION_FIELD not in rendered


def test_umbrella_replan_guidance_is_specificity_oriented() -> None:
    rendered = _render(planner_mini_goal_improvements=True)
    assert "MORE SPECIFIC variant" in rendered


LOOP_SCOPE_MARK = "This goal is run once per"


def _render_loop_block(planner_mini_goal_improvements: bool = False, is_link: bool = False) -> str:
    return prompt_engine.load_prompt(
        "task_v2_generate_task_block",
        plan="For each of the top schools, find its admissions page and application fee.",
        local_datetime="2026-01-01T00:00:00",
        is_link=is_link,
        loop_values=["Stanford University", "Yale University"],
        planner_mini_goal_improvements=planner_mini_goal_improvements,
    )


def test_loop_block_scoping_guidance_gated_on_umbrella() -> None:
    # without the umbrella the inner-task prompt is unchanged (no per-iteration scoping guidance)
    assert LOOP_SCOPE_MARK not in _render_loop_block(planner_mini_goal_improvements=False)
    on = _render_loop_block(planner_mini_goal_improvements=True)
    assert LOOP_SCOPE_MARK in on
    # a non-link loop must scope to the current item via the placeholder, never enumerate the list
    assert "{{ current_value }}" in on


def test_loop_block_link_variant_omits_current_value_placeholder() -> None:
    # link loops already open the current link as the page, so no current_value placeholder is needed
    on_link = _render_loop_block(planner_mini_goal_improvements=True, is_link=True)
    assert LOOP_SCOPE_MARK in on_link
    assert "{{ current_value }}" not in on_link


def _render_extract_action_static(planner_mini_goal_improvements: bool) -> str:
    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "skyvern", "forge", "prompts", "skyvern")
    env = Environment(loader=FileSystemLoader(prompts_dir))
    ctx = dict(
        enable_new_planner_actions=False,
        data_extraction_goal=None,
        complete_criterion="",
        show_new_tab_action=False,
        show_switch_tab_action=False,
        show_close_page_action=False,
        navigation_goal="g",
        elements="e",
        action_history="",
        local_datetime="now",
        utc_datetime="now",
        error_code_mapping_str=None,
        data_extraction_schema=None,
    )
    return env.get_template("extract-action-static.j2").render(
        planner_mini_goal_improvements=planner_mini_goal_improvements, **ctx
    )
