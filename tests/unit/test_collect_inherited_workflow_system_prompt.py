"""Unit tests for ``WorkflowService._collect_inherited_workflow_system_prompt``.

The helper walks the parent workflow-run chain via
``app.DATABASE.workflow_runs.get_workflow_run`` + ``WorkflowService.get_workflow``
and joins each ancestor's raw ``workflow_system_prompt`` outermost-first. These
tests pin the chain-break-on-opt-out, cycle detection, outermost-first ordering,
and depth-cap semantics described in the helper's docstring (SKY-9147).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import WorkflowTriggerBlock
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService


def _make_workflow(workflow_id: str, workflow_system_prompt: str | None) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id="o_test",
        title=f"workflow {workflow_id}",
        workflow_permanent_id=f"wpid_{workflow_id}",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            parameters=[],
            blocks=[],
            workflow_system_prompt=workflow_system_prompt,
        ),
        created_at=now,
        modified_at=now,
    )


def _make_run(
    *,
    run_id: str,
    workflow_id: str,
    parent_run_id: str | None,
    skip_inherited: bool = False,
) -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id=run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=f"wpid_{workflow_id}",
        organization_id="o_test",
        status=WorkflowRunStatus.running,
        parent_workflow_run_id=parent_run_id,
        ignore_inherited_workflow_system_prompt=skip_inherited,
        created_at=now,
        modified_at=now,
    )


def _install_chain(
    monkeypatch: pytest.MonkeyPatch,
    runs: dict[str, WorkflowRun],
    workflows: dict[str, Workflow],
) -> tuple[AsyncMock, AsyncMock]:
    """Wire mocked ``get_workflow_run`` + ``get_workflow`` against the provided
    dicts. Returns both mocks so tests can assert call counts."""

    get_run = AsyncMock(side_effect=lambda run_id: runs.get(run_id))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_run)

    get_workflow = AsyncMock(side_effect=lambda workflow_id: workflows.get(workflow_id))
    monkeypatch.setattr(WorkflowService, "get_workflow", get_workflow)

    return get_run, get_workflow


@pytest.mark.asyncio
async def test_returns_none_when_parent_run_id_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_chain(monkeypatch, runs={}, workflows={})

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id=None)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_ancestor_has_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chain exists but every ancestor's ``workflow_system_prompt`` is None/empty."""
    runs = {
        "wr_parent": _make_run(run_id="wr_parent", workflow_id="w_parent", parent_run_id="wr_grandparent"),
        "wr_grandparent": _make_run(run_id="wr_grandparent", workflow_id="w_grandparent", parent_run_id=None),
    }
    workflows = {
        "w_parent": _make_workflow("w_parent", workflow_system_prompt=None),
        "w_grandparent": _make_workflow("w_grandparent", workflow_system_prompt=""),
    }
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result is None


@pytest.mark.asyncio
async def test_single_ancestor_with_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = {"wr_parent": _make_run(run_id="wr_parent", workflow_id="w_parent", parent_run_id=None)}
    workflows = {"w_parent": _make_workflow("w_parent", workflow_system_prompt="Respond in English.")}
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result == "Respond in English."


@pytest.mark.asyncio
async def test_outermost_first_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chain: great-grandparent -> grandparent -> parent -> (child). The helper
    walks up bottom-up starting from the parent; the returned string must join
    outermost-first (great-grandparent before grandparent before parent)."""
    runs = {
        "wr_parent": _make_run(run_id="wr_parent", workflow_id="w_parent", parent_run_id="wr_grand"),
        "wr_grand": _make_run(run_id="wr_grand", workflow_id="w_grand", parent_run_id="wr_great"),
        "wr_great": _make_run(run_id="wr_great", workflow_id="w_great", parent_run_id=None),
    }
    workflows = {
        "w_parent": _make_workflow("w_parent", workflow_system_prompt="PARENT rule."),
        "w_grand": _make_workflow("w_grand", workflow_system_prompt="GRAND rule."),
        "w_great": _make_workflow("w_great", workflow_system_prompt="GREAT rule."),
    }
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result == "GREAT rule.\n\nGRAND rule.\n\nPARENT rule."


@pytest.mark.asyncio
async def test_chain_break_includes_opted_out_ancestors_own_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an ancestor has ``ignore_inherited_workflow_system_prompt=True``, its own
    prompt is still collected but traversal stops — the grandparent's rules must
    NOT appear in the result (SKY-9147 design: an opted-out workflow rejects its
    parents' rules but still propagates its own to descendants)."""
    runs = {
        "wr_parent": _make_run(
            run_id="wr_parent",
            workflow_id="w_parent",
            parent_run_id="wr_grand",
            skip_inherited=True,
        ),
        "wr_grand": _make_run(run_id="wr_grand", workflow_id="w_grand", parent_run_id=None),
    }
    workflows = {
        "w_parent": _make_workflow("w_parent", workflow_system_prompt="PARENT rule."),
        "w_grand": _make_workflow("w_grand", workflow_system_prompt="GRAND rule."),
    }
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result == "PARENT rule."


@pytest.mark.asyncio
async def test_chain_break_opted_out_ancestor_with_no_own_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the opted-out ancestor has no prompt of its own and its ancestors do,
    the result is None — traversal still stops at the opted-out boundary."""
    runs = {
        "wr_parent": _make_run(
            run_id="wr_parent",
            workflow_id="w_parent",
            parent_run_id="wr_grand",
            skip_inherited=True,
        ),
        "wr_grand": _make_run(run_id="wr_grand", workflow_id="w_grand", parent_run_id=None),
    }
    workflows = {
        "w_parent": _make_workflow("w_parent", workflow_system_prompt=None),
        "w_grand": _make_workflow("w_grand", workflow_system_prompt="GRAND rule."),
    }
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result is None


@pytest.mark.asyncio
async def test_cycle_detection_breaks_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent chain that points back to an already-visited run must not infinite-
    loop. The helper should collect each unique ancestor's prompt exactly once."""
    runs = {
        "wr_a": _make_run(run_id="wr_a", workflow_id="w_a", parent_run_id="wr_b"),
        "wr_b": _make_run(run_id="wr_b", workflow_id="w_b", parent_run_id="wr_a"),
    }
    workflows = {
        "w_a": _make_workflow("w_a", workflow_system_prompt="A rule."),
        "w_b": _make_workflow("w_b", workflow_system_prompt="B rule."),
    }
    get_run, _ = _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_a")

    assert result == "B rule.\n\nA rule."
    # Each unique run fetched exactly once — no infinite loop.
    assert get_run.await_count == 2


@pytest.mark.asyncio
async def test_missing_parent_run_breaks_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``get_workflow_run`` returns None mid-walk (soft-deleted / race),
    traversal stops cleanly and returns whatever was collected so far."""
    runs = {
        "wr_parent": _make_run(run_id="wr_parent", workflow_id="w_parent", parent_run_id="wr_missing"),
    }
    workflows = {"w_parent": _make_workflow("w_parent", workflow_system_prompt="PARENT rule.")}
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result == "PARENT rule."


@pytest.mark.asyncio
async def test_missing_parent_workflow_skips_that_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``get_workflow`` returns None (deleted definition) the traversal still
    continues up the chain, silently skipping that level rather than aborting."""
    runs = {
        "wr_parent": _make_run(run_id="wr_parent", workflow_id="w_parent_missing", parent_run_id="wr_grand"),
        "wr_grand": _make_run(run_id="wr_grand", workflow_id="w_grand", parent_run_id=None),
    }
    workflows = {"w_grand": _make_workflow("w_grand", workflow_system_prompt="GRAND rule.")}
    _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_parent")

    assert result == "GRAND rule."


@pytest.mark.asyncio
async def test_depth_cap_bounds_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed deep chain must stop at ``MAX_TRIGGER_DEPTH`` — the helper
    collects ancestors up to the cap and returns those without hanging."""
    depth = WorkflowTriggerBlock.MAX_TRIGGER_DEPTH + 5
    runs: dict[str, WorkflowRun] = {}
    workflows: dict[str, Workflow] = {}
    for i in range(depth):
        run_id = f"wr_{i}"
        workflow_id = f"w_{i}"
        parent_id = f"wr_{i + 1}" if i + 1 < depth else None
        runs[run_id] = _make_run(run_id=run_id, workflow_id=workflow_id, parent_run_id=parent_id)
        workflows[workflow_id] = _make_workflow(workflow_id, workflow_system_prompt=f"rule {i}.")

    get_run, _ = _install_chain(monkeypatch, runs, workflows)

    result = await WorkflowService()._collect_inherited_workflow_system_prompt(parent_workflow_run_id="wr_0")

    # Exactly MAX_TRIGGER_DEPTH ancestors fetched; deeper ones dropped.
    assert get_run.await_count == WorkflowTriggerBlock.MAX_TRIGGER_DEPTH
    assert result is not None
    # Parts are "rule 0." … "rule (cap-1)." joined outermost-first.
    expected_parts = [f"rule {i}." for i in reversed(range(WorkflowTriggerBlock.MAX_TRIGGER_DEPTH))]
    assert result == "\n\n".join(expected_parts)
