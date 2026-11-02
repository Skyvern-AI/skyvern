"""The task_v2 planner and completion-gate prompts must surface the live open-tab set."""

from typing import Any

import pytest

from skyvern.forge.prompts import prompt_engine

_EIGHT_TABS = "\n".join(
    f"Tab {i}{' [current]' if i == 0 else ''}: https://golf{i}.test/course (Course {i})" for i in range(8)
)
_GOAL = "Research reputable sources and keep exactly 5 golf course candidate tabs open."


def _render(template: str, variables: dict[str, Any], open_tabs_context: str | None) -> str:
    return prompt_engine.load_prompt(
        template,
        user_goal=_GOAL,
        task_history="[]",
        open_tabs_context=open_tabs_context,
        local_datetime="2026-06-24T00:00:00Z",
        **variables,
    )


@pytest.mark.parametrize(
    "template, variables",
    [
        pytest.param(
            "task_v2", {"current_url": "https://golf0.test/course", "elements": "<html></html>"}, id="planner"
        ),
        pytest.param("task_v2_check_completion", {}, id="completion-gate"),
    ],
)
def test_the_live_open_tab_list_reaches_the_prompt(template: str, variables: dict[str, Any]) -> None:
    assert "Tab 7: https://golf7.test/course (Course 7)" in _render(template, variables, _EIGHT_TABS)
    assert "golf7.test" not in _render(template, variables, None)
