"""Tests for dialog-handler recording into SkyvernContext."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye import dialog_handler


def _make_dialog(dialog_type: str, message: str, default_value: str = "") -> MagicMock:
    dialog = MagicMock()
    dialog.type = dialog_type
    dialog.message = message
    dialog.default_value = default_value
    dialog.accept = AsyncMock()
    dialog.dismiss = AsyncMock()
    return dialog


@pytest.fixture
def isolated_context() -> Generator[SkyvernContext, None, None]:
    ctx = SkyvernContext(
        organization_id="o_test",
        task_id="tsk_test",
        workflow_run_id="wr_test",
    )
    skyvern_context.set(ctx)
    try:
        yield ctx
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_alert_records_into_context_and_auto_accepts(isolated_context: SkyvernContext) -> None:
    dialog = _make_dialog("alert", "The value of '47' is invalid.")

    await dialog_handler._handle_dialog(dialog)

    dialog.accept.assert_awaited_once()
    assert isolated_context.recent_dialog_messages == [
        {"type": "alert", "message": "The value of '47' is invalid.", "count": 1}
    ]


@pytest.mark.asyncio
async def test_repeated_alerts_dedupe_with_count(isolated_context: SkyvernContext) -> None:
    dialog = _make_dialog("alert", "phone invalid")

    for _ in range(5):
        await dialog_handler._handle_dialog(dialog)

    assert len(isolated_context.recent_dialog_messages) == 1
    assert isolated_context.recent_dialog_messages[0]["count"] == 5


@pytest.mark.asyncio
async def test_no_context_does_not_raise() -> None:
    skyvern_context.reset()
    dialog = _make_dialog("alert", "something")
    await dialog_handler._handle_dialog(dialog)
    dialog.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_failure_does_not_break_dialog_acceptance(
    isolated_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated record failure")

    monkeypatch.setattr(SkyvernContext, "record_dialog_message", boom)
    dialog = _make_dialog("alert", "anything")

    await dialog_handler._handle_dialog(dialog)

    dialog.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_alert_dialogs_are_not_recorded(
    isolated_context: SkyvernContext,
) -> None:
    isolated_context.navigation_goal = "test"

    await dialog_handler._handle_dialog(_make_dialog("confirm", "Are you sure?"))
    await dialog_handler._handle_dialog(_make_dialog("prompt", "Enter your name:"))
    await dialog_handler._handle_dialog(_make_dialog("beforeunload", "Changes may not be saved."))

    assert isolated_context.recent_dialog_messages == []
