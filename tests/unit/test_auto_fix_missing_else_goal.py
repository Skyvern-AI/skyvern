"""Tests for ``_auto_fix_missing_else`` goal-required behavior."""

from __future__ import annotations

import pytest

from skyvern.services.script_reviewer import AutoFixMissingElseGoalError, ScriptReviewer

CLASSIFY_NO_ELSE_CODE = """
async def block_fn(page, context):
    state = await page.classify(options={"a": "page A"})
    if state == "a":
        await page.click(selector="#a-button")
"""


CLASSIFY_WITH_ELSE_CODE = """
async def block_fn(page, context):
    state = await page.classify(options={"a": "page A"})
    if state == "a":
        await page.click(selector="#a-button")
    else:
        await page.element_fallback(navigation_goal="Click the A button")
"""


NO_CLASSIFY_CODE = """
async def block_fn(page, context):
    await page.click(selector="#submit")
"""


class TestAutoFixMissingElseGoal:
    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_raises_on_missing_goal_when_else_needs_injection(self) -> None:
        with pytest.raises(AutoFixMissingElseGoalError):
            self.reviewer._auto_fix_missing_else(CLASSIFY_NO_ELSE_CODE, None)

    def test_raises_on_empty_string_goal(self) -> None:
        with pytest.raises(AutoFixMissingElseGoalError):
            self.reviewer._auto_fix_missing_else(CLASSIFY_NO_ELSE_CODE, "")

    def test_raises_on_whitespace_only_goal(self) -> None:
        with pytest.raises(AutoFixMissingElseGoalError):
            self.reviewer._auto_fix_missing_else(CLASSIFY_NO_ELSE_CODE, "   \n\t ")

    def test_no_classify_returns_none_without_raising(self) -> None:
        # No classify in the code → nothing to inject → None, no goal needed.
        assert self.reviewer._auto_fix_missing_else(NO_CLASSIFY_CODE, None) is None
        assert self.reviewer._auto_fix_missing_else(NO_CLASSIFY_CODE, "") is None

    def test_real_goal_threaded_into_injected_else(self) -> None:
        fixed = self.reviewer._auto_fix_missing_else(
            CLASSIFY_NO_ELSE_CODE,
            "Click the A button on the search results page",
        )
        assert fixed is not None
        assert "else:" in fixed
        assert "page.element_fallback" in fixed
        assert "Click the A button on the search results page" in fixed
        # And the placeholder string must NOT appear.
        assert "Complete the navigation task for this block" not in fixed

    def test_existing_else_branch_is_untouched(self) -> None:
        # Even with a missing goal — there's no injection needed, so no raise.
        result = self.reviewer._auto_fix_missing_else(CLASSIFY_WITH_ELSE_CODE, None)
        # _auto_fix_missing_else returns the (possibly-edited) code; if there
        # was nothing to fix, the code is returned unchanged. Either None or
        # the original-equivalent string is acceptable as long as no raise
        # occurs.
        if result is not None:
            assert "Click the A button" in result
