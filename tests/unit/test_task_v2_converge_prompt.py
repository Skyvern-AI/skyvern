from skyvern.config import Settings
from skyvern.forge.prompts import prompt_engine
from skyvern.services.task_v2_service import _converge_iterations_remaining


def test_converge_ships_disabled() -> None:
    assert Settings.model_fields["TASK_V2_CONVERGE_PCT"].default == 0


def _render(iterations_remaining: int | None) -> str:
    return prompt_engine.load_prompt(
        "task_v2",
        current_url="https://example.com",
        elements="",
        user_goal="do the thing",
        task_history=[],
        local_datetime="2026-01-01T00:00:00",
        prior_required_subgoals=None,
        iterations_remaining=iterations_remaining,
    )


def test_wrapup_rendered_when_iterations_remaining_set() -> None:
    rendered = _render(3)
    assert "WRAP-UP MODE" in rendered
    assert "3 planning iteration" in rendered


def test_wrapup_absent_when_none() -> None:
    assert "WRAP-UP MODE" not in _render(None)


def test_converge_disabled_when_pct_zero() -> None:
    assert _converge_iterations_remaining(0, 50, 0) is None
    assert _converge_iterations_remaining(49, 50, 0) is None


def test_converge_none_outside_window() -> None:
    # pct=20, max=50 -> window=10 -> fires only when remaining<=10 (i>=40)
    assert _converge_iterations_remaining(0, 50, 20) is None
    assert _converge_iterations_remaining(39, 50, 20) is None


def test_converge_fires_inside_window() -> None:
    assert _converge_iterations_remaining(40, 50, 20) == 10
    assert _converge_iterations_remaining(45, 50, 20) == 5


def test_converge_last_iteration_is_one_never_zero() -> None:
    assert _converge_iterations_remaining(49, 50, 20) == 1


def test_converge_window_floored_at_one() -> None:
    # tiny pct -> window floors at 1 -> only the final iteration fires
    assert _converge_iterations_remaining(49, 50, 1) == 1
    assert _converge_iterations_remaining(48, 50, 1) is None
