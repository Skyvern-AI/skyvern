"""Regression guard: `_make_agent_result` is the only allowed
``AgentResult(...)`` constructor in `skyvern/forge/sdk/copilot/agent.py`.

The factory routes every ``AgentResult`` through the discovery counter
finalizer, so the per-chat budget survives every exit path of the agent
loop. Route-level constructors (e.g.
`skyvern/forge/sdk/routes/workflow_copilot.py`'s recoverable-failure path)
are intentionally outside this scope — they run before the agent loop or
on a route-side failure where the agent's counter is by definition zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

_AGENT_MODULE = Path(__file__).resolve().parents[2] / "skyvern/forge/sdk/copilot/agent.py"


def _agent_result_call_sites() -> list[tuple[str, int]]:
    """Return (function_name, line) for every ``AgentResult(...)`` call in agent.py."""
    source = _AGENT_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_AGENT_MODULE))

    sites: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._fn_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._fn_stack.append(node.name)
            self.generic_visit(node)
            self._fn_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._fn_stack.append(node.name)
            self.generic_visit(node)
            self._fn_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name) and func.id == "AgentResult":
                enclosing = self._fn_stack[-1] if self._fn_stack else "<module>"
                sites.append((enclosing, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def test_agent_result_is_only_constructed_inside_make_agent_result() -> None:
    sites = _agent_result_call_sites()
    offending = [(fn, ln) for fn, ln in sites if fn != "_make_agent_result"]
    assert not offending, (
        "Every AgentResult construction in agent.py must go through "
        "_make_agent_result(ctx, ...) so the discovery counter writeback fires "
        "on every exit. Offending sites:\n  "
        + "\n  ".join(f"line {ln} inside {fn or '<module>'}" for fn, ln in offending)
    )


def test_make_agent_result_actually_constructs_agent_result() -> None:
    """Belt-and-braces: ensure we didn't accidentally remove the one AgentResult call."""
    sites = _agent_result_call_sites()
    inside_factory = [site for site in sites if site[0] == "_make_agent_result"]
    assert len(inside_factory) >= 1, (
        f"_make_agent_result must construct exactly one AgentResult; found {len(inside_factory)} calls inside it"
    )
