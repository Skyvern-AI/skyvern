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
from skyvern.forge.sdk.schemas.tasks import TaskType
from skyvern.forge.sdk.workflow.models.block import ExtractionBlock
from skyvern.forge.taskv3.goal_composition import (
    MAX_HANDOFF_LABEL_CHARS,
    GoalDirectives,
    compose_goal,
    render_block_context,
)
from skyvern.forge.taskv3.workflow_position import PreviousBlockHandoff
from tests.unit._taskv3_block_fakes import PLAIN_URL
from tests.unit._taskv3_block_fakes import make_block as _make_block
from tests.unit._taskv3_block_fakes import output_param
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
    "criteria_precedence": (False, True),
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
    criteria_precedence: bool,
    framing: str,
    block_context_section: str,
) -> str:
    """The goal-patching expressions as `ForgeAgent._execute_task_v3` inlined them before `compose_goal`
    existed, frozen here as the oracle for the extraction, plus any directive a later commit deliberately
    reworded -- the overlap-precedence sentence is one such, so this is no longer a pure historical record.

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
    if complete_criterion and terminate_criterion and criteria_precedence:
        # SKY-16193: the one deliberate divergence from what agent.py inlined. Criteria that can hold
        # at once had no stated precedence, and v3 resolved that the opposite way to the engine they
        # were authored against. Updated here in the same commit as the production wording, which is
        # what this oracle's docstring asks of a change that means to reword a directive.
        goal = (
            f"{goal}\n\nIf the completion criterion and the termination criterion both hold at once, "
            "the completion criterion wins: finish with status=completed."
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


def test_two_criteria_that_can_hold_at_once_get_an_explicit_precedence() -> None:
    """A block whose criteria overlap has no correct answer without a precedence rule, and the two
    engines frame that choice differently (SKY-16193).

    The measured case: complete was "if no error message is present" and terminate ended "...or if
    pop up message is not available". On a page with neither an error nor a popup BOTH hold, and
    nothing told the model which to apply.

    Effect size, arm-resolved, over runs that REACHED the block: v3 terminated there 22/37 = 59.5%
    of the time against v1's 14/119 = 11.8%. That establishes something costly happens here; it
    does not establish what. What does is the composition. v1's `decisive-criterion-validate.j2`
    asks for "only one action" and resolves both criteria inside a single enum rule that names
    COMPLETE first (`:11`, `:15`); v3 renders the terminate criterion as its own standing interrupt
    -- "If this becomes true, stop and finish with status=terminated". The criteria were authored
    against v1, so v3 is the one that has to say which wins.

    And v1 fails this block too, 14 times in three days. Any mechanism proposed for it has to
    explain a 5x ratio, not a v3-only defect.
    """
    both = compose_goal(
        "Add the store.",
        GoalDirectives(
            complete_criterion="no error message is present",
            terminate_criterion="an error message is present, or the pop up is not available",
            criteria_precedence=True,
        ),
    )
    assert "the completion criterion wins" in both
    # Precedence for the OVERLAP only. An earlier draft added "terminate only when the termination
    # criterion holds", which reads as a ban on every other use of the status -- and `terminated` is
    # also how this engine reports being blocked, while v1 terminates whenever a complete criterion is
    # provided and not met. A precedence rule must not narrow the status it mentions.
    # The flag must add EXACTLY this paragraph and nothing else. An earlier guard asserted only that
    # ONE paragraph was added containing ONE full stop, which constrains punctuation rather than
    # content: round 1's prohibition walks straight through it joined by a semicolon, the naming fix
    # reverts to a demonstrative untouched, and even inverting the prescribed status passes. Asserting
    # the text verbatim is what makes the intent readable at the one place a reviewer looks.
    #
    # What equality buys, precisely: accidental defeat becomes impossible, and a DELIBERATE edit
    # becomes visible in the diff, because changing the sentence means retyping it here. It does NOT
    # make a lockstep edit impossible -- the oracle's docstring still invites one, and someone who
    # updates production, the oracle and this line together will pass. Naming that boundary is the
    # point; a fourth guard claiming to close it would repeat the error one level up.
    #
    # And it cannot see a prohibition appended to a DIFFERENT directive: the delta is between the
    # gated and ungated renders, so text added to both cancels. That is a property of the oracle
    # beside it rather than of this flag.
    #
    # AND IT IS THE SOLE CATCH FOR A LOCKSTEP EDIT. A lockstep change defeats the byte-identity mask
    # BY CONSTRUCTION -- production and the oracle agree again -- so all 512 mask cases stay GREEN and
    # only the assertion below reds. Three defeats are demonstrated against it: a semicolon for the
    # full stop, the prescribed status inverted to `terminated`, and a demonstrative restored in the
    # condition. Weaken, move or lose this one assertion in a refactor and all three go invisible
    # with every other test in the file still green.
    ungated_for_delta = compose_goal(
        "Add the store.",
        GoalDirectives(
            complete_criterion="no error message is present",
            terminate_criterion="an error message is present, or the pop up is not available",
        ),
    )
    added = [p for p in both.split("\n\n") if p not in ungated_for_delta.split("\n\n")]
    assert added == [
        "If the completion criterion and the termination criterion both hold at once, "
        "the completion criterion wins: finish with status=completed."
    ], added
    # Named rather than implied: the model must be able to tell which of the two it is applying.
    assert both.index("Consider the goal complete") < both.index("the completion criterion wins")

    # One criterion alone cannot conflict with anything, so it gets no precedence sentence.
    complete_only = compose_goal(
        "Add the store.", GoalDirectives(complete_criterion="the store is listed", criteria_precedence=True)
    )
    terminate_only = compose_goal(
        "Add the store.", GoalDirectives(terminate_criterion="the posting closed", criteria_precedence=True)
    )
    assert "the completion criterion wins" not in complete_only
    assert "the completion criterion wins" not in terminate_only

    # And it is OFF unless the caller asks: v1 shows the terminate criterion to a decision-maker only on
    # validation tasks, so a rule about which criterion wins has no measured meaning anywhere else.
    ungated = compose_goal(
        "Add the store.",
        GoalDirectives(complete_criterion="no error message is present", terminate_criterion="the pop up is missing"),
    )
    assert "the completion criterion wins" not in ungated
    # Worded without "the page": a page-free validation block is told it has no browser tools at all.
    assert "the page satisfies" not in both


def test_an_extraction_only_block_is_told_to_report_absent_data_as_completed_nulls() -> None:
    # v1 runs a block with no navigation goal as ONE extract action that returns nulls for whatever the page does
    # not show and completes; workflows branch on those nulls. v3 was told neither what such a block is for nor
    # what finishing it means, so it failed the block whenever an earlier block had not reached the data
    # (SKY-16398). Present on exactly v1's predicate, only in the treatment arm.
    now = datetime.now(UTC)
    org = make_organization(now)
    extraction_only = make_task(now, org, navigation_goal=None, data_extraction_goal="the license status")
    block = ExtractionBlock(
        label="blk", output_parameter=output_param("blk"), data_extraction_goal="the license status"
    )

    treated, _ = render_block_context(extraction_only, block, None, extraction_reports=True)
    control, _ = render_block_context(extraction_only, block, None, extraction_reports=False)

    added = [p for p in treated.split("\n\n") if p not in control.split("\n\n")]
    assert len(added) == 1, added
    assert "null" in added[0]
    assert "status=completed" in added[0]
    # Nulls cover only what the goal asks to read from the page: a value the goal asks the block to produce
    # (today's date, a value the goal states) must still come back, and page data must never be invented.
    assert "every field the page does not show set to null" not in added[0]
    assert "read from the page" in added[0]
    assert "never invent" in added[0]
    assert "current date" in added[0]
    # The control render is what shipped before the arm existed.
    assert control == render_block_context(extraction_only, block, None)[0]

    out_of_predicate = [
        make_task(now, org, navigation_goal="Search for the record", data_extraction_goal="the license status"),
        make_task(now, org, navigation_goal=None, data_extraction_goal=None),
        make_task(now, org, navigation_goal=None, data_extraction_goal="x", task_type=TaskType.validation),
    ]
    for task in out_of_predicate:
        assert render_block_context(task, block, None, extraction_reports=True) == render_block_context(
            task, block, None
        )
    # A task block carrying only an extraction goal keeps its fill tools, so it is not told it only reads.
    assert render_block_context(extraction_only, _make_block("blk"), None, extraction_reports=True) == (
        render_block_context(extraction_only, _make_block("blk"), None)
    )
    # A bare task has no workflow to route its nulls, so it gets no block framing at all.
    assert render_block_context(extraction_only, None, None, extraction_reports=True) == ("", "")
