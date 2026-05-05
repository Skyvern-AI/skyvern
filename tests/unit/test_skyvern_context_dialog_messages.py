"""Tests for the dialog-message buffer on SkyvernContext."""

from skyvern.forge.sdk.core.skyvern_context import (
    MAX_DIALOG_MESSAGE_CHARS,
    MAX_RECENT_DIALOG_MESSAGES,
    SkyvernContext,
)


class TestRecordDialogMessage:
    def test_appends_first_message(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "phone invalid")
        assert ctx.recent_dialog_messages == [{"type": "alert", "message": "phone invalid", "count": 1}]

    def test_dedupes_repeated_message_by_bumping_count(self) -> None:
        ctx = SkyvernContext()
        for _ in range(2062):
            ctx.record_dialog_message("alert", "phone invalid")
        assert len(ctx.recent_dialog_messages) == 1
        assert ctx.recent_dialog_messages[0]["count"] == 2062

    def test_distinct_messages_kept_separately_with_independent_counts(self) -> None:
        ctx = SkyvernContext()
        for _ in range(3):
            ctx.record_dialog_message("alert", "phone invalid")
            ctx.record_dialog_message("alert", "name invalid")
        assert len(ctx.recent_dialog_messages) == 2
        assert {(e["message"], e["count"]) for e in ctx.recent_dialog_messages} == {
            ("phone invalid", 3),
            ("name invalid", 3),
        }

    def test_distinguishes_by_dialog_type(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "are you sure?")
        ctx.record_dialog_message("confirm", "are you sure?")
        assert len(ctx.recent_dialog_messages) == 2

    def test_caps_at_max_with_fifo_eviction(self) -> None:
        ctx = SkyvernContext()
        for i in range(MAX_RECENT_DIALOG_MESSAGES + 3):
            ctx.record_dialog_message("alert", f"msg-{i}")
        assert len(ctx.recent_dialog_messages) == MAX_RECENT_DIALOG_MESSAGES
        assert ctx.recent_dialog_messages[0]["message"] == "msg-3"
        assert ctx.recent_dialog_messages[-1]["message"] == f"msg-{MAX_RECENT_DIALOG_MESSAGES + 2}"

    def test_empty_message_is_ignored(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "")
        assert ctx.recent_dialog_messages == []

    def test_long_messages_are_truncated_with_ellipsis(self) -> None:
        ctx = SkyvernContext()
        long_msg = "x" * (MAX_DIALOG_MESSAGE_CHARS + 200)
        ctx.record_dialog_message("alert", long_msg)
        stored = ctx.recent_dialog_messages[0]["message"]
        assert len(stored) == MAX_DIALOG_MESSAGE_CHARS + 1
        assert stored.endswith("…")


class TestFormatAndClearRecentDialogMessages:
    def test_format_returns_text_without_clearing(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "phone invalid")
        rendered = ctx.format_recent_dialog_messages()
        assert rendered == "[alert] phone invalid"
        assert len(ctx.recent_dialog_messages) == 1

    def test_format_can_be_called_repeatedly(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "msg")
        a = ctx.format_recent_dialog_messages()
        b = ctx.format_recent_dialog_messages()
        assert a == b == "[alert] msg"

    def test_format_returns_none_when_empty(self) -> None:
        assert SkyvernContext().format_recent_dialog_messages() is None

    def test_format_renders_count_suffix_when_deduped(self) -> None:
        ctx = SkyvernContext()
        for _ in range(2062):
            ctx.record_dialog_message("alert", "phone invalid")
        assert ctx.format_recent_dialog_messages() == "[alert (x2062)] phone invalid"

    def test_format_renders_multiple_messages_in_insertion_order(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "first")
        ctx.record_dialog_message("alert", "second")
        ctx.record_dialog_message("alert", "first")
        assert ctx.format_recent_dialog_messages() == "[alert (x2)] first\n[alert] second"

    def test_clear_empties_buffer(self) -> None:
        ctx = SkyvernContext()
        ctx.record_dialog_message("alert", "a")
        ctx.record_dialog_message("alert", "b")
        ctx.clear_recent_dialog_messages()
        assert ctx.recent_dialog_messages == []
