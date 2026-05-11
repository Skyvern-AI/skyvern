"""Tests for PendingFileChooserListener and SkyvernContext.cleanup_pending_file_chooser."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from skyvern.forge.sdk.core.skyvern_context import (
    PendingFileChooserListener,
    SkyvernContext,
    _cleanup_outgoing_context,
)


def _make_mock_page() -> MagicMock:
    page = MagicMock()
    page.is_closed.return_value = False
    return page


def _make_pending(page: MagicMock | None = None, triggered: bool = False) -> PendingFileChooserListener:
    page = page or _make_mock_page()
    handler = MagicMock()
    return PendingFileChooserListener(page=page, file_paths=["/tmp/test.pdf"], handler=handler, triggered=triggered)


class TestPendingFileChooserListenerCleanup:
    def test_cleanup_removes_listener(self) -> None:
        page = _make_mock_page()
        pending = _make_pending(page=page)
        handler = pending.handler

        pending.cleanup()

        page.remove_listener.assert_called_once_with("filechooser", handler)
        assert pending.handler is None

    def test_cleanup_idempotent(self) -> None:
        pending = _make_pending()
        pending.cleanup()
        pending.cleanup()

        assert pending.handler is None

    def test_cleanup_handles_closed_page(self) -> None:
        page = _make_mock_page()
        page.remove_listener.side_effect = Exception("Target page closed")
        pending = _make_pending(page=page)

        pending.cleanup()

        assert pending.handler is None


class TestSkyvernContextCleanupPendingFileChooser:
    def test_cleanup_when_no_pending(self) -> None:
        ctx = SkyvernContext()
        ctx.cleanup_pending_file_chooser()
        assert ctx.pending_file_chooser is None

    def test_cleanup_unconsumed_logs_warning(self) -> None:
        ctx = SkyvernContext()
        ctx.pending_file_chooser = _make_pending(triggered=False)

        with patch("skyvern.forge.sdk.core.skyvern_context.LOG") as mock_log:
            ctx.cleanup_pending_file_chooser()
            mock_log.warning.assert_called_once()

        assert ctx.pending_file_chooser is None

    def test_cleanup_consumed_no_warning(self) -> None:
        ctx = SkyvernContext()
        ctx.pending_file_chooser = _make_pending(triggered=True)

        with patch("skyvern.forge.sdk.core.skyvern_context.LOG") as mock_log:
            ctx.cleanup_pending_file_chooser()
            mock_log.warning.assert_not_called()

        assert ctx.pending_file_chooser is None

    def test_cleanup_removes_listener_from_page(self) -> None:
        page = _make_mock_page()
        ctx = SkyvernContext()
        pending = _make_pending(page=page, triggered=True)
        handler = pending.handler
        ctx.pending_file_chooser = pending

        ctx.cleanup_pending_file_chooser()

        page.remove_listener.assert_called_once_with("filechooser", handler)

    def test_new_pending_replaces_old(self) -> None:
        page_a = _make_mock_page()
        page_b = _make_mock_page()
        ctx = SkyvernContext()

        old_pending = _make_pending(page=page_a)
        old_handler = old_pending.handler
        ctx.pending_file_chooser = old_pending

        ctx.cleanup_pending_file_chooser()
        page_a.remove_listener.assert_called_once_with("filechooser", old_handler)

        new_pending = _make_pending(page=page_b)
        ctx.pending_file_chooser = new_pending
        assert ctx.pending_file_chooser.page is page_b


class TestCleanupOutgoingContext:
    def test_none_context_is_safe(self) -> None:
        _cleanup_outgoing_context(None)

    def test_cleans_pending_file_chooser(self) -> None:
        ctx = SkyvernContext()
        pending = _make_pending(triggered=False)
        ctx.pending_file_chooser = pending

        _cleanup_outgoing_context(ctx)

        assert ctx.pending_file_chooser is None

    def test_flushes_feature_flags_and_pending(self) -> None:
        ctx = SkyvernContext()
        ctx.feature_flag_entries = {"FLAG_A": True}
        ctx.pending_file_chooser = _make_pending(triggered=False)

        with patch.object(ctx, "flush_feature_flags") as mock_flush:
            _cleanup_outgoing_context(ctx)
            mock_flush.assert_called_once()

        assert ctx.pending_file_chooser is None
