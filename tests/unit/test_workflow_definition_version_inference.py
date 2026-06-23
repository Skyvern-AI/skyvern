"""Tests for workflow_definition.version inference and graph validation.

version describes only top-level routing. A definition is v2 when its top-level blocks use a
conditional or explicit next_block_label; loop interiors are graph-built independently and do not
promote the top-level version. Conditional branch chains that omit their merge edge are resolved by
the shared SKY-8571 merge resolver at both the top level and inside loops.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import (
    convert_workflow_definition,
)
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest

_MISSING = object()


def _navigation_block(label: str, next_block_label: str | None | object = _MISSING) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": "navigation",
        "label": label,
        "navigation_goal": f"Visit {label}",
        "url": "https://example.com",
    }
    if next_block_label is not _MISSING:
        block["next_block_label"] = next_block_label
    return block


def _conditional_block(label: str, next_block_label: str | None) -> dict[str, object]:
    return {
        "block_type": "conditional",
        "label": label,
        "branch_conditions": [
            {
                "is_default": True,
                "next_block_label": next_block_label,
            },
        ],
    }


def _conditional_block_with_merge(
    label: str,
    branch_labels: list[str],
    merge_label: str,
) -> dict[str, object]:
    return {
        "block_type": "conditional",
        "label": label,
        "next_block_label": merge_label,
        "branch_conditions": [
            {
                "is_default": index == len(branch_labels) - 1,
                "next_block_label": branch_label,
                **(
                    {}
                    if index == len(branch_labels) - 1
                    else {"criteria": {"criteria_type": "jinja2_template", "expression": "{{ true }}"}}
                ),
            }
            for index, branch_label in enumerate(branch_labels)
        ],
    }


def _for_loop_block(
    label: str,
    loop_blocks: list[dict[str, object]],
    next_block_label: str | None | object = _MISSING,
) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": "for_loop",
        "label": label,
        "loop_variable_reference": "item",
        "loop_blocks": loop_blocks,
    }
    if next_block_label is not _MISSING:
        block["next_block_label"] = next_block_label
    return block


def _request_payload(
    blocks: list[dict[str, object]],
    version: int | None | object = _MISSING,
) -> dict[str, object]:
    workflow_definition: dict[str, object] = {
        "parameters": [],
        "blocks": blocks,
    }
    if version is not _MISSING:
        workflow_definition["version"] = version
    return {
        "title": "Version inference test",
        "workflow_definition": workflow_definition,
    }


def _validate_request(
    blocks: list[dict[str, object]],
    version: int | None | object = _MISSING,
) -> WorkflowCreateYAMLRequest:
    return WorkflowCreateYAMLRequest.model_validate(_request_payload(blocks, version))


def test_omitted_version_defaults_to_one_for_sequential_workflows() -> None:
    """Definitions with no top-level graph constructs remain legacy v1."""
    request = _validate_request([_navigation_block("first"), _navigation_block("second")])
    assert request.workflow_definition.version == 1


def test_omitted_version_infers_two_for_top_level_conditional() -> None:
    request = _validate_request([_conditional_block("choose_path", "done"), _navigation_block("done")])
    assert request.workflow_definition.version == 2
    converted = convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
    assert converted.version == 2


def test_omitted_version_infers_two_for_top_level_next_block_label() -> None:
    request = _validate_request([_navigation_block("first", "second"), _navigation_block("second", None)])
    assert request.workflow_definition.version == 2


def test_nested_only_v2_constructs_keep_top_level_v1() -> None:
    """A sequential top level stays v1 even when a loop body uses v2 constructs; the blocks are not
    mutated and the definition is accepted (validation is skipped for v1)."""
    request = _validate_request(
        [
            _for_loop_block(
                "loop",
                [
                    _navigation_block("inner_first", "inner_second"),
                    _navigation_block("inner_second", None),
                ],
            ),
            _navigation_block("after_loop"),
        ]
    )
    assert request.workflow_definition.version == 1
    loop_block, after_loop_block = request.workflow_definition.blocks
    assert loop_block.next_block_label is None
    assert after_loop_block.next_block_label is None
    converted = convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
    WorkflowService().validate_workflow_block_graph(converted)


def test_conditional_branch_terminals_validate_via_merge_resolution() -> None:
    """A top-level conditional whose branches omit the merge edge validates: the shared resolver
    reconnects each branch terminal to the conditional's successor. Blocks are not mutated on parse."""
    request = _validate_request(
        [
            _conditional_block_with_merge("choose_path", ["branch_a", "branch_b"], "merge"),
            _navigation_block("branch_a"),
            _navigation_block("branch_b"),
            _navigation_block("merge"),
        ]
    )
    assert request.workflow_definition.version == 2
    _, branch_a_block, branch_b_block, merge_block = request.workflow_definition.blocks
    assert branch_a_block.next_block_label is None
    assert branch_b_block.next_block_label is None
    converted = convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
    WorkflowService().validate_workflow_block_graph(converted)


def test_loop_with_nested_conditional_validates_via_shared_merge_resolution() -> None:
    """SKY-8571: loop validation uses the same merge resolution as the top-level builder, so a loop
    body with a conditional whose branch omits the merge edge is not rejected as disconnected."""
    request = _validate_request(
        [
            _navigation_block("start", "loop"),
            _for_loop_block(
                "loop",
                [
                    _conditional_block_with_merge("inner_choice", ["leaf"], "inner_merge"),
                    _navigation_block("leaf"),
                    _navigation_block("inner_merge"),
                ],
            ),
        ]
    )
    assert request.workflow_definition.version == 2
    converted = convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
    WorkflowService().validate_workflow_block_graph(converted)


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param([_conditional_block("choose_path", None)], id="conditional"),
        pytest.param(
            [_navigation_block("first", "second"), _navigation_block("second", None)],
            id="next_block_label",
        ),
    ],
)
def test_explicit_version_one_rejects_top_level_v2_constructs(blocks: list[dict[str, object]]) -> None:
    """Explicit v1 definitions must not contain top-level v2-only graph constructs."""
    with pytest.raises(ValidationError, match="version.*2"):
        _validate_request(blocks, version=1)


def test_explicit_version_one_allows_nested_only_constructs() -> None:
    """A v1 top level is valid even when a loop body uses v2 constructs."""
    request = _validate_request(
        [
            _for_loop_block(
                "loop",
                [
                    _navigation_block("inner_first", "inner_second"),
                    _navigation_block("inner_second", None),
                ],
            )
        ],
        version=1,
    )
    assert request.workflow_definition.version == 1
