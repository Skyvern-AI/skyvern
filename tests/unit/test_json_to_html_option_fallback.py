"""Tests for json_to_html rendering value-only <select> options.

A native <option> carries {text, value, optionIndex}. When the text is blank or
whitespace-only, the option must render its value as the body so the LLM planner
can identify it (mirroring _collect_option_texts in handler.py). Otherwise the
option shows up unlabeled and the matcher cannot pick it.
"""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraped_page import json_to_html


@pytest.fixture(autouse=True)
def _scoped_context():
    """`json_to_html` calls `skyvern_context.ensure_context()`, so we need one."""
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        yield


def _select(options: list[dict]) -> str:
    return json_to_html({"isSelectable": True, "tagName": "select", "options": options}, need_skyvern_attrs=False)


def test_has_text_renders_text_as_label() -> None:
    html = _select([{"optionIndex": 0, "text": "United States", "value": "us"}])
    assert '<option index="0">United States</option>' in html


def test_empty_text_falls_back_to_value() -> None:
    html = _select([{"optionIndex": 1, "text": "", "value": "ca"}])
    assert '<option index="1" value="ca">ca</option>' in html


def test_whitespace_text_falls_back_to_value() -> None:
    html = _select([{"optionIndex": 2, "text": "   ", "value": "mx"}])
    assert '<option index="2" value="mx">mx</option>' in html


def test_no_option_is_rendered_with_empty_body() -> None:
    html = _select(
        [
            {"optionIndex": 0, "text": "United States", "value": "us"},
            {"optionIndex": 1, "text": "", "value": "ca"},
            {"optionIndex": 2, "text": "   ", "value": "mx"},
        ]
    )
    assert "></option>" not in html
