"""Behavioral tests for LLM attribution span attributes."""

from __future__ import annotations

import ast
import inspect
from unittest.mock import MagicMock

from skyvern.forge.sdk.api.llm.api_handler_factory import (
    _llm_screenshots_for_call,
    _set_llm_context_attrs,
)


def _make_span() -> MagicMock:
    return MagicMock()


def test_screenshots_included_true_when_present() -> None:
    span = _make_span()
    _set_llm_context_attrs(span, screenshots=[b"img1", b"img2"], is_speculative_step=False)
    span.set_attribute.assert_any_call("screenshots_included", True)
    span.set_attribute.assert_any_call("screenshot_count", 2)


def test_screenshots_included_false_when_none() -> None:
    span = _make_span()
    _set_llm_context_attrs(span, screenshots=None, is_speculative_step=False)
    span.set_attribute.assert_any_call("screenshots_included", False)
    span.set_attribute.assert_any_call("screenshot_count", 0)


def test_screenshots_included_false_when_empty() -> None:
    span = _make_span()
    _set_llm_context_attrs(span, screenshots=[], is_speculative_step=False)
    span.set_attribute.assert_any_call("screenshots_included", False)
    span.set_attribute.assert_any_call("screenshot_count", 0)


def test_speculative_true_when_step_is_speculative() -> None:
    span = _make_span()
    _set_llm_context_attrs(span, screenshots=None, is_speculative_step=True)
    span.set_attribute.assert_any_call("speculative", True)


def test_speculative_false_when_step_is_not_speculative() -> None:
    span = _make_span()
    _set_llm_context_attrs(span, screenshots=None, is_speculative_step=False)
    span.set_attribute.assert_any_call("speculative", False)


def test_set_llm_context_attrs_uses_only_explicit_args() -> None:
    # Invariant: every attribute must be derived from the explicit kwargs.
    # Reading skyvern_context or next_step_pre_scraped_data would tie the value
    # to a cache that is cleared mid-flight in some call paths.
    from skyvern.forge.sdk.api.llm import api_handler_factory as mod

    tree = ast.parse(inspect.getsource(mod._set_llm_context_attrs))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert "skyvern_context" not in names, "_set_llm_context_attrs must not reference skyvern_context"
    assert "next_step_pre_scraped_data" not in attrs, "_set_llm_context_attrs must not read next_step_pre_scraped_data"


def test_screenshot_attrs_reflect_post_filter_for_non_vision_config() -> None:
    # Invariant: span attrs must reflect what was actually sent to the LLM.
    # _llm_screenshots_for_call drops screenshots for non-vision configs, so
    # _set_llm_context_attrs must run on the filtered list, not the raw arg.
    span = _make_span()
    raw_screenshots = [b"img1", b"img2"]

    non_vision_config = MagicMock()
    non_vision_config.supports_vision = False

    filtered = _llm_screenshots_for_call(raw_screenshots, non_vision_config, None, "any-prompt", None)
    assert filtered is None, "Non-vision config must drop screenshots"

    _set_llm_context_attrs(span, screenshots=filtered, is_speculative_step=False)
    span.set_attribute.assert_any_call("screenshots_included", False)
    span.set_attribute.assert_any_call("screenshot_count", 0)


def test_set_llm_context_attrs_invoked_in_all_three_handlers() -> None:
    # Invariant: router, non-router, and LLMCaller handlers must each record
    # context attrs; a missing call site silently drops attribution.
    from skyvern.forge.sdk.api.llm import api_handler_factory as mod

    tree = ast.parse(inspect.getsource(mod))
    call_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        fname = (
            target.id if isinstance(target, ast.Name) else (target.attr if isinstance(target, ast.Attribute) else None)
        )
        if fname == "_set_llm_context_attrs":
            call_count += 1
    assert call_count == 3, f"Expected exactly 3 call sites, found {call_count}"


def _iter_function_defs(node: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(n)
    return funcs


def _call_lineno(func: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> int | None:
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            target = n.func
            fname = (
                target.id
                if isinstance(target, ast.Name)
                else (target.attr if isinstance(target, ast.Attribute) else None)
            )
            if fname == name:
                return n.lineno
    return None


def test_handlers_compute_screenshot_attrs_after_filter() -> None:
    # Invariant: in every handler that records screenshot attrs, the
    # _set_llm_context_attrs call must run AFTER _llm_screenshots_for_call so
    # the attrs reflect what was actually sent to the LLM, not the raw arg.
    from skyvern.forge.sdk.api.llm import api_handler_factory as mod

    tree = ast.parse(inspect.getsource(mod))
    matches = 0
    for func in _iter_function_defs(tree):
        attrs_line = _call_lineno(func, "_set_llm_context_attrs")
        filter_line = _call_lineno(func, "_llm_screenshots_for_call")
        if attrs_line is None or filter_line is None:
            continue
        assert attrs_line > filter_line, (
            f"In `{func.name}` (defined at line {func.lineno}): "
            f"_set_llm_context_attrs (line {attrs_line}) must come AFTER "
            f"_llm_screenshots_for_call (line {filter_line})."
        )
        matches += 1
    # ast.walk yields the 3 inner handler closures plus their two enclosing
    # factory functions, so the ordering invariant must hold for >=3 matches.
    assert matches >= 3, f"Expected at least 3 handlers with both calls, found {matches}"
