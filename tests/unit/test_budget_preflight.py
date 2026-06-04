"""Tests for budget/max-steps preflight check before speculative scrape+LLM."""

import ast
import inspect
import textwrap

from skyvern.forge import agent as agent_module


def _get_await_order(source: str, target_names: list[str]) -> list[str]:
    """Return the order in which target coroutine names are awaited in source."""
    tree = ast.parse(textwrap.dedent(source))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        val = node.value
        name = None
        if isinstance(val, ast.Call):
            func = val.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
        elif isinstance(val, ast.Name):
            name = val.id
        if name and name in target_names and name not in found:
            found.append(name)
    return found


def test_budget_check_before_speculative_await() -> None:
    """_check_workflow_run_step_budget must be called before speculative_task is awaited."""
    source = inspect.getsource(agent_module.ForgeAgent._handle_completed_step_with_parallel_verification)
    order = _get_await_order(source, ["_check_workflow_run_step_budget", "speculative_task"])
    assert "_check_workflow_run_step_budget" in order, "Budget check not found in function"
    budget_idx = order.index("_check_workflow_run_step_budget")
    # speculative_task may appear multiple times (cancel path + normal path);
    # the first await of speculative_task should be AFTER the budget check
    assert "speculative_task" in order, "speculative_task await not found"
    spec_idx = order.index("speculative_task")
    assert budget_idx < spec_idx, (
        f"Budget check (position {budget_idx}) must come before speculative_task await (position {spec_idx})"
    )


def test_speculative_task_cancelled_on_budget_path() -> None:
    """The code must call speculative_task.cancel() when budget is exhausted."""
    source = inspect.getsource(agent_module.ForgeAgent._handle_completed_step_with_parallel_verification)
    assert "speculative_task.cancel()" in source, "Missing speculative_task.cancel() call"
    assert "budget_exhausted" in source, "Missing budget_exhausted variable"
    assert "steps_exhausted" in source, "Missing steps_exhausted variable"
