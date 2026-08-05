"""Regression tests for autocomplete input detection.

Covers direct attribute detection in is_auto_completion_input().
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler as handler_module
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


@pytest.mark.asyncio
async def test_active_element_secret_visual_mask_script_skips_password_inputs() -> None:
    with patch("skyvern.webeye.utils.page.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await apply_secret_visual_mask_to_active_element(MagicMock())

    expression = evaluate.await_args.kwargs["expression"]
    assert "document.activeElement" in expression  # nosemgrep: incomplete-url-substring-sanitization
    assert 'toLowerCase() === "password"' in expression
    assert "getRootNode" in expression
    assert "data-skyvern-secret-mask" in expression


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
