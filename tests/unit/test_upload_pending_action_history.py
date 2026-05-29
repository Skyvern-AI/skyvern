from skyvern.webeye.actions.handler import UPLOAD_PENDING_FOLLOWUP_MESSAGE
from skyvern.webeye.actions.responses import ActionResult, ActionSuccess


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
