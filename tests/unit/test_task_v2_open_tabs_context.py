"""The task_v2 planner and completion-gate prompts must surface the live open-tab set.

The planner and completion gate receive open_tabs_context (previously only per-step action
selection did), so a "keep exactly N tabs" goal is judged from the actual open-tab count rather
than the number the agent intended to open. These tests pin that the live tab list reaches both
prompts and that the curate-down guidance references it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.prompts import prompt_engine
from skyvern.webeye.utils.page import build_open_tabs_context

# More tabs (8) than the goal allows (5): the over-open the planner must notice and curate.
_EIGHT_TABS = "\n".join(
    f"Tab {i}{' [current]' if i == 0 else ''}: https://golf{i}.test/course (Course {i})" for i in range(8)
)
_GOAL = "Research reputable sources and keep exactly 5 golf course candidate tabs open."


class TestTaskV2PlannerOpenTabs:
    def _render(self, open_tabs_context: str | None) -> str:
        return prompt_engine.load_prompt(
            "task_v2",
            current_url="https://golf0.test/course",
            elements="<html></html>",
            user_goal=_GOAL,
            task_history="[]",
            open_tabs_context=open_tabs_context,
            local_datetime="2026-06-24T00:00:00Z",
        )

    def test_planner_sees_full_tab_list_when_present(self) -> None:
        rendered = self._render(_EIGHT_TABS)
        assert "Open browser tabs right now" in rendered
        assert "[current]" in rendered
        # Every actually-open tab is listed — including the ones beyond the goal's count of 5.
        assert "Tab 7" in rendered and "Course 7" in rendered
        # The planner is told to trust the live list, not its intended count.
        assert "true open-tab state" in rendered

    def test_planner_curate_rule_references_the_list(self) -> None:
        rendered = self._render(_EIGHT_TABS)
        assert "close the extra tabs down to exactly" in rendered
        assert 'live "Open browser tabs" list' in rendered

    def test_planner_block_absent_without_tab_context(self) -> None:
        rendered = self._render(None)
        assert "Open browser tabs right now" not in rendered
        # The standing count/selection rule is unconditional and must remain.
        assert "Honor explicit count" in rendered


class TestTaskV2CompletionGateOpenTabs:
    def _render(self, open_tabs_context: str | None) -> str:
        return prompt_engine.load_prompt(
            "task_v2_check_completion",
            user_goal=_GOAL,
            task_history="[]",
            open_tabs_context=open_tabs_context,
            local_datetime="2026-06-24T00:00:00Z",
        )

    def test_completion_gate_sees_full_tab_list_when_present(self) -> None:
        rendered = self._render(_EIGHT_TABS)
        assert "Open browser tabs right now" in rendered
        assert "Tab 7" in rendered and "Course 7" in rendered
        assert "compare that limit against the live" in rendered

    def test_completion_gate_block_absent_without_tab_context(self) -> None:
        rendered = self._render(None)
        assert "Open browser tabs right now" not in rendered
        # The standing keep-N-tabs rule is unconditional and must remain.
        assert "keep exactly N tabs open" in rendered


class TestPlannerCountIsActualNotIntended:
    """The helper reports the real number of open tabs — the signal the planner was missing."""

    @pytest.mark.asyncio
    async def test_eight_open_tabs_yield_eight_listed(self) -> None:
        pages = []
        for i in range(8):
            p = MagicMock()
            p.url = f"https://golf{i}.test/course"
            p.title = AsyncMock(return_value=f"Course {i}")
            pages.append(p)
        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=pages)

        result = await build_open_tabs_context(browser_state, pages[0])
        assert result is not None
        assert result.count("Tab ") == 8
        assert "Tab 7: https://golf7.test/course (Course 7)" in result
        assert "Tab 0 [current]:" in result
