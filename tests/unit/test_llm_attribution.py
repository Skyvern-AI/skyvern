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
