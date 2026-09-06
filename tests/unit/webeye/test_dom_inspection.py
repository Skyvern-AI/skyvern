import ast
import contextlib
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
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
        "read_locator_is_content_editable",
        "read_locator_selected_state",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [True, False])
async def test_read_locator_selected_state_uses_only_the_approved_expression(raw: bool) -> None:
    from skyvern.webeye import dom_inspection

    locator = AsyncMock()
    locator.evaluate.return_value = raw

    assert await dom_inspection.read_locator_selected_state(locator, timeout=123) is raw
    locator.evaluate.assert_awaited_once_with(dom_inspection._READ_LOCATOR_SELECTED_STATE, timeout=123)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "true", 1, 0, {}, []])
async def test_read_locator_selected_state_rejects_any_non_boolean_read(raw: object) -> None:
    # Anything but a real boolean is an unreadable state, and callers fall open to one ordinary click.
    from skyvern.webeye import dom_inspection

    locator = AsyncMock()
    locator.evaluate.return_value = raw

    assert await dom_inspection.read_locator_selected_state(locator) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), ("true", None)])
async def test_read_locator_is_content_editable_uses_only_the_approved_expression(
    raw: object, expected: bool | None
) -> None:
    from skyvern.webeye import dom_inspection

    locator = AsyncMock()
    locator.evaluate.return_value = raw

    assert await dom_inspection.read_locator_is_content_editable(locator, timeout=123) is expected
    locator.evaluate.assert_awaited_once_with(dom_inspection._READ_LOCATOR_IS_CONTENT_EDITABLE, timeout=123)


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


# ---------------------------------------------------------------------------
# read_locator_selected_state parity matrix (SKY-14051)
#
# The cached-script click guard reads live selected/checked state from a bare Locator, with no
# scrape and no SkyvernElement.  Every shape it can meet must resolve to the same answer the agent's
# SkyvernElement-based resolver gives on the same DOM, so a cached replay and an agent run make the
# same suppress / click-once decision.
# ---------------------------------------------------------------------------

_SELECTED_STATE_FIXTURE_HTML = """<!doctype html><html><body>
<input type="checkbox" id="native-checkbox-checked" checked>
<input type="checkbox" id="native-checkbox-unchecked">
<input type="radio" id="native-radio-checked" name="r" checked>
<input type="radio" id="native-radio-unchecked" name="r2">
<input type="text" id="native-text-input" value="x">
<input type="submit" id="native-submit-input" value="Go">

<div id="aria-checkbox-true" role="checkbox" aria-checked="true">a</div>
<div id="aria-checkbox-false" role="checkbox" aria-checked="false">b</div>
<div id="aria-checkbox-mixed" role="checkbox" aria-checked="mixed">c</div>
<div id="aria-checkbox-blank" role="checkbox" aria-checked="">d</div>
<div id="aria-checked-padded" role="checkbox" aria-checked="  TRUE  ">e</div>
<button id="aria-pressed-true" aria-pressed="true">f</button>
<button id="aria-pressed-false" aria-pressed="false">g</button>
<div id="aria-checked-wins-over-pressed" role="checkbox" aria-checked="false" aria-pressed="true">h</div>
<div id="aria-selected-row" role="row" aria-selected="true">i</div>

<div role="listbox">
  <div id="single-select-option-selected" role="option" aria-selected="true">j</div>
  <div id="single-select-option-unselected" role="option" aria-selected="false">k</div>
</div>
<div role="listbox" aria-multiselectable="true">
  <div id="multiselect-option-selected" role="option" aria-selected="true">l</div>
</div>
<div role="listbox" aria-multiselectable="false">
  <div id="explicit-single-select-option-selected" role="option" aria-selected="true">m</div>
</div>

<label id="label-explicit-checked" for="explicit-control">n</label>
<input type="checkbox" id="explicit-control" checked>
<label id="label-explicit-unchecked" for="explicit-control-off">o</label>
<input type="checkbox" id="explicit-control-off">
<label id="label-implicit-checked">p<span><input type="radio" name="r3" checked></span></label>
<label id="label-implicit-hidden-checked">q<input type="checkbox" style="display:none" checked></label>
<label id="label-two-controls">r<input type="checkbox" checked><input type="checkbox"></label>
<label id="label-dangling-for" for="no-such-control">s</label>
<label id="label-non-toggle-control" for="native-text-input">t</label>

<div role="grid">
  <div role="row">
    <div role="gridcell"><input type="checkbox" id="grid-row-checkbox-checked" checked></div>
  </div>
</div>

<div id="plain-div">u</div>
</body></html>"""

# id -> the one live answer both resolvers must agree on. None = unreadable (fall open to one click).
_SELECTED_STATE_MATRIX: dict[str, bool | None] = {
    "native-checkbox-checked": True,
    "native-checkbox-unchecked": False,
    "native-radio-checked": True,
    "native-radio-unchecked": False,
    "native-text-input": None,
    "native-submit-input": None,
    "aria-checkbox-true": True,
    "aria-checkbox-false": False,
    "aria-checkbox-mixed": None,
    "aria-checkbox-blank": None,
    "aria-checked-padded": True,
    "aria-pressed-true": True,
    "aria-pressed-false": False,
    "aria-checked-wins-over-pressed": False,
    "aria-selected-row": True,
    "single-select-option-selected": None,
    "single-select-option-unselected": False,
    "multiselect-option-selected": True,
    "explicit-single-select-option-selected": None,
    "label-explicit-checked": True,
    "label-explicit-unchecked": False,
    "label-implicit-checked": True,
    "label-implicit-hidden-checked": True,
    "label-two-controls": None,
    "label-dangling-for": None,
    "label-non-toggle-control": None,
    "plain-div": None,
}

_STATIC_ELEMENT_JS = """
(el) => {
  const attributes = {};
  for (const attr of el.attributes) { attributes[attr.name] = attr.value; }
  return {tagName: el.tagName.toLowerCase(), attributes: attributes};
}
"""


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


@contextlib.asynccontextmanager
async def _selected_state_page() -> AsyncIterator[Any]:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.set_content(_SELECTED_STATE_FIXTURE_HTML)
            yield page
        finally:
            await browser.close()


async def _agent_resolver_state(page: Any, css_id: str) -> bool | None:
    """The same live read through the agent's SkyvernElement resolvers, for parity."""
    from skyvern.webeye.actions import handler  # noqa: PLC0415
    from skyvern.webeye.utils.dom import SkyvernElement  # noqa: PLC0415

    locator = page.locator(f"#{css_id}")
    element = SkyvernElement(locator, page, await locator.evaluate(_STATIC_ELEMENT_JS))
    if element.get_tag_name() == "label":
        return await handler._read_label_control_state(element)
    return await handler._resolve_live_selected_state(element)


@_skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize(("css_id", "expected"), sorted(_SELECTED_STATE_MATRIX.items()))
async def test_read_locator_selected_state_parity_matrix(css_id: str, expected: bool | None) -> None:
    from skyvern.webeye import dom_inspection  # noqa: PLC0415

    async with _selected_state_page() as page:
        assert await dom_inspection.read_locator_selected_state(page.locator(f"#{css_id}")) is expected
        assert await _agent_resolver_state(page, css_id) is expected


@_skip_no_browser
@pytest.mark.asyncio
async def test_checkbox_inside_an_aria_grid_reads_as_unreadable() -> None:
    # A grid row-selection checkbox's native `checked` can diverge from the app's row selection
    # (SKY-13695). The agent resolves that through the row; the locator-scoped reader has no row
    # snapshot, so it reports unreadable and the cached click falls open rather than mis-suppressing.
    from skyvern.webeye import dom_inspection  # noqa: PLC0415

    async with _selected_state_page() as page:
        locator = page.locator("#grid-row-checkbox-checked")
        assert await locator.is_checked() is True
        assert await dom_inspection.read_locator_selected_state(locator) is None


@_skip_no_browser
@pytest.mark.asyncio
async def test_read_locator_selected_state_reads_state_live_not_from_markup() -> None:
    from skyvern.webeye import dom_inspection  # noqa: PLC0415

    async with _selected_state_page() as page:
        locator = page.locator("#native-checkbox-unchecked")
        assert await dom_inspection.read_locator_selected_state(locator) is False

        await locator.check()

        assert await dom_inspection.read_locator_selected_state(locator) is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_read_locator_selected_state_raises_for_a_missing_element() -> None:
    # Callers own the fall-open decision, so an unresolvable locator surfaces rather than being
    # silently reported as a readable state.
    from playwright.async_api import Error as PlaywrightError  # noqa: PLC0415

    from skyvern.webeye import dom_inspection  # noqa: PLC0415

    async with _selected_state_page() as page:
        with pytest.raises((PlaywrightError, TimeoutError)):
            await dom_inspection.read_locator_selected_state(page.locator("#nope"), timeout=250)
