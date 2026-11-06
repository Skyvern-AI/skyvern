"""Regression tests for autocomplete input detection.

Covers direct attribute detection in is_auto_completion_input().
"""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler as handler_module
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.dom import SkyvernElement
from skyvern.webeye.utils.page import apply_secret_visual_mask_to_active_element


def _make_element(
    attributes: dict[str, str] | None = None,
    *,
    tag_name: str = "input",
) -> tuple[SkyvernElement, MagicMock]:
    locator = MagicMock()
    locator.get_attribute = AsyncMock(return_value=None)
    locator.element_handle = AsyncMock(return_value=MagicMock())
    element = SkyvernElement(
        locator=locator,
        frame=MagicMock(),
        static_element={
            "id": "AA1",
            "tagName": tag_name,
            "attributes": attributes or {},
        },
    )
    return element, locator


@pytest.mark.asyncio
async def test_direct_aria_autocomplete_list() -> None:
    element, _ = _make_element({"aria-autocomplete": "list"})
    assert await element.is_auto_completion_input() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["both", "inline"])
async def test_aria_autocomplete_both_inline_not_detected(value: str) -> None:
    """After partial revert of #9417, only 'list' triggers autocomplete."""
    element, _ = _make_element({"aria-autocomplete": value})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_role_combobox_not_detected() -> None:
    """After partial revert of #9417, role=combobox alone does not trigger."""
    element, _ = _make_element({"role": "combobox"})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_non_input_ignored() -> None:
    element, _ = _make_element({"aria-autocomplete": "list"}, tag_name="textarea")
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_plain_text_input_not_detected() -> None:
    element, _ = _make_element({"type": "text", "autocomplete": "off"})
    assert await element.is_auto_completion_input() is False


@pytest.mark.asyncio
async def test_autocomplete_class() -> None:
    element, _ = _make_element({"class": "my-autocomplete-input"})
    assert await element.is_auto_completion_input() is True


@pytest.mark.asyncio
async def test_data_x_bind_autocomplete() -> None:
    element, _ = _make_element({"data-x-bind": "someAutocomplete"})
    assert await element.is_auto_completion_input() is True


@pytest.mark.asyncio
async def test_secret_visual_mask_skips_password_input() -> None:
    element, locator = _make_element({"type": "password"})

    with patch("skyvern.webeye.utils.dom.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await element.apply_secret_visual_mask()

    evaluate.assert_not_awaited()
    locator.element_handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_visual_mask_script_targets_element_root() -> None:
    element, _ = _make_element({"type": "text"})

    with patch("skyvern.webeye.utils.dom.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await element.apply_secret_visual_mask()

    expression = evaluate.await_args.kwargs["expression"]
    assert "getRootNode" in expression
    assert "data-skyvern-secret-mask" in expression


def _active_element_handle(*, content_frame: object) -> MagicMock:
    handle = MagicMock()
    handle.as_element.return_value.content_frame = AsyncMock(return_value=content_frame)
    handle.dispose = AsyncMock()
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [1, 7])
async def test_active_element_secret_visual_mask_follows_focus_into_a_frame(depth: int) -> None:
    """With focus in a payment iframe, the page's active element is the <iframe> itself; masking it would blur
    the whole frame, and a run that works from screenshots could no longer see the card form. However deep the
    frame holding the field is nested, the mask belongs on that field."""
    field_frame = MagicMock()
    field_frame.evaluate_handle = AsyncMock(return_value=_active_element_handle(content_frame=None))
    frame = field_frame
    for _ in range(depth - 1):
        parent = MagicMock()
        parent.evaluate_handle = AsyncMock(return_value=_active_element_handle(content_frame=frame))
        frame = parent
    page = MagicMock()
    page.main_frame.evaluate_handle = AsyncMock(return_value=_active_element_handle(content_frame=frame))

    with patch("skyvern.webeye.utils.page.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await apply_secret_visual_mask_to_active_element(page)

    assert evaluate.await_args.kwargs["frame"] is field_frame
    assert evaluate.await_args.kwargs["expression"] is page_module._SECRET_VISUAL_MASK_ACTIVE_ELEMENT_SCRIPT


# The shipped scripts, run against a minimal DOM: which element ends up masked is decided in the page.
_MASK_DRIVER_JS = """
const scripts = %s;
class ShadowRoot { querySelector() { return null; } appendChild() {} }
globalThis.ShadowRoot = ShadowRoot;
const doc = { head: { appendChild() {} }, createElement: () => ({}), querySelector: () => null };
doc.documentElement = doc.head;
globalThis.document = doc;
const el = (tagName, { type, parent, root, editable } = {}) => ({
  tagName, parent, isContentEditable: Boolean(editable), ownerDocument: doc, attrs: type ? { type } : {},
  style: { props: {}, setProperty(name, value) { this.props[name] = value; } },
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  getRootNode() { return root || doc; },
  closest(tag) { for (let n = this; n; n = n.parent) if (n.tagName.toLowerCase() === tag) return n; return null; },
});
const state = (e) => ({ masked: e.attrs["data-skyvern-secret-mask"] === "true",
  textSecurity: e.style.props["-webkit-text-security"] ?? null, blur: e.style.filter ?? null });
const mask = eval("(" + scripts.mask + ")");
const active = eval("(" + scripts.active + ")");
const out = {};

const labelled = el("INPUT", { type: "text" });
const label = el("LABEL");
label.control = labelled;
mask(label);
out.label = { label: state(label), control: state(labelled) };

const wrapped = el("INPUT", { type: "text" });
const wrappingLabel = el("LABEL");
wrappingLabel.control = wrapped;
mask(el("SPAN", { parent: wrappingLabel }));
out.textInsideLabel = state(wrapped);

const shadow = new ShadowRoot();
const inner = el("INPUT", { type: "text", root: shadow });
shadow.activeElement = inner;
const host = el("CARD-FIELD");
host.shadowRoot = shadow;
doc.activeElement = host;
out.shadowHost = { returned: active(), host: state(host), inner: state(inner) };

for (const [name, focused] of [["iframe", el("IFRAME")], ["body", el("BODY")], ["password", el("INPUT", { type: "password" })]]) {
  doc.activeElement = focused;
  out[name] = { returned: active(), focused: state(focused) };
}
console.log(JSON.stringify(out));
"""

_UNMASKED = {"masked": False, "textSecurity": None, "blur": None}
_MASKED_FIELD = {"masked": True, "textSecurity": "disc", "blur": None}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_secret_visual_mask_scripts_mask_the_field_that_receives_the_text() -> None:
    scripts = {
        "mask": page_module.SECRET_VISUAL_MASK_SCRIPT,
        "active": page_module._SECRET_VISUAL_MASK_ACTIVE_ELEMENT_SCRIPT,
    }
    # Piped on stdin, not a file: CodeQL flags file writes of SECRET_* names as clear-text storage.
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-"],
        input=_MASK_DRIVER_JS % json.dumps(scripts),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    # A label, or text inside one, hands the write to its control; the label itself must not be blurred.
    assert out["label"] == {"label": _UNMASKED, "control": _MASKED_FIELD}
    assert out["textInsideLabel"] == _MASKED_FIELD
    # Focus inside an open shadow root leaves the host active; the field inside it is what gets masked.
    assert out["shadowHost"] == {"returned": True, "host": _UNMASKED, "inner": _MASKED_FIELD}
    # A frame element or an unfocused body is never blurred, and is reported as nothing masked.
    assert out["iframe"] == {"returned": False, "focused": _UNMASKED}
    assert out["body"] == {"returned": False, "focused": _UNMASKED}
    # A password field is already masked by the browser.
    assert out["password"] == {"returned": True, "focused": _UNMASKED}


@pytest.mark.asyncio
async def test_locator_secret_visual_mask_reaches_a_field_that_re_rendered() -> None:
    """A field that re-renders mid-evaluation fails the first attempt, and the write that follows reaches the new
    element, so the mask has to land on that element too. Neither attempt may hold the write for the full action
    timeout when the selector matches nothing."""
    masked: list[str] = []
    timeouts: list[float | None] = []
    outcomes = iter([RuntimeError("Element is not attached to the DOM"), None])

    async def evaluate(script: str, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        failure = next(outcomes)
        if failure is not None:
            raise failure
        masked.append(script)

    await page_module.apply_secret_visual_mask_to_locator(
        SimpleNamespace(wait_for=AsyncMock(), evaluate=evaluate), timeout=15000
    )

    assert masked == [page_module.SECRET_VISUAL_MASK_SCRIPT]
    assert all(timeout is not None and timeout <= 5000 for timeout in timeouts)


@pytest.mark.asyncio
@pytest.mark.parametrize(("masked", "logged"), [(False, True), (True, False)])
async def test_active_element_secret_visual_mask_logs_a_skip(masked: bool, logged: bool) -> None:
    """A mask that finds no field to land on must leave a trace, or a missed card mask cannot be counted."""
    page = MagicMock()
    page.main_frame.evaluate_handle = AsyncMock(return_value=_active_element_handle(content_frame=None))

    with (
        patch("skyvern.webeye.utils.page.SkyvernFrame.evaluate", new_callable=AsyncMock, return_value=masked),
        patch("skyvern.webeye.utils.page.LOG") as log,
    ):
        await apply_secret_visual_mask_to_active_element(page)

    assert log.info.called is logged


@pytest.mark.asyncio
@pytest.mark.parametrize(("workflow_run_id", "enabled"), [(None, False), ("wr_disabled", False)])
async def test_secret_visual_mask_helper_skips_inactive_runs(
    monkeypatch: pytest.MonkeyPatch,
    workflow_run_id: str | None,
    enabled: bool,
) -> None:
    element = SimpleNamespace(apply_secret_visual_mask=AsyncMock())
    manager = SimpleNamespace(mask_secrets_enabled_for_run=MagicMock(return_value=enabled))
    monkeypatch.setattr(handler_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(handler_module.settings, "ENABLE_SECRET_VISUAL_MASKING", True)

    await handler_module._apply_secret_visual_mask_if_needed(
        element,
        workflow_run_id=workflow_run_id,
        is_secret_value=True,
        is_totp_value=False,
    )

    element.apply_secret_visual_mask.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_visual_mask_helper_applies_for_opted_in_run(monkeypatch: pytest.MonkeyPatch) -> None:
    element = SimpleNamespace(apply_secret_visual_mask=AsyncMock())
    manager = SimpleNamespace(mask_secrets_enabled_for_run=MagicMock(return_value=True))
    monkeypatch.setattr(handler_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(handler_module.settings, "ENABLE_SECRET_VISUAL_MASKING", True)

    await handler_module._apply_secret_visual_mask_if_needed(
        element,
        workflow_run_id="wr_enabled",
        is_secret_value=True,
        is_totp_value=False,
    )

    element.apply_secret_visual_mask.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_secret_visual_mask_helper_skips_when_global_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    element = SimpleNamespace(apply_secret_visual_mask=AsyncMock())
    manager = SimpleNamespace(mask_secrets_enabled_for_run=MagicMock(return_value=True))
    monkeypatch.setattr(handler_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(handler_module.settings, "ENABLE_SECRET_VISUAL_MASKING", False)

    await handler_module._apply_secret_visual_mask_if_needed(
        element,
        workflow_run_id="wr_enabled",
        is_secret_value=True,
        is_totp_value=False,
    )

    element.apply_secret_visual_mask.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_element_secret_visual_mask_helper_skips_inactive_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        mask_secrets_enabled_for_run=MagicMock(return_value=False),
        get_secret_values_for_run=MagicMock(return_value={"secret-value"}),
    )
    apply_mask = AsyncMock()
    monkeypatch.setattr(handler_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(handler_module.settings, "ENABLE_SECRET_VISUAL_MASKING", True)
    monkeypatch.setattr(handler_module, "apply_secret_visual_mask_to_active_element", apply_mask)

    await handler_module._apply_active_element_secret_visual_mask_if_needed(MagicMock(), "secret-value", "wr_disabled")

    manager.get_secret_values_for_run.assert_not_called()
    apply_mask.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_element_secret_visual_mask_helper_uses_raw_values_for_opted_in_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        mask_secrets_enabled_for_run=MagicMock(return_value=True),
        get_secret_values_for_run=MagicMock(return_value={"secret-value"}),
    )
    apply_mask = AsyncMock()
    monkeypatch.setattr(handler_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(handler_module.settings, "ENABLE_SECRET_VISUAL_MASKING", True)
    monkeypatch.setattr(handler_module, "apply_secret_visual_mask_to_active_element", apply_mask)

    page = MagicMock()
    await handler_module._apply_active_element_secret_visual_mask_if_needed(page, "secret-value", "wr_enabled")

    manager.get_secret_values_for_run.assert_called_once_with(
        "wr_enabled",
        respect_artifact_redaction_flag=False,
    )
    apply_mask.assert_awaited_once_with(page)
