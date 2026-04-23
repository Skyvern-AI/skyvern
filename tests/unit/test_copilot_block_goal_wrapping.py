"""Tests for the copilot v2 block-goal wrap helper (SKY-9174, Part B).

Covers wrapping of ``navigation_goal``, ``complete_criterion``, and
``terminate_criterion`` via ``MINI_GOAL_TEMPLATE``.
"""

from __future__ import annotations

import textwrap

import yaml

from skyvern.constants import MINI_GOAL_TEMPLATE
from skyvern.forge.sdk.copilot.block_goal_wrapping import wrap_block_goals

USER_MESSAGE = "Submit a contact form on example.com with my details."


def _yaml_with_blocks(*blocks: dict) -> str:
    return yaml.safe_dump(
        {
            "title": "test workflow",
            "workflow_definition": {"blocks": list(blocks)},
        },
        sort_keys=False,
    )


def _blocks_from(yaml_str: str) -> list[dict]:
    return yaml.safe_load(yaml_str)["workflow_definition"]["blocks"]


def _wrapped(mini_goal: str) -> str:
    return MINI_GOAL_TEMPLATE.format(mini_goal=mini_goal, main_goal=USER_MESSAGE)


def test_wraps_task_block_navigation_goal() -> None:
    src = _yaml_with_blocks(
        {"block_type": "task", "label": "fill_form", "navigation_goal": "Fill in name and email, click Send"}
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    blocks = _blocks_from(out)
    assert blocks[0]["navigation_goal"] == _wrapped("Fill in name and email, click Send")


def test_wraps_navigation_action_login_and_file_download_block_types() -> None:
    src = _yaml_with_blocks(
        {"block_type": "navigation", "label": "a", "navigation_goal": "nav goal"},
        {"block_type": "action", "label": "b", "navigation_goal": "action goal"},
        {"block_type": "login", "label": "c", "navigation_goal": "login goal"},
        {"block_type": "file_download", "label": "d", "navigation_goal": "download goal"},
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    blocks = _blocks_from(out)
    assert blocks[0]["navigation_goal"] == _wrapped("nav goal")
    assert blocks[1]["navigation_goal"] == _wrapped("action goal")
    assert blocks[2]["navigation_goal"] == _wrapped("login goal")
    assert blocks[3]["navigation_goal"] == _wrapped("download goal")


def test_wraps_complete_criterion_on_validation_navigation_and_login_blocks() -> None:
    src = _yaml_with_blocks(
        {"block_type": "validation", "label": "v", "complete_criterion": "Your message has been sent"},
        {
            "block_type": "navigation",
            "label": "n",
            "navigation_goal": "submit form",
            "complete_criterion": "confirmation page visible",
        },
        {
            "block_type": "login",
            "label": "l",
            "navigation_goal": "login",
            "complete_criterion": "user dashboard visible",
        },
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    blocks = _blocks_from(out)
    assert blocks[0]["complete_criterion"] == _wrapped("Your message has been sent")
    assert blocks[1]["navigation_goal"] == _wrapped("submit form")
    assert blocks[1]["complete_criterion"] == _wrapped("confirmation page visible")
    assert blocks[2]["navigation_goal"] == _wrapped("login")
    assert blocks[2]["complete_criterion"] == _wrapped("user dashboard visible")


def test_wraps_terminate_criterion() -> None:
    src = _yaml_with_blocks(
        {
            "block_type": "validation",
            "label": "v",
            "complete_criterion": "order placed",
            "terminate_criterion": "payment failed",
        },
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    block = _blocks_from(out)[0]
    assert block["complete_criterion"] == _wrapped("order placed")
    assert block["terminate_criterion"] == _wrapped("payment failed")


def test_leaves_blocks_without_wrappable_fields_untouched() -> None:
    src = _yaml_with_blocks(
        {"block_type": "extraction", "label": "extract", "data_extraction_goal": "get title"},
        {"block_type": "goto_url", "label": "go", "url": "https://example.com"},
        {"block_type": "task", "label": "empty_goal", "navigation_goal": "", "complete_criterion": ""},
        {"block_type": "validation", "label": "null_crit", "complete_criterion": None},
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    blocks = _blocks_from(out)
    assert blocks[0] == {"block_type": "extraction", "label": "extract", "data_extraction_goal": "get title"}
    assert blocks[1] == {"block_type": "goto_url", "label": "go", "url": "https://example.com"}
    assert blocks[2]["navigation_goal"] == ""
    assert blocks[2]["complete_criterion"] == ""
    assert blocks[3]["complete_criterion"] is None


def test_idempotent_on_already_wrapped_fields() -> None:
    already_wrapped_goal = _wrapped("Fill in name and email, click Send")
    already_wrapped_criterion = _wrapped("Your message has been sent")
    src = _yaml_with_blocks(
        {
            "block_type": "task",
            "label": "fill_form",
            "navigation_goal": already_wrapped_goal,
            "complete_criterion": already_wrapped_criterion,
        }
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    block = _blocks_from(out)[0]
    assert block["navigation_goal"] == already_wrapped_goal
    assert block["complete_criterion"] == already_wrapped_criterion


def test_noop_on_empty_user_message() -> None:
    src = _yaml_with_blocks(
        {
            "block_type": "task",
            "label": "fill_form",
            "navigation_goal": "Fill the form",
            "complete_criterion": "Your message has been sent",
        }
    )

    out = wrap_block_goals(src, "")

    assert out == src


def test_noop_when_no_block_mutations_needed() -> None:
    src = _yaml_with_blocks(
        {"block_type": "extraction", "label": "extract", "data_extraction_goal": "get title"},
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    assert out == src


def test_recurses_into_for_loop_blocks() -> None:
    src = yaml.safe_dump(
        {
            "title": "loop workflow",
            "workflow_definition": {
                "blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "loop",
                        "loop_over": {"parameter_key": "items"},
                        "loop_blocks": [
                            {"block_type": "task", "label": "inner_task", "navigation_goal": "Process each item"},
                            {
                                "block_type": "validation",
                                "label": "inner_check",
                                "complete_criterion": "item processed",
                            },
                        ],
                    }
                ]
            },
        },
        sort_keys=False,
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    parsed = yaml.safe_load(out)
    loop_blocks = parsed["workflow_definition"]["blocks"][0]["loop_blocks"]
    assert loop_blocks[0]["navigation_goal"] == _wrapped("Process each item")
    assert loop_blocks[1]["complete_criterion"] == _wrapped("item processed")


def test_preserves_other_fields() -> None:
    src = _yaml_with_blocks(
        {
            "block_type": "task",
            "label": "fill_form",
            "url": "https://example.com",
            "title": "Fill form",
            "navigation_goal": "Fill in fields",
            "parameter_keys": ["name", "email"],
            "complete_criterion": "Form submitted",
            "max_retries": 2,
        }
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    block = _blocks_from(out)[0]
    assert block["url"] == "https://example.com"
    assert block["title"] == "Fill form"
    assert block["parameter_keys"] == ["name", "email"]
    assert block["max_retries"] == 2
    assert block["navigation_goal"] == _wrapped("Fill in fields")
    assert block["complete_criterion"] == _wrapped("Form submitted")


def test_returns_input_unchanged_on_malformed_yaml() -> None:
    malformed = textwrap.dedent(
        """
        title: bad
        workflow_definition:
          blocks:
            - block_type: task
              navigation_goal: "unclosed
        """
    ).strip()

    out = wrap_block_goals(malformed, USER_MESSAGE)

    assert out == malformed


def test_returns_input_unchanged_when_workflow_definition_missing() -> None:
    src = yaml.safe_dump({"title": "no definition"}, sort_keys=False)

    out = wrap_block_goals(src, USER_MESSAGE)

    assert out == src


def test_returns_input_unchanged_when_blocks_not_list() -> None:
    src = yaml.safe_dump(
        {"title": "bad blocks", "workflow_definition": {"blocks": "not a list"}},
        sort_keys=False,
    )

    out = wrap_block_goals(src, USER_MESSAGE)

    assert out == src
