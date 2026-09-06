"""Unit tests for the Task V3 cross-block handoff context (skyvern/forge/taskv3/block_context.py)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import ForLoopBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.taskv3.block_context import (
    MAX_HANDOFF_LABEL_CHARS,
    MAX_HANDOFF_REASON_CHARS,
    MAX_HANDOFF_URL_CHARS,
    GoalDirectives,
    PreviousBlockHandoff,
    compose_goal,
    is_last_block,
    mask_signed_urls_in_text,
    render_block_context,
    sanitize_handoff_reason,
    sanitize_handoff_url,
    select_previous_block,
)
from skyvern.schemas.workflows import BlockStatus, BlockType
from tests.unit.helpers import make_organization, make_task

# Known-signed example (mirrors tests/unit/test_taskv3_opaque_refs.py::SIGNED): a JWT-shaped token
# value under an allowlisted "token" key, which is_signed_url() flags as a signing artifact.
SIGNED_URL = (
    "https://files.example.test/uploads/a1b2c3d4e5f6/resume.pdf"
    "?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.Q29ycmVjdEhvcnNlQmF0dGVyeVN0YXBsZTAxMjM0NTY3ODk"
)
PLAIN_URL = "https://portfolio.example.test/jobs/12345"


def _output_param(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id=f"op_{key}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_block(label: str = "blk") -> TaskBlock:
    return TaskBlock(label=label, output_parameter=_output_param(label))


def _run_block(**overrides: Any) -> WorkflowRunBlock:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "workflow_run_block_id": "wrb_1",
        "workflow_run_id": "wr_1",
        "organization_id": "org-123",
        "block_type": BlockType.TASK,
        "created_at": now,
        "modified_at": now,
    }
    base.update(overrides)
    return WorkflowRunBlock(**base)


# ---------------------------------------------------------------------------
# sanitize_handoff_reason
# ---------------------------------------------------------------------------


def test_sanitize_handoff_reason_collapses_multiline_to_one_line() -> None:
    assert sanitize_handoff_reason("line one\nline two\r\n  line three") == "line one line two line three"


def test_sanitize_handoff_reason_masks_signed_url_but_keeps_plain_url() -> None:
    text = f"stopped at {SIGNED_URL} after also visiting {PLAIN_URL}"
    result = sanitize_handoff_reason(text)
    assert result is not None
    assert "[signed-url]" in result
    assert SIGNED_URL not in result
    assert PLAIN_URL in result  # nosemgrep: incomplete-url-substring-sanitization


def test_sanitize_handoff_reason_truncates_overlong_text() -> None:
    text = "x" * (MAX_HANDOFF_REASON_CHARS + 50)
    result = sanitize_handoff_reason(text)
    assert result is not None
    assert len(result) <= MAX_HANDOFF_REASON_CHARS
    assert result.endswith("…")


@pytest.mark.parametrize("value", [None, ""])
def test_sanitize_handoff_reason_none_or_empty_returns_none(value: str | None) -> None:
    assert sanitize_handoff_reason(value) is None


# ---------------------------------------------------------------------------
# sanitize_handoff_url
# ---------------------------------------------------------------------------


def test_sanitize_handoff_url_strips_query_and_fragment() -> None:
    result = sanitize_handoff_url("https://example.test/path/to/page?foo=bar#section")
    assert result == "https://example.test/path/to/page"
    assert (
        sanitize_handoff_url("https://admin:s3cr3t@example.com:8443/dashboard?token=abc")
        == "https://example.com:8443/dashboard"
    )


@pytest.mark.parametrize("value", [None, "", "not a url", "just some prose about a page", "https://host:notaport/x"])
def test_sanitize_handoff_url_non_url_returns_none(value: str | None) -> None:
    assert sanitize_handoff_url(value) is None


def test_sanitize_handoff_url_truncates_overlong_path() -> None:
    long_path = "/segment" * ((MAX_HANDOFF_URL_CHARS + 50) // 8)
    result = sanitize_handoff_url(f"https://example.test{long_path}")
    assert result is not None
    assert len(result) <= MAX_HANDOFF_URL_CHARS
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# is_last_block
# ---------------------------------------------------------------------------


def _make_workflow_run_context(blocks: list) -> MagicMock:
    ctx = MagicMock()
    ctx.workflow.workflow_definition.blocks = blocks
    # A bare MagicMock attribute is truthy; a real definition has no finally block unless set.
    ctx.workflow.workflow_definition.finally_block_label = None
    return ctx


def test_is_last_block_true_for_last_label() -> None:
    first, last = _make_block("first"), _make_block("last")
    workflow_run_context = _make_workflow_run_context([first, last])
    assert is_last_block(last, workflow_run_context) is True


def test_is_last_block_false_for_first_label() -> None:
    first, last = _make_block("first"), _make_block("last")
    workflow_run_context = _make_workflow_run_context([first, last])
    assert is_last_block(first, workflow_run_context) is False


def test_is_last_block_none_when_label_not_in_definition() -> None:
    workflow_run_context = _make_workflow_run_context([_make_block("other")])
    assert is_last_block(_make_block("missing"), workflow_run_context) is None


def test_is_last_block_none_when_workflow_run_context_is_none() -> None:
    assert is_last_block(_make_block("solo"), None) is None


def test_is_last_block_none_for_dag_workflow_with_next_block_label() -> None:
    # A DAG workflow executes along next_block_label edges, not definition order, so position in
    # the flattened list is meaningless for any block once any block declares an edge.
    first, last = _make_block("first"), _make_block("last")
    first.next_block_label = "last"
    workflow_run_context = _make_workflow_run_context([first, last])

    assert is_last_block(first, workflow_run_context) is None
    assert is_last_block(last, workflow_run_context) is None


def test_is_last_block_none_when_nested_inside_trailing_loop() -> None:
    inner = _make_block("inner")
    first = _make_block("first")
    loop_block = ForLoopBlock(label="loop", output_parameter=_output_param("loop"), loop_blocks=[inner])
    workflow_run_context = _make_workflow_run_context([first, loop_block])

    assert is_last_block(inner, workflow_run_context) is None
    assert is_last_block(first, workflow_run_context) is False


# ---------------------------------------------------------------------------
# select_previous_block
# ---------------------------------------------------------------------------


def test_select_previous_block_returns_none_with_no_candidates() -> None:
    assert select_previous_block([], "wrb_current") is None


def test_select_previous_block_skips_current_running_loop_and_future_rows() -> None:
    now = datetime.now(UTC)
    current = _run_block(
        workflow_run_block_id="wrb_current", task_id="task_current", created_at=now, status=BlockStatus.running
    )
    older_terminal = _run_block(
        workflow_run_block_id="wrb_older",
        created_at=now - timedelta(minutes=10),
        status=BlockStatus.completed,
        label="older",
        finish_reason="older finish",
    )
    newer_terminal = _run_block(
        workflow_run_block_id="wrb_newer",
        created_at=now - timedelta(minutes=5),
        status=BlockStatus.completed,
        label="newer",
        finish_reason="newer finish",
    )
    still_running = _run_block(
        workflow_run_block_id="wrb_running",
        created_at=now - timedelta(minutes=1),
        status=BlockStatus.running,
        label="running",
    )
    loop_container = _run_block(
        workflow_run_block_id="wrb_loop",
        created_at=now - timedelta(minutes=1),
        status=BlockStatus.completed,
        block_type=BlockType.FOR_LOOP,
        label="loop",
    )
    skipped_row = _run_block(
        workflow_run_block_id="wrb_skipped",
        created_at=now - timedelta(seconds=30),
        status=BlockStatus.skipped,
        label="skipped",
    )
    created_after_current = _run_block(
        workflow_run_block_id="wrb_future",
        created_at=now + timedelta(minutes=1),
        status=BlockStatus.completed,
        label="future",
    )
    blocks = [
        current,
        older_terminal,
        newer_terminal,
        still_running,
        loop_container,
        skipped_row,
        created_after_current,
    ]

    result = select_previous_block(blocks, "task_current")

    assert result is not None
    assert result.label == "newer"
    assert result.reason == "newer finish"


def test_select_previous_block_reason_prefers_finish_reason_over_failure_reason() -> None:
    row = _run_block(status=BlockStatus.failed, finish_reason="finish text", failure_reason="failure text")
    result = select_previous_block([row], None)
    assert result is not None
    assert result.reason == "finish text"


def test_select_previous_block_reason_falls_back_to_failure_reason() -> None:
    row = _run_block(status=BlockStatus.failed, finish_reason=None, failure_reason="failure text")
    result = select_previous_block([row], None)
    assert result is not None
    assert result.reason == "failure text"


# ---------------------------------------------------------------------------
# render_block_context
# ---------------------------------------------------------------------------


def test_render_block_context_section_empty_when_handoff_disabled() -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), data_extraction_goal=None)
    previous = PreviousBlockHandoff(label="prev", status="failed", reason="captcha blocked", final_url=PLAIN_URL)

    _framing, section = render_block_context(
        task, _make_block("blk"), None, handoff_enabled=False, previous_block=previous
    )

    assert section == ""


def test_render_block_context_section_includes_label_status_reason_and_url_when_enabled() -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), data_extraction_goal=None)
    long_label = "checkout" + "x" * MAX_HANDOFF_LABEL_CHARS
    previous = PreviousBlockHandoff(
        label=long_label, status="failed", reason="captcha never cleared", final_url=PLAIN_URL
    )

    _framing, section = render_block_context(
        task, _make_block("blk"), None, handoff_enabled=True, previous_block=previous
    )

    assert "checkout" in section
    assert "status: failed" in section
    assert "captcha never cleared" in section
    assert PLAIN_URL in section  # nosemgrep: incomplete-url-substring-sanitization
    assert long_label not in section


def test_render_block_context_section_empty_when_no_previous_and_last_unknown() -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), data_extraction_goal=None)

    _framing, section = render_block_context(task, _make_block("blk"), None, handoff_enabled=True, previous_block=None)

    assert section == ""


def test_is_last_block_none_when_finally_block_configured() -> None:
    first, last = _make_block("first"), _make_block("cleanup_finally")
    ctx = _make_workflow_run_context([last, first])
    ctx.workflow.workflow_definition.finally_block_label = "cleanup_finally"
    assert is_last_block(first, ctx) is None
    assert is_last_block(last, ctx) is None


def test_is_last_block_none_when_definition_has_a_conditional_block() -> None:
    first, last = _make_block("first"), _make_block("last")
    conditional = MagicMock()
    conditional.label = "router"
    conditional.block_type = BlockType.CONDITIONAL
    conditional.next_block_label = None
    ctx = _make_workflow_run_context([first, conditional, last])
    assert is_last_block(last, ctx) is None
    assert is_last_block(first, ctx) is None


def test_sanitize_handoff_url_scrubs_token_shaped_path_segments() -> None:
    # A magic link carries its credential in the path; no secret registry knows it.
    token = "a3f9c2e8d1b4a7f6c9e2d5b8a1f4c7e0"
    result = sanitize_handoff_url(f"https://example.test/reset/{token}/confirm")
    assert result == "https://example.test/reset/***/confirm"
    assert sanitize_handoff_url("https://example.test/jobs/apply") == "https://example.test/jobs/apply"


def test_mask_signed_urls_in_text_masks_only_signing_shaped_urls() -> None:
    signed = "https://example.test/cb?signature=" + "A1b2" * 12
    text = f"stopped at {signed} after visiting https://example.test/plain"
    masked = mask_signed_urls_in_text(text)
    assert "[signed-url]" in masked
    assert "signature=" not in masked
    assert "https://example.test/plain" in masked  # nosemgrep: incomplete-url-substring-sanitization


def test_sanitize_handoff_url_scrubs_percent_encoded_token_segments() -> None:
    encoded = "qh%2Fx" + "A1b2" * 8  # decodes to a high-entropy base64-with-slash token
    result = sanitize_handoff_url(f"https://example.test/reset/{encoded}/done")
    assert result == "https://example.test/reset/***/done"


def test_mask_signed_urls_in_text_survives_trailing_punctuation() -> None:
    signed = "https://example.test/cb?signature=" + "A1b2" * 12
    masked = mask_signed_urls_in_text(f"stopped (see {signed}).")
    assert "signature=" not in masked
    assert masked.endswith("[signed-url]).")


def test_is_last_block_none_for_partial_run_selection() -> None:
    first, last = _make_block("first"), _make_block("last")
    ctx = _make_workflow_run_context([first, last])
    assert is_last_block(last, ctx, selected_block_labels=["last"]) is None
    # A selection covering the whole definition is a full run; position is still meaningful.
    assert is_last_block(last, ctx, selected_block_labels=["first", "last"]) is True


def _goal_as_agent_py_built_it(
    navigation_goal: str,
    *,
    data_extraction_goal: str | None,
    extracted_information_schema: Any,
    complete_criterion: str | None,
    terminate_criterion: str | None,
    error_code_mapping: dict[str, str] | None,
    framing: str,
    block_context_section: str,
) -> str:
    """The goal-patching expressions exactly as `ForgeAgent._execute_task_v3` inlined them before
    `compose_goal` existed, frozen here as the oracle for the extraction.

    This is deliberately a duplicate of production wording: it is the only thing that can catch a
    single dropped space or reordered clause in a refactor whose entire acceptance bar is that the
    string the model receives did not move. If a future change intends to reword a directive, it
    changes this function in the same commit and the diff shows the intent.
    """
    goal = navigation_goal
    if data_extraction_goal:
        goal = (
            f"{goal}\n\nWhen the page goal is met, extract the requested data and return it as the "
            f"`extracted_output` argument to finish. Data to extract: {data_extraction_goal}"
        ).strip()
    if extracted_information_schema:
        goal = (
            f"{goal}\n\nThe extracted_output MUST be valid JSON conforming to this schema:\n"
            f"{json.dumps(extracted_information_schema, default=str)}"
        ).strip()
    if complete_criterion:
        goal = (
            f"{goal}\n\nConsider the goal complete, and finish with status=completed, only when: {complete_criterion}"
        ).strip()
    if terminate_criterion:
        goal = (
            f"{goal}\n\nIf this becomes true, stop and finish with status=terminated: {terminate_criterion}"
        ).strip()
    if error_code_mapping:
        goal = (
            f"{goal}\n\nThe user defined these business outcomes and their descriptions:\n"
            f"```\n{json.dumps(error_code_mapping, indent=2)}\n```\n"
            "If one of these descriptions is what actually happened, set error_code to exactly "
            "that code, on whatever finish status is honest -- choose the status on its own "
            "merits, never to make a code fit. Do not return a code the user did not define, and "
            "do not stretch a description to cover something it does not say. Never use a code to "
            "describe a failure of YOU or the browser -- being stuck, losing track of which page "
            "you are on, running out of steps, or simply not managing the task are ours to "
            "report, so finish those WITHOUT an error_code. A problem with the SITE may take a "
            "code when the user defined one whose description names that problem."
        ).strip()
    if framing:
        goal = f"{goal}\n\n{framing}".strip()
    if block_context_section:
        goal = f"{goal}\n\n{block_context_section}".strip()
    return goal


_DIRECTIVE_VALUES: dict[str, tuple[Any, Any]] = {
    "data_extraction_goal": (None, "the applicant reference number"),
    "extracted_information_schema": (None, {"type": "object", "properties": {"ref": {"type": "string"}}}),
    "complete_criterion": (None, "the confirmation page is showing"),
    "terminate_criterion": (None, "the form says the role is closed"),
    "error_code_mapping": (None, {"ROLE_CLOSED": "the posting is no longer accepting submissions"}),
    "framing": ("", "This is one block of a larger workflow."),
    "block_context_section": ("", "<workflow_context>\nblocks: one, two\n</workflow_context>"),
}


@pytest.mark.parametrize("navigation_goal", ["", "Apply to the posting for Jane Doe."])
@pytest.mark.parametrize("mask", list(itertools.product([0, 1], repeat=len(_DIRECTIVE_VALUES))))
def test_compose_goal_is_byte_identical_to_the_inline_patching_it_replaced(
    navigation_goal: str, mask: tuple[int, ...]
) -> None:
    # Every on/off combination of the seven directives, against both an empty and a non-empty
    # navigation goal. The empty one matters: the first directive's .strip() is what decides
    # whether the goal opens with a blank line, and that only shows up when the base is "".
    chosen = {name: options[bit] for (name, options), bit in zip(_DIRECTIVE_VALUES.items(), mask)}
    assert compose_goal(navigation_goal, GoalDirectives(**chosen)) == _goal_as_agent_py_built_it(
        navigation_goal, **chosen
    )
