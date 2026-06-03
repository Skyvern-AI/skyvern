"""Verify extract-action prompts contain captcha solve-timing guidance.

Captcha tokens expire quickly, so the prompt must instruct the LLM to
place SOLVE_CAPTCHA immediately before the protected submit — not before
a long form-fill sequence.
"""

from typing import Any

import pytest

from skyvern.forge.prompts import prompt_engine

_BASE_KWARGS: dict[str, Any] = {
    "navigation_goal": "fill out the privacy request form and submit",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.test/start",
    "current_url": "https://example.test/form",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2026-06-03T10:00:00Z",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "show_close_page_action": False,
    "open_tabs_context": None,
    "elements": "<html></html>",
    "recent_dialog_messages_str": None,
}


@pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
def test_prompt_contains_captcha_timing_guidance(template: str) -> None:
    rendered = prompt_engine.load_prompt(template, **_BASE_KWARGS)
    assert "expire" in rendered.lower(), "Prompt must mention token expiry"
    assert "submit" in rendered.lower() and "captcha" in rendered.lower(), (
        "Prompt must connect captcha timing to submit"
    )


@pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
def test_prompt_captcha_guidance_says_fill_first(template: str) -> None:
    rendered = prompt_engine.load_prompt(template, **_BASE_KWARGS).lower()
    assert "fill all form fields first" in rendered, "Prompt must instruct filling form fields first before captcha"
