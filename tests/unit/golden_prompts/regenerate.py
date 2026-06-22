"""Regenerate the golden control renders for the slim-output templates.

Run this ONLY when intentionally changing the control (flag-off) rendering of an
in-scope template, and review the golden diff like production code — it is the
byte-level contract that the SLIM_LLM_OUTPUT_PROMPTS control cohort is unchanged:

    uv run python tests/unit/golden_prompts/regenerate.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from skyvern.forge.sdk.prompting import PromptEngine

GOLDEN_DIR = Path(__file__).parent

EXTRACT_ACTION_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.com",
    "current_url": "https://example.com",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2025-01-01T00:00:00",
    "verification_code_check": True,
    "complete_criterion": None,
    "terminate_criterion": None,
    "show_close_page_action": False,
    "open_tabs_context": None,
    "recent_dialog_messages_str": None,
    "llm_screenshots_enabled": True,
    "enriched_tree_enabled": False,
    "elements": "<html></html>",
}
CHECK_USER_GOAL_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload": "{}",
    "complete_criterion": None,
    "action_history": "[]",
    "new_elements_ids": None,
    "without_screenshots": False,
    "local_datetime": "2025-01-01T00:00:00",
    "elements": "<html></html>",
}

# Must stay in sync with _TEMPLATE_CASES in tests/unit/test_slim_llm_output.py.
CASES: dict[str, dict[str, Any]] = {
    "extract-action": EXTRACT_ACTION_KWARGS,
    "extract-action-static": EXTRACT_ACTION_KWARGS,
    "check-user-goal": CHECK_USER_GOAL_KWARGS,
    "check-user-goal-with-termination": {**CHECK_USER_GOAL_KWARGS, "terminate_criterion": None},
    "auto-completion-choose-option": {
        "is_search": False,
        "field_information": "name",
        "filled_value": "John",
        "navigation_goal": "test goal",
        "navigation_payload_str": "{}",
        "elements": "<html></html>",
        "new_elements_ids": None,
        "local_datetime": "2025-01-01T00:00:00",
    },
    "parse-input-or-select-context": {
        "element_id": "elem_1",
        "action_reasoning": "test reasoning",
        "elements": "<html></html>",
    },
}


def main() -> None:
    engine = PromptEngine(model="skyvern")
    for name, kwargs in CASES.items():
        rendered = engine.load_prompt(name, **kwargs)
        path = GOLDEN_DIR / f"{name}.control.txt"
        path.write_text(rendered)
        print(f"wrote {path} ({len(rendered)} chars)")


if __name__ == "__main__":
    main()
