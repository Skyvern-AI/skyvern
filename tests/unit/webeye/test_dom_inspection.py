import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_READ_ONLY_CALLERS = {
    "skyvern/forge/agent.py": {"_build_extract_action_prompt"},
    "skyvern/webeye/utils/dom.py": {
        "find_bound_label_by_direct_parent",
        "is_child_of_pdf_object",
        "is_safe_for_checkbox_direct_click",
        "resolve_http_href",
    },
}


def _calls_in_functions(path: str, function_names: set[str]) -> list[ast.Call]:
    tree = ast.parse(Path(path).read_text())
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) or node.name not in function_names:
            continue
        calls.extend(child for child in ast.walk(node) if isinstance(child, ast.Call))
    return calls


def test_read_only_dom_callers_do_not_use_raw_evaluate() -> None:
    offenders = []
    for path, function_names in _READ_ONLY_CALLERS.items():
        for call in _calls_in_functions(path, function_names):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "evaluate":
                offenders.append((path, call.lineno, ast.unparse(call)))

    assert offenders == []


def test_dom_inspection_interface_accepts_no_caller_supplied_code() -> None:
    from skyvern.webeye import dom_inspection

    public_operations = {
        name: operation
        for name, operation in inspect.getmembers(dom_inspection, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert set(public_operations) == {
        "read_current_url",
        "read_locator_tag_name",
        "read_resolved_anchor_href",
        "read_whether_link_or_button",
    }
    for operation in public_operations.values():
        assert not {"code", "debug", "expression", "script"} & set(inspect.signature(operation).parameters)


@pytest.mark.asyncio
async def test_read_current_url_uses_only_the_approved_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye import dom_inspection

    frame = object()
    evaluate = AsyncMock(return_value="https://example.com/")
    monkeypatch.setattr(dom_inspection.SkyvernFrame, "evaluate", evaluate)

    assert await dom_inspection.read_current_url(frame) == "https://example.com/"  # type: ignore[arg-type]
    evaluate.assert_awaited_once_with(frame=frame, expression="() => document.location.href")


@pytest.mark.asyncio
async def test_read_locator_tag_name_uses_only_the_approved_expression() -> None:
    from skyvern.webeye import dom_inspection

    locator = AsyncMock()
    locator.evaluate.return_value = "BUTTON"

    assert await dom_inspection.read_locator_tag_name(locator, timeout=123) == "BUTTON"
    locator.evaluate.assert_awaited_once_with("element => element.tagName", timeout=123)


@pytest.mark.asyncio
async def test_read_resolved_anchor_href_uses_only_the_approved_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye import dom_inspection

    frame = object()
    element = object()
    evaluate = AsyncMock(return_value="https://example.com/target")
    monkeypatch.setattr(dom_inspection.SkyvernFrame, "evaluate", evaluate)

    assert (
        await dom_inspection.read_resolved_anchor_href(frame, element)  # type: ignore[arg-type]
        == "https://example.com/target"
    )
    evaluate.assert_awaited_once_with(
        frame=frame,
        expression="(element) => element instanceof HTMLAnchorElement ? element.href : null",
        arg=element,
    )


@pytest.mark.asyncio
async def test_read_whether_link_or_button_uses_only_the_approved_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye import dom_inspection

    frame = object()
    element = object()
    evaluate = AsyncMock(return_value=True)
    monkeypatch.setattr(dom_inspection.SkyvernFrame, "evaluate", evaluate)

    assert await dom_inspection.read_whether_link_or_button(frame, element) is True  # type: ignore[arg-type]
    evaluate.assert_awaited_once_with(
        frame=frame,
        expression="(element) => element.matches('a[href], button')",
        arg=element,
    )


@pytest.mark.parametrize(
    "forbidden_node",
    (ast.FormattedValue, ast.JoinedStr, ast.Lambda),
)
def test_dom_inspection_evaluate_code_is_fixed_module_data(forbidden_node: type[ast.AST]) -> None:
    path = Path("skyvern/webeye/dom_inspection.py")
    tree = ast.parse(path.read_text())
    assert not any(isinstance(node, forbidden_node) for node in ast.walk(tree))

    evaluate_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "evaluate"
    ]
    assert evaluate_calls
    for call in evaluate_calls:
        expression = next((keyword.value for keyword in call.keywords if keyword.arg == "expression"), None)
        if expression is None and call.args:
            expression = call.args[0]
        assert isinstance(expression, ast.Name)
        assert expression.id.startswith("_READ_")
