"""Tests for ``skyvern.forge.sdk.copilot.workflow_change_summary``."""

from __future__ import annotations

import copy
from typing import Any

import yaml

from skyvern.forge.sdk.copilot.workflow_change_summary import (
    WorkflowChangeKind,
    summarize_user_workflow_change,
)


def _baseline_dict() -> dict[str, Any]:
    return {
        "title": "t",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "applicant_name",
                    "workflow_parameter_type": "string",
                }
            ],
            "blocks": [
                {"block_type": "goto_url", "label": "open_site", "url": "https://example.com"},
                {
                    "block_type": "navigation",
                    "label": "fill_form",
                    "navigation_goal": "Fill out the form with the applicant's name.",
                },
            ],
        },
    }


def _dump(workflow: dict[str, Any]) -> str:
    return yaml.safe_dump(workflow, sort_keys=False)


def _with_blocks(extra_blocks: list[dict[str, Any]]) -> dict[str, Any]:
    workflow = _baseline_dict()
    workflow["workflow_definition"]["blocks"].extend(extra_blocks)
    return workflow


def test_first_turn_when_prior_yaml_missing() -> None:
    summary = summarize_user_workflow_change(prior_yaml=None, current_yaml=_dump(_baseline_dict()))
    assert summary.kind is WorkflowChangeKind.FIRST_TURN_NO_PRIOR_STATE


def test_first_turn_when_prior_yaml_blank() -> None:
    summary = summarize_user_workflow_change(prior_yaml="   \n", current_yaml=_dump(_baseline_dict()))
    assert summary.kind is WorkflowChangeKind.FIRST_TURN_NO_PRIOR_STATE


def test_unchanged_when_strings_match() -> None:
    baseline = _dump(_baseline_dict())
    summary = summarize_user_workflow_change(prior_yaml=baseline, current_yaml=baseline)
    assert summary.kind is WorkflowChangeKind.UNCHANGED_SINCE_LAST_TURN


def test_unchanged_when_only_whitespace_differs() -> None:
    baseline = _dump(_baseline_dict())
    summary = summarize_user_workflow_change(prior_yaml=baseline, current_yaml=baseline + "\n\n")
    assert summary.kind is WorkflowChangeKind.UNCHANGED_SINCE_LAST_TURN


def test_user_added_block() -> None:
    appended = _with_blocks(
        [
            {
                "block_type": "text_prompt",
                "label": "summarize_result",
                "llm_key": "OPENAI_GPT5_MINI",
                "prompt": "Summarise success/failure.",
            }
        ]
    )
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(appended),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.added_block_labels == ("summarize_result",)
    assert summary.removed_block_labels == ()
    assert summary.modified_block_labels == ()


def test_user_removed_block() -> None:
    trimmed = _baseline_dict()
    trimmed["workflow_definition"]["blocks"] = [
        block for block in trimmed["workflow_definition"]["blocks"] if block["label"] != "fill_form"
    ]
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(trimmed),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.removed_block_labels == ("fill_form",)


def test_user_modified_block_in_place() -> None:
    edited = _baseline_dict()
    edited["workflow_definition"]["blocks"][1]["navigation_goal"] = "Fill out the form and click Submit."
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(edited),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.modified_block_labels == ("fill_form",)
    assert summary.added_block_labels == ()
    assert summary.removed_block_labels == ()


def test_user_added_parameter() -> None:
    expanded = _baseline_dict()
    expanded["workflow_definition"]["parameters"].append(
        {"parameter_type": "workflow", "key": "company", "workflow_parameter_type": "string"}
    )
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(expanded),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.added_parameter_keys == ("company",)


def test_user_modified_parameter_default_in_place() -> None:
    edited = _baseline_dict()
    edited["workflow_definition"]["parameters"][0]["default_value"] = "Jane Roe"
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(edited),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.modified_parameter_keys == ("applicant_name",)
    assert summary.added_parameter_keys == ()
    assert summary.removed_parameter_keys == ()
    assert "modified parameters: applicant_name" in summary.render_prompt_block()


def test_top_level_change_recorded() -> None:
    renamed = _baseline_dict()
    renamed["title"] = "renamed-workflow"
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(renamed),
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert "title" in summary.other_top_level_changes


def test_unparseable_current_yaml_falls_back_to_modified() -> None:
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=":\n: not yaml",
    )
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.added_block_labels == ()
    assert summary.structural_diff_unavailable is True
    assert "structural diff unavailable" in summary.render_prompt_block()


def test_prompt_block_first_turn() -> None:
    rendered = summarize_user_workflow_change(prior_yaml=None, current_yaml="").render_prompt_block()
    assert "first_turn_no_prior_state" in rendered


def test_prompt_block_unchanged() -> None:
    baseline = _dump(_baseline_dict())
    rendered = summarize_user_workflow_change(prior_yaml=baseline, current_yaml=baseline).render_prompt_block()
    assert "unchanged_since_last_turn" in rendered


def test_prompt_block_modified_lists_blocks() -> None:
    appended = _with_blocks(
        [{"block_type": "text_prompt", "label": "summarize_result", "llm_key": "x", "prompt": "ok"}]
    )
    rendered = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(appended),
    ).render_prompt_block()
    assert "user_modified_since_last_turn" in rendered
    assert "summarize_result" in rendered


def test_user_modified_block_inside_for_loop() -> None:
    base = _baseline_dict()
    base["workflow_definition"]["blocks"].append(
        {
            "block_type": "for_loop",
            "label": "for_each_row",
            "loop_over_parameter_key": "rows",
            "loop_blocks": [
                {
                    "block_type": "navigation",
                    "label": "process_row",
                    "navigation_goal": "Click the row's primary action.",
                }
            ],
        }
    )
    edited = copy.deepcopy(base)
    edited["workflow_definition"]["blocks"][2]["loop_blocks"][0]["navigation_goal"] = (
        "Click the row's secondary action."
    )
    summary = summarize_user_workflow_change(prior_yaml=_dump(base), current_yaml=_dump(edited))
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert "for_each_row" in summary.modified_block_labels
    assert "for_each_row/process_row" in summary.modified_block_labels


def test_user_added_block_inside_for_loop() -> None:
    base = _baseline_dict()
    base["workflow_definition"]["blocks"].append(
        {
            "block_type": "for_loop",
            "label": "for_each_row",
            "loop_over_parameter_key": "rows",
            "loop_blocks": [
                {
                    "block_type": "navigation",
                    "label": "process_row",
                    "navigation_goal": "Click the primary action.",
                }
            ],
        }
    )
    expanded = copy.deepcopy(base)
    expanded["workflow_definition"]["blocks"][2]["loop_blocks"].append(
        {
            "block_type": "extraction",
            "label": "extract_row_value",
            "data_extraction_goal": "Read the row's value.",
        }
    )
    summary = summarize_user_workflow_change(prior_yaml=_dump(base), current_yaml=_dump(expanded))
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert "for_each_row/extract_row_value" in summary.added_block_labels
    assert "for_each_row" in summary.modified_block_labels


def test_duplicate_block_labels_emit_structural_diff_unavailable() -> None:
    base = _baseline_dict()
    base["workflow_definition"]["blocks"].append(
        {"block_type": "extraction", "label": "open_site", "data_extraction_goal": "Read the heading."}
    )
    edited = copy.deepcopy(base)
    edited["workflow_definition"]["blocks"][0]["url"] = "https://example.com/other"
    summary = summarize_user_workflow_change(prior_yaml=_dump(base), current_yaml=_dump(edited))
    assert summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN
    assert summary.structural_diff_unavailable is True
    assert summary.added_block_labels == ()
    assert summary.modified_block_labels == ()


def test_added_block_truncates_long_list() -> None:
    extras = [
        {"block_type": "goto_url", "label": f"extra_block_{i}", "url": f"https://example.com/{i}"} for i in range(12)
    ]
    expanded = _with_blocks(copy.deepcopy(extras))
    summary = summarize_user_workflow_change(
        prior_yaml=_dump(_baseline_dict()),
        current_yaml=_dump(expanded),
    )
    rendered = summary.render_prompt_block()
    assert "extra_block_0" in rendered
    assert "(+4 more)" in rendered
