"""Render-level tests for the dialog-messages section of extract-action prompts."""

from typing import Any

import pytest

from skyvern.forge.prompts import prompt_engine

_BASE_KWARGS: dict[str, Any] = {
    "navigation_goal": "fill out the order form",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.test/start",
    "current_url": "https://example.test/form",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2026-05-04T17:14:00Z",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "has_magic_link_page": False,
    "elements": "<html></html>",
}


@pytest.mark.parametrize("template", ["extract-action", "extract-action-dynamic"])
def test_renders_dialog_section_when_messages_present(template: str) -> None:
    rendered = prompt_engine.load_prompt(
        template,
        recent_dialog_messages_str=(
            "[alert (x1031)] The value of '47' is invalid.  "
            "Only 9999999999, 999-999-9999 or (999) 999-9999 are allowed."
        ),
        **_BASE_KWARGS,
    )
    assert "Browser alert messages raised during the previous step" in rendered
    assert "is invalid" in rendered
    assert "999-999-9999" in rendered


@pytest.mark.parametrize("template", ["extract-action", "extract-action-dynamic"])
def test_omits_dialog_section_when_no_messages(template: str) -> None:
    rendered = prompt_engine.load_prompt(
        template,
        recent_dialog_messages_str=None,
        **_BASE_KWARGS,
    )
    assert "Browser alert messages raised during the previous step" not in rendered


def test_static_template_omits_dialog_block() -> None:
    """The dialog section must live in the dynamic template only; if it leaks into
    the static prefix the cached static contents would invalidate the Vertex cache
    every time a new alert fires."""
    rendered = prompt_engine.load_prompt(
        "extract-action-static",
        recent_dialog_messages_str="[alert] should not appear in static",
        **_BASE_KWARGS,
    )
    assert "Browser alert messages" not in rendered
    assert "should not appear in static" not in rendered
