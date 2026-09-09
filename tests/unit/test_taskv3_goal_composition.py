"""Unit tests for Task V3 goal/prompt composition (skyvern/forge/taskv3/goal_composition.py)."""

from __future__ import annotations

import ast
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import skyvern.forge.taskv3
from skyvern.forge.taskv3.goal_composition import (
    MAX_HANDOFF_LABEL_CHARS,
    GoalDirectives,
    compose_goal,
    render_block_context,
)
from skyvern.forge.taskv3.workflow_position import PreviousBlockHandoff
from tests.unit._taskv3_block_fakes import PLAIN_URL
from tests.unit._taskv3_block_fakes import make_block as _make_block
from tests.unit.helpers import make_organization, make_task


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


def test_the_split_modules_stay_one_way_dependent_on_goal_composition() -> None:
    # goal_composition is the only module here that renders prompt text. Terminality analysis and
    # secret-egress filtering were pulled out of it precisely so neither can grow prompt wording;
    # an import back into it is how the three responsibilities would silently re-conflate.
    package_dir = Path(skyvern.forge.taskv3.__file__).parent
    for module in ("workflow_position", "handoff_redaction", "llm_call_params"):
        source = (package_dir / f"{module}.py").read_text()
        imported = {
            name.name if isinstance(node, ast.Import) else f"{node.module}.{name.name}"
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for name in node.names
        } | {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) and node.module}
        assert not any("goal_composition" in ref for ref in imported), (
            f"{module} imports goal_composition; the responsibilities are re-conflating"
        )
