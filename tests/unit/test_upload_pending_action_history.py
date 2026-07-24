from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.services.action_service import get_action_history
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.handler import DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE, UPLOAD_PENDING_FOLLOWUP_MESSAGE
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess


class TestUploadFollowupActionHistory:
    def test_followup_message_field_exists(self):
        r = ActionResult(success=True)
        assert r.followup_message is None

    def test_followup_message_set_on_deferred_upload(self):
        r = ActionSuccess()
        r.upload_file_triggered = False
        r.followup_message = UPLOAD_PENDING_FOLLOWUP_MESSAGE
        assert r.success is True
        assert r.upload_file_triggered is False
        assert r.followup_message == UPLOAD_PENDING_FOLLOWUP_MESSAGE

    def test_followup_message_absent_on_successful_upload(self):
        r = ActionSuccess()
        r.upload_file_triggered = True
        assert r.followup_message is None

    def test_followup_message_in_str_representation(self):
        r = ActionSuccess()
        r.upload_file_triggered = False
        r.followup_message = UPLOAD_PENDING_FOLLOWUP_MESSAGE
        s = str(r)
        assert "followup_message=" in s

    def test_followup_message_in_model_dump(self):
        r = ActionSuccess()
        r.upload_file_triggered = False
        r.followup_message = UPLOAD_PENDING_FOLLOWUP_MESSAGE
        d = r.model_dump(
            exclude_none=True,
            include={"success", "upload_file_triggered", "followup_message"},
        )
        assert d["success"] is True
        assert d["upload_file_triggered"] is False
        assert d["followup_message"] == UPLOAD_PENDING_FOLLOWUP_MESSAGE

    def test_no_followup_message_in_dump_when_none(self):
        r = ActionSuccess()
        r.upload_file_triggered = True
        d = r.model_dump(
            exclude_none=True,
            include={"success", "upload_file_triggered", "followup_message"},
        )
        assert "followup_message" not in d

    def test_needs_followup_set_on_deferred(self):
        r = ActionSuccess()
        r.needs_followup = True
        r.followup_message = UPLOAD_PENDING_FOLLOWUP_MESSAGE
        d = r.model_dump(
            exclude_none=True,
            include={"success", "needs_followup", "followup_message"},
        )
        assert d["needs_followup"] is True
        assert d["followup_message"] == UPLOAD_PENDING_FOLLOWUP_MESSAGE

    def test_needs_followup_absent_by_default(self):
        r = ActionSuccess()
        d = r.model_dump(exclude_none=True)
        assert "needs_followup" not in d
        assert "followup_message" not in d


async def _download_action_history(result: ActionResult) -> dict:
    """Run the real get_action_history over a single download-intent action/result."""
    action = ClickAction(element_id="download-link", download=True)
    step = MagicMock()
    step.output = SimpleNamespace(actions_and_results=[(action, [result])])
    task = MagicMock(task_id="tsk_1", organization_id="o_1")
    with patch("skyvern.services.action_service.app") as app_mock:
        app_mock.DATABASE.tasks.get_task_steps = AsyncMock(return_value=[])
        history = await get_action_history(task, current_step=step)
    assert len(history) == 1
    return history[0]


class TestDownloadFollowupActionHistory:
    """The no-download feedback must survive action-history serialization for the next prompt."""

    @pytest.mark.asyncio
    async def test_no_download_success_result_carries_feedback_through_history(self) -> None:
        r = ActionSuccess()
        r.download_triggered = False
        r.needs_followup = True
        r.followup_message = DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE

        entry = await _download_action_history(r)

        assert entry["action"]["download"] is True
        assert entry["result"]["success"] is True
        assert entry["result"]["download_triggered"] is False
        assert entry["result"]["needs_followup"] is True
        assert entry["result"]["followup_message"] == DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE

    @pytest.mark.asyncio
    async def test_terminal_failure_result_omits_followup_from_history(self) -> None:
        # Negative control: a page-confirmed terminal user error yields an
        # ActionFailure with download_triggered=false and NO followup fields, so the
        # serialized history must not carry a contradictory "keep trying" signal.
        r = ActionFailure(Exception("data not downloadable"), download_triggered=False)

        entry = await _download_action_history(r)

        assert entry["result"]["success"] is False
        assert entry["result"]["download_triggered"] is False
        assert "needs_followup" not in entry["result"]
        assert "followup_message" not in entry["result"]

    @pytest.mark.asyncio
    async def test_successful_download_omits_followup_from_history(self) -> None:
        r = ActionSuccess()
        r.download_triggered = True

        entry = await _download_action_history(r)

        assert entry["result"]["download_triggered"] is True
        assert "needs_followup" not in entry["result"]
        assert "followup_message" not in entry["result"]
