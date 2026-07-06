"""Tests for the shared script validators module and the generator's emission of selectorless actions."""

from unittest.mock import patch

import libcst as cst

from skyvern.core.script_generations import generate_script as generate_script_module
from skyvern.core.script_generations.generate_script import _action_to_stmt
from skyvern.core.script_generations.script_validators import (
    find_recoverable_proactive_candidates,
    validate_marker_kwarg_only_on_recoverable_proactive,
    validate_missing_selectors,
    validate_unmarked_proactive_unchanged,
)


def _render(stmt: cst.BaseStatement) -> str:
    """Render a libcst statement node to source code."""
    module = cst.Module(body=[stmt])
    return module.code


class TestValidateMissingSelectorsShared:
    def test_fallback_with_selector_is_fine(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
"""
        assert validate_missing_selectors(code) is None

    def test_fallback_without_selector_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt='Click Billing & Payments')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error
        assert "Missing selector" in error

    def test_proactive_without_selector_not_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Click something')
"""
        assert validate_missing_selectors(code) is None

    def test_no_ai_arg_without_selector_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click something')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "no ai= argument" in error

    def test_multiline_call_with_selector_ok(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(
        selector='a:has-text("Billing")',
        ai='fallback',
        prompt='Click billing link',
    )
"""
        assert validate_missing_selectors(code) is None

    def test_selector_inside_prompt_string_does_not_pass(self) -> None:
        """Regression: prompt text containing 'selector=' must not satisfy the validator (CORR-3)."""
        code = """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt='No selector= available for this widget')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error

    def test_ai_proactive_inside_prompt_string_still_flagged(self) -> None:
        """Regression: prompt text containing ai='proactive' must not falsely look like the proactive escape hatch (CORR-3)."""
        code = """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt="The original used ai='proactive'")
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error

    def test_proactive_without_selector_AND_prompt_is_flagged(self) -> None:
        """Regression: ai='proactive' without selector AND without prompt would crash at runtime
        (`Missing input: pass a selector and/or a prompt.`). Validator must catch it (CORR-3 from debate-2)."""
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error
        assert "no selector= AND no prompt=" in error

    def test_no_selector_no_prompt_no_ai_is_flagged(self) -> None:
        """All interaction methods missing selector AND prompt are flagged regardless of ai."""
        code = """
async def block_fn(page, context):
    await page.fill(value='x')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        assert "page.fill()" in error

    def test_comments_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    # await page.click(ai='fallback', prompt='old code')
    await page.click(selector='button', ai='fallback', prompt='submit')
"""
        assert validate_missing_selectors(code) is None

    def test_non_interaction_methods_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    await page.wait(ai='fallback', prompt='wait for page')
"""
        assert validate_missing_selectors(code) is None

    def test_multiple_methods_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.fill(ai='fallback', value='x', prompt='enter')
    await page.type(ai='fallback', value='y', prompt='type')
    await page.select_option(ai='fallback', value='z', prompt='select')
    await page.fill_autocomplete(ai='fallback', value='w', prompt='auto')
"""
        error = validate_missing_selectors(code)
        assert error is not None
        for method in ("fill", "type", "select_option", "fill_autocomplete"):
            assert f"page.{method}()" in error


class TestGeneratorDoesNotEmitSelectorlessFallback:
    """Verify the generator downgrades ai='fallback' to ai='proactive' when no semantic selector is available.

    This is the SKY-9436 fix: the runtime crashes with `Locator.fill: selector:
    expected string, got undefined` when an interaction call has ai='fallback' but
    no selector= argument. We test the regression by emitting an action where
    `_build_semantic_selector` returns None (no aria-label, placeholder, name, or
    text content) and confirming the generated code uses ai='proactive'.
    """

    @staticmethod
    def _action_with_no_semantic_signal() -> dict:
        """Build a CLICK action whose element has no aria-label/placeholder/name/text but has intention.

        Real recordings include an `intention` from the agent — without one the runtime
        would correctly raise `Missing input: pass a selector and/or a prompt.`
        """
        return {
            "action_type": "click",
            "xpath": "/html/body/div[3]/div[1]",
            "intention": "Click the help icon to expand the password requirements",
            "skyvern_element_data": {
                "tagName": "div",
                "text": "",
                "attributes": {},
            },
        }

    @staticmethod
    def _action_with_aria_label() -> dict:
        return {
            "action_type": "click",
            "xpath": "/html/body/button[1]",
            "skyvern_element_data": {
                "tagName": "button",
                "text": "Submit",
                "attributes": {"aria-label": "Submit form"},
            },
        }

    @staticmethod
    def _has_kwarg(rendered: str, key: str, val: str) -> bool:
        """libcst preserves emitter spacing — kwargs may render as `key=val` or `key = val`.

        Match either form by stripping whitespace around `=`.
        """
        normalized = rendered.replace(" = ", "=").replace(" =", "=").replace("= ", "=")
        return f"{key}='{val}'" in normalized or f'{key}="{val}"' in normalized

    def test_no_semantic_selector_downgrades_to_proactive(self) -> None:
        action = self._action_with_no_semantic_signal()
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = _render(stmt)
        assert self._has_kwarg(rendered, "ai", "proactive")
        assert not self._has_kwarg(rendered, "ai", "fallback")
        assert "selector=" not in rendered.replace(" ", "")
        assert validate_missing_selectors(rendered) is None

    def test_semantic_selector_keeps_fallback(self) -> None:
        action = self._action_with_aria_label()
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = _render(stmt)
        assert 'aria-label="Submit form"' in rendered
        assert self._has_kwarg(rendered, "ai", "fallback")
        assert validate_missing_selectors(rendered) is None

    def test_fill_no_semantic_selector_downgrades_to_proactive(self) -> None:
        action = {
            "action_type": "input_text",
            "xpath": "/html/body/div[3]/input[1]",
            "text": "hello",
            "intention": "Fill the captcha challenge box",
            "skyvern_element_data": {
                "tagName": "div",
                "text": "",
                "attributes": {},
            },
        }
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = _render(stmt)
        assert self._has_kwarg(rendered, "ai", "proactive")
        assert validate_missing_selectors(rendered) is None

    def test_proactive_escape_hatch_with_intention_emits_prompt(self) -> None:
        """When the action has an intention, the generator emits prompt= alongside ai='proactive'.

        For truly degenerate cases (no semantic signal AND no intention/reasoning),
        we deliberately let the runtime raise `Missing input: pass a selector
        and/or a prompt` rather than synthesizing a generic prompt that would
        give the AI a vague/unsafe target (RISK-1).
        """
        action = self._action_with_no_semantic_signal()
        action["intention"] = "Click the help icon next to the password field"
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = _render(stmt)
        assert "prompt=" in rendered.replace(" ", "")
        assert self._has_kwarg(rendered, "ai", "proactive")


class TestGeneratorEndOfGenerationHook:
    """Verifies the generator-side `validate_missing_selectors` safety net is wired in.

    Regression guard for COMP-2: extracting the end-of-generation hook into a
    helper (`_check_missing_selectors_and_warn`) lets us test the integration
    directly without setting up a full workflow.
    """

    def test_warn_on_selectorless_call(self) -> None:
        bad_code = "async def block_fn(page, context):\n    await page.click(ai='fallback', prompt='Click something')\n"
        with patch.object(generate_script_module, "LOG") as mock_log:
            warning = generate_script_module._check_missing_selectors_and_warn(
                bad_code,
                organization_id="o_test",
                workflow_permanent_id="wpid_test",
                workflow_run_id="wr_test",
            )
        assert warning is not None
        assert "page.click()" in warning
        mock_log.warning.assert_called_once()
        call_args = mock_log.warning.call_args
        assert call_args.args[0] == "script_generator_emitted_selectorless_action"
        assert call_args.kwargs["organization_id"] == "o_test"
        assert call_args.kwargs["workflow_permanent_id"] == "wpid_test"
        assert call_args.kwargs["workflow_run_id"] == "wr_test"

    def test_no_warning_on_clean_code(self) -> None:
        clean_code = (
            "async def block_fn(page, context):\n"
            "    await page.click(selector='button:has-text(\"Submit\")', ai='fallback', prompt='submit')\n"
        )
        with patch.object(generate_script_module, "LOG") as mock_log:
            warning = generate_script_module._check_missing_selectors_and_warn(
                clean_code,
                organization_id="o_test",
                workflow_permanent_id="wpid_test",
                workflow_run_id="wr_test",
            )
        assert warning is None
        mock_log.warning.assert_not_called()

    def test_validator_crash_is_caught_and_logged(self) -> None:
        """A regex crash inside the validator must not block codegen — log and continue."""
        with (
            patch.object(generate_script_module, "validate_missing_selectors", side_effect=ValueError("boom")),
            patch.object(generate_script_module, "LOG") as mock_log,
        ):
            warning = generate_script_module._check_missing_selectors_and_warn(
                "irrelevant", workflow_permanent_id=None, workflow_run_id=None
            )
        assert warning is None
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "script_generator_missing_selector_validator_failed_to_run"


class TestRecoverableProactive:
    """Marker-based recovery: opportunity detector + safety enforcer (SKY-9436)."""

    def test_marked_click_is_a_candidate(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Click help', recoverable_marker_id=42)
"""
        candidates = find_recoverable_proactive_candidates(code)
        assert len(candidates) == 1
        assert candidates[0].method == "click"
        assert candidates[0].marker_id == 42

    def test_unmarked_proactive_is_not_a_candidate(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick the most professional option')
"""
        assert find_recoverable_proactive_candidates(code) == []

    def test_marked_with_selector_is_not_a_candidate(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button', ai='proactive', prompt='click', recoverable_marker_id=1)
"""
        assert find_recoverable_proactive_candidates(code) == []

    def test_marked_fill_without_value_is_not_a_candidate(self) -> None:
        """Per Rule 8f restrictions: fill without value cannot be safely upgraded."""
        code = """
async def block_fn(page, context):
    await page.fill(ai='proactive', prompt='fill', recoverable_marker_id=1)
"""
        assert find_recoverable_proactive_candidates(code) == []

    def test_marked_fill_with_value_is_a_candidate(self) -> None:
        code = """
async def block_fn(page, context):
    await page.fill(value='hi', ai='proactive', prompt='fill', recoverable_marker_id=1)
"""
        candidates = find_recoverable_proactive_candidates(code)
        assert len(candidates) == 1
        assert candidates[0].method == "fill"

    def test_safety_validator_passes_when_unmarked_unchanged(self) -> None:
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best option')
"""
        after = before
        assert validate_unmarked_proactive_unchanged(before, after) is None

    def test_safety_validator_passes_when_marked_call_upgraded(self) -> None:
        """Upgrading a MARKED proactive call is allowed; the unmarked sibling stays."""
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best option')
    await page.click(ai='proactive', prompt='Click help', recoverable_marker_id=42)
"""
        after = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best option')
    await page.click(selector='button[aria-label="Help"]', ai='fallback', prompt='Click help')
"""
        assert validate_unmarked_proactive_unchanged(before, after) is None

    def test_safety_validator_blocks_when_unmarked_proactive_was_mutated(self) -> None:
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best option')
"""
        after = """
async def block_fn(page, context):
    await page.click(selector='button.primary', ai='fallback', prompt='Pick best option')
"""
        error = validate_unmarked_proactive_unchanged(before, after)
        assert error is not None
        assert "page.click()" in error
        assert "intentional" in error.lower()

    def test_safety_validator_detects_prompt_text_change(self) -> None:
        """Regression: kwarg-name-only matching missed semantic edits to prompt text (CORR-2)."""
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best option')
"""
        after = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Click the submit button')
"""
        error = validate_unmarked_proactive_unchanged(before, after)
        assert error is not None
        assert "page.click()" in error

    def test_safety_validator_detects_removal_among_duplicates(self) -> None:
        """Regression: set-based matching lost multiplicity (CORR-3)."""
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best')
    await page.click(ai='proactive', prompt='Pick best')
    await page.click(ai='proactive', prompt='Pick best')
"""
        after = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick best')
    await page.click(ai='proactive', prompt='Pick best')
"""
        error = validate_unmarked_proactive_unchanged(before, after)
        assert error is not None
        assert "page.click()" in error


class TestGeneratorEmitsRecoverableMarker:
    """The generator emits `recoverable_marker_id` only when no semantic selector is buildable (SKY-9436)."""

    def test_marker_emitted_when_no_semantic_selector(self) -> None:
        action = {
            "action_type": "click",
            "xpath": "/html/body/div[3]/div[1]",
            "intention": "Click help icon",
            "skyvern_element_data": {"tagName": "div", "text": "", "attributes": {}},
        }
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = cst.Module(body=[stmt]).code
        normalized = rendered.replace(" ", "")
        assert "recoverable_marker_id=" in normalized

    def test_marker_not_emitted_when_semantic_selector_available(self) -> None:
        action = {
            "action_type": "click",
            "xpath": "/html/body/button[1]",
            "intention": "Click submit",
            "skyvern_element_data": {"tagName": "button", "text": "Submit", "attributes": {"aria-label": "Submit"}},
        }
        stmt = _action_to_stmt(action, task={}, use_semantic_selectors=True)
        rendered = cst.Module(body=[stmt]).code
        assert "recoverable_marker_id" not in rendered

    def test_marker_stable_across_invocations(self) -> None:
        """Same action data → same marker_id (matches across recording → reviewer)."""
        action = {
            "action_type": "click",
            "xpath": "/html/body/div[3]/div[1]",
            "intention": "Click help",
            "skyvern_element_data": {"tagName": "div", "text": "", "attributes": {}},
        }
        a = cst.Module(body=[_action_to_stmt(action, task={}, use_semantic_selectors=True)]).code
        b = cst.Module(body=[_action_to_stmt(action, task={}, use_semantic_selectors=True)]).code
        assert a == b


class TestMarkerKwargPosition:
    """validate_marker_kwarg_only_on_recoverable_proactive — Rule 8f cleanup enforcement."""

    def test_marker_on_proactive_no_selector_is_ok(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='click', recoverable_marker_id=42)
"""
        assert validate_marker_kwarg_only_on_recoverable_proactive(code) is None

    def test_marker_on_fallback_with_selector_is_flagged(self) -> None:
        """Reviewer upgraded marker→selector but forgot to remove the kwarg."""
        code = """
async def block_fn(page, context):
    await page.click(selector='button', ai='fallback', prompt='click', recoverable_marker_id=42)
"""
        error = validate_marker_kwarg_only_on_recoverable_proactive(code)
        assert error is not None
        assert "page.click()" in error

    def test_marker_on_proactive_with_selector_is_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button', ai='proactive', prompt='click', recoverable_marker_id=42)
"""
        assert validate_marker_kwarg_only_on_recoverable_proactive(code) is not None


class TestSemanticKwargComparison:
    """Safety validator catches edits to ANY kwarg, not just prompt/value/intention (CORR-2 round 2)."""

    def test_safety_validator_detects_timeout_change(self) -> None:
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick', timeout=5000)
"""
        after = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick', timeout=30000)
"""
        error = validate_unmarked_proactive_unchanged(before, after)
        assert error is not None

    def test_safety_validator_detects_data_change(self) -> None:
        before = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick', data='abc')
"""
        after = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Pick', data='xyz')
"""
        error = validate_unmarked_proactive_unchanged(before, after)
        assert error is not None
