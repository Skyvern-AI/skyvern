"""Tests for ScopedXhrDownloadCapture — action-scoped XHR download listener."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from structlog.testing import capture_logs

from skyvern.webeye.actions.handler import ScopedXhrDownloadCapture


def _make_response(
    *,
    resource_type: str = "xhr",
    status: int = 200,
    content_type: str = "application/pdf",
    content_disposition: str = 'inline; filename="report.pdf"',
    body: bytes = b"%PDF-1.4 fake",
    content_length: str | None = None,
    url: str = "https://example.com/api/report",
    redirected_from: MagicMock | None = None,
) -> MagicMock:
    resp = AsyncMock()
    resp.status = status
    request_mock = MagicMock()
    request_mock.resource_type = resource_type
    request_mock.redirected_from = redirected_from
    resp.request = request_mock
    resp.url = url
    headers: dict[str, str] = {"content-type": content_type}
    if content_disposition:
        headers["content-disposition"] = content_disposition
    if content_length is not None:
        headers["content-length"] = content_length
    resp.headers = headers
    resp.body = AsyncMock(return_value=body)
    return resp


def _make_request(
    *,
    resource_type: str = "xhr",
    redirected_from: MagicMock | None = None,
    page: MagicMock | None = None,
) -> MagicMock:
    request = MagicMock()
    request.resource_type = resource_type
    request.redirected_from = redirected_from
    if page is not None:
        request.frame.page = page
    return request


def _make_page(*, cdp_active: bool = False) -> MagicMock:
    page = MagicMock()
    context = MagicMock()
    context._skyvern_cdp_download_active = cdp_active
    page.context = context
    return page


def _admit_response(capture: ScopedXhrDownloadCapture, response: MagicMock) -> None:
    capture.enable()
    capture._on_request(response.request)


class TestScopedXhrDownloadCapture:
    def test_enable_registers_listener_on_page_and_context(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        page.on.assert_any_call("response", capture._on_response_event)
        page.on.assert_any_call("request", capture._on_request)
        page.on.assert_any_call("requestfinished", capture._on_request_finished)
        page.on.assert_any_call("requestfailed", capture._on_request_finished)
        page.context.on.assert_called_once_with("page", capture._on_new_page)

    @pytest.mark.asyncio
    async def test_response_listener_wrapper_owns_capture_task(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path, timeout_seconds=1)
        response = _make_response()
        _admit_response(capture, response)

        listener = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "response")
        result = listener(response)

        assert result is None
        assert len(capture._response_tasks) == 1
        await capture.drain()
        assert capture._response_tasks == set()
        assert (tmp_path / "report.pdf").exists()

    def test_disable_removes_all_listeners(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        capture.disable()
        page.remove_listener.assert_any_call("response", capture._on_response_event)
        page.remove_listener.assert_any_call("request", capture._on_request)
        page.remove_listener.assert_any_call("requestfinished", capture._on_request_finished)
        page.remove_listener.assert_any_call("requestfailed", capture._on_request_finished)
        page.context.remove_listener.assert_called_once_with("page", capture._on_new_page)

    def test_tracks_request_lifecycle_and_removes_request_listeners(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        request = _make_request()

        capture.enable()
        capture._on_request(request)

        assert capture.has_in_flight_requests

        capture._on_request_finished(request)

        assert not capture.has_in_flight_requests
        capture.disable()
        assert page.remove_listener.call_count == 4

    def test_ignores_document_request_for_download_wait_extension(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))

        capture.enable()
        capture._on_request(_make_request(resource_type="document"))

        assert not capture.has_in_flight_requests

    def test_seal_ignores_requests_started_after_action_returns(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()

        capture.seal_in_flight_requests()
        capture._on_request(_make_request())

        assert not capture.has_in_flight_requests

    @pytest.mark.asyncio
    async def test_seal_admits_multi_hop_redirect_chain_and_captures_response(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        root_request = _make_request()
        first_redirect = _make_request(redirected_from=root_request)
        final_redirect = _make_request(redirected_from=first_redirect)
        response = _make_response(redirected_from=first_redirect)
        response.request = final_redirect

        capture.enable()
        capture._on_request(root_request)
        capture.seal_in_flight_requests()
        capture._on_request(first_redirect)
        capture._on_request(final_redirect)

        capture._on_request_finished(root_request)
        assert capture.has_in_flight_requests
        capture._on_request_finished(first_redirect)
        assert capture.has_in_flight_requests

        await capture._on_response(response)

        assert (tmp_path / "report.pdf").exists()
        capture._on_request_finished(final_redirect)
        assert not capture.has_in_flight_requests

    @pytest.mark.asyncio
    async def test_seal_rejects_unrelated_request_response(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response(redirected_from=None)
        capture.enable()
        capture.seal_in_flight_requests()

        capture._on_request(response.request)
        await capture._on_response(response)

        assert not capture.has_in_flight_requests
        response.body.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("resource_type", ["xhr", "fetch"])
    def test_seal_admits_first_root_request_from_child_page_observed_before_seal(self, resource_type: str) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        child_page = _make_page()
        capture.enable()
        capture._on_new_page(child_page)
        capture.seal_in_flight_requests()

        child_request = _make_request(resource_type=resource_type, page=child_page)
        capture._on_request(child_request)

        assert capture.has_in_flight_requests
        assert child_request in capture._admitted_requests

    def test_pre_seal_child_requests_do_not_consume_bootstrap_allowance(self) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        child_page = _make_page()
        pre_seal_requests = [
            _make_request(resource_type="xhr", page=child_page),
            _make_request(resource_type="fetch", page=child_page),
        ]
        first_post_seal_request = _make_request(page=child_page)
        second_post_seal_request = _make_request(page=child_page)
        capture.enable()
        capture._on_new_page(child_page)

        for request in pre_seal_requests:
            capture._on_request(request)
            assert request in capture._admitted_requests
            assert capture.has_in_flight_requests
            capture._on_request_finished(request)
            assert not capture.has_in_flight_requests

        capture.seal_in_flight_requests()
        capture._on_request(first_post_seal_request)

        assert first_post_seal_request in capture._admitted_requests
        assert capture.has_in_flight_requests

        capture._on_request_finished(first_post_seal_request)
        assert not capture.has_in_flight_requests

        capture._on_request(second_post_seal_request)

        assert second_post_seal_request not in capture._admitted_requests
        assert not capture.has_in_flight_requests

    def test_seal_admits_first_root_request_when_child_page_event_arrives_after_seal(self) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        child_page = _make_page()
        capture.enable()
        capture.seal_in_flight_requests()

        capture._on_new_page(child_page)
        child_request = _make_request(page=child_page)
        capture._on_request(child_request)

        assert capture.has_in_flight_requests
        assert child_request in capture._admitted_requests

    @pytest.mark.asyncio
    async def test_child_bootstrap_response_is_captured_and_extends_request_lifecycle(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        child_page = _make_page()
        response = _make_response()
        response.request.frame.page = child_page
        capture.enable()
        capture._on_new_page(child_page)
        capture.seal_in_flight_requests()

        capture._on_request(response.request)
        capture._on_response_event(response)
        await capture.drain()

        assert (tmp_path / "report.pdf").exists()
        assert capture.has_in_flight_requests
        capture._on_request_finished(response.request)
        assert not capture.has_in_flight_requests

    @pytest.mark.asyncio
    async def test_child_bootstrap_allowance_rejects_second_unrelated_root(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        child_page = _make_page()
        first_request = _make_request(page=child_page)
        second_response = _make_response()
        second_response.request.frame.page = child_page
        capture.enable()
        capture._on_new_page(child_page)
        capture.seal_in_flight_requests()

        capture._on_request(first_request)
        capture._on_request_finished(first_request)
        capture._on_request(second_response.request)
        await capture._on_response(second_response)

        assert not capture.has_in_flight_requests
        assert second_response.request not in capture._admitted_requests
        second_response.body.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    def test_child_bootstrap_admission_is_inherited_by_redirect_chain(self) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        child_page = _make_page()
        root_request = _make_request(page=child_page)
        first_redirect = _make_request(redirected_from=root_request)
        final_redirect = _make_request(redirected_from=first_redirect)
        capture.enable()
        capture._on_new_page(child_page)
        capture.seal_in_flight_requests()

        capture._on_request(root_request)
        capture._on_request(first_redirect)
        capture._on_request(final_redirect)

        assert {root_request, first_redirect, final_redirect} <= capture._admitted_requests
        assert {root_request, first_redirect, final_redirect} <= capture._in_flight_requests

    def test_request_page_lookup_failure_after_seal_fails_closed(self) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        request = _make_request()
        type(request).frame = PropertyMock(side_effect=RuntimeError("frame is unavailable"))
        capture.enable()
        capture.seal_in_flight_requests()

        capture._on_request(request)

        assert not capture.has_in_flight_requests

    def test_disable_clears_in_flight_request_state(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        capture._on_request(_make_request())

        capture.disable()

        assert not capture.has_in_flight_requests

    def test_disable_clears_child_page_bootstrap_state(self) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), Path("/tmp/downloads"))
        child_page = _make_page()
        capture.enable()
        capture._on_new_page(child_page)

        capture.disable()

        assert capture._child_pages_with_bootstrap_allowance == set()

    def test_cdp_interceptor_attaches_response_listener_for_passive_observation(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()

        page.on.assert_any_call("request", capture._on_request)
        page.on.assert_any_call("requestfinished", capture._on_request_finished)
        page.on.assert_any_call("requestfailed", capture._on_request_finished)
        # Passive status observation is always on, so the response listener attaches even on the CDP
        # lane; body capture stays gated off there.
        page.on.assert_any_call("response", capture._on_response_event)
        assert capture._capture_responses is False
        assert capture._active

    def test_cdp_interceptor_detaches_response_listener_symmetrically(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        capture.disable()

        page.remove_listener.assert_any_call("response", capture._on_response_event)

    def test_cdp_interceptor_response_event_schedules_no_body_capture_task(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        response = _make_response()
        _admit_response(capture, response)

        capture._on_response_event(response)

        assert capture._response_tasks == set()

    def test_enable_uses_current_cdp_interceptor_state(self) -> None:
        page = _make_page(cdp_active=False)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        page.context._skyvern_cdp_download_active = True

        capture.enable()

        assert capture._capture_responses is False
        # Response listener still attaches on the CDP lane for passive status observation.
        page.on.assert_any_call("response", capture._on_response_event)
        capture.disable()
        page.remove_listener.assert_any_call("response", capture._on_response_event)

    def test_disable_noop_when_not_enabled(self) -> None:
        page = _make_page(cdp_active=True)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.disable()
        page.remove_listener.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_xhr_inline_pdf_to_download_dir(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response()
        _admit_response(capture, response)

        await capture._on_response(response)

        saved = tmp_path / "report.pdf"
        assert saved.exists()
        assert saved.read_bytes() == b"%PDF-1.4 fake"

    @pytest.mark.asyncio
    async def test_saves_xhr_attachment_pdf(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition='attachment; filename="invoice.pdf"')
        _admit_response(capture, response)

        await capture._on_response(response)

        assert (tmp_path / "invoice.pdf").exists()

    @pytest.mark.asyncio
    async def test_ignores_non_xhr_response(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(resource_type="document")

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_json_response(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_type="application/json", content_disposition="")
        _admit_response(capture, response)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_xhr_pdf_without_filename(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition="inline")
        _admit_response(capture, response)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_ignores_error_responses(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(status=403)
        _admit_response(capture, response)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_deduplicates_same_filename(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response1 = _make_response(body=b"first")
        response2 = _make_response(body=b"second")
        _admit_response(capture, response1)
        capture._on_request(response2.request)

        await capture._on_response(response1)
        await capture._on_response(response2)

        saved = tmp_path / "report.pdf"
        assert saved.read_bytes() == b"first"

    @pytest.mark.asyncio
    async def test_sanitizes_path_traversal(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_disposition='inline; filename="../../etc/evil.pdf"')
        _admit_response(capture, response)

        await capture._on_response(response)

        assert not (tmp_path.parent.parent / "etc" / "evil.pdf").exists()
        assert (tmp_path / "evil.pdf").exists()

    @pytest.mark.asyncio
    async def test_skips_oversized_content_length(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(content_length="200000000")
        _admit_response(capture, response)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    def test_new_page_gets_listener(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        new_page = MagicMock()

        capture.enable()
        capture._on_new_page(new_page)

        new_page.on.assert_any_call("response", capture._on_response_event)
        new_page.on.assert_any_call("request", capture._on_request)
        new_page.on.assert_any_call("requestfinished", capture._on_request_finished)
        new_page.on.assert_any_call("requestfailed", capture._on_request_finished)
        assert new_page in capture._extra_pages

    def test_disable_cleans_up_extra_pages(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        new_page = MagicMock()

        capture.enable()
        capture._on_new_page(new_page)
        capture.disable()

        assert new_page.remove_listener.call_count == 4
        assert capture._extra_pages == []
        assert not capture._active

    @pytest.mark.asyncio
    async def test_drain_waits_for_inflight_write(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)

        body_gate = asyncio.Event()

        response = _make_response()
        _admit_response(capture, response)

        async def slow_body() -> bytes:
            await body_gate.wait()
            return b"%PDF-1.4 fake"

        response.body = slow_body

        capture._on_response_event(response)
        await asyncio.sleep(0)

        assert not (tmp_path / "report.pdf").exists()
        assert not capture._drained.is_set()

        body_gate.set()
        await capture.drain()

        assert (tmp_path / "report.pdf").exists()

    @pytest.mark.asyncio
    async def test_drain_noop_when_no_writes(self, tmp_path: Path) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        await capture.drain()

    @pytest.mark.asyncio
    async def test_saves_xhr_generic_binary_with_large_body(self, tmp_path: Path) -> None:
        """Production shape: XHR with application/*, large body, no Content-Disposition,
        filename extracted from URL path."""
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(
            content_type="application/*",
            content_disposition="",
            content_length="46681129",
            body=b"fake-excel-bytes" * 1024,
            url="https://example.com/report/General.xlsx",
        )
        _admit_response(capture, response)

        await capture._on_response(response)

        saved = tmp_path / "General.xlsx"
        assert saved.exists()
        assert saved.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_skips_xhr_generic_binary_small_body(self, tmp_path: Path) -> None:
        """XHR with application/* and very small Content-Length (6) should NOT save."""
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(
            content_type="application/*",
            content_disposition="",
            content_length="6",
            body=b"empty",
            url="https://example.com/report/General.xlsx",
        )
        _admit_response(capture, response)

        await capture._on_response(response)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_saves_xhr_generic_binary_inline_no_filename_fallsback_to_url(self, tmp_path: Path) -> None:
        """XHR with application/*, inline Content-Disposition (no filename), and URL fallback."""
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(
            content_type="application/*",
            content_disposition="inline",
            content_length="99999999",
            body=b"spreadsheet-data",
            url="https://example.com/report/General.xlsx",
        )
        _admit_response(capture, response)

        await capture._on_response(response)

        saved = tmp_path / "General.xlsx"
        assert saved.exists()
        assert saved.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_saves_xhr_generic_binary_attachment_no_filename_fallsback_to_url(self, tmp_path: Path) -> None:
        """XHR with application/*, attachment header but no filename, and URL fallback."""
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, tmp_path)
        response = _make_response(
            content_type="application/*",
            content_disposition="attachment",
            content_length="99999999",
            body=b"spreadsheet-data",
            url="https://example.com/report/General.xlsx",
        )
        _admit_response(capture, response)

        await capture._on_response(response)

        saved = tmp_path / "General.xlsx"
        assert saved.exists()
        assert saved.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_captures_admitted_response_after_request_finishes(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response()
        _admit_response(capture, response)

        capture._on_request_finished(response.request)
        await capture._on_response(response)

        assert (tmp_path / "report.pdf").exists()

    @pytest.mark.asyncio
    async def test_rejects_response_for_request_not_admitted_during_action(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response()
        capture.enable()

        await capture._on_response(response)

        response.body.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_drain_budget_cancels_and_awaits_never_resolving_body_without_late_write(
        self, tmp_path: Path
    ) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        capture = ScopedXhrDownloadCapture(_make_page(), staging_dir)
        response = _make_response(url="https://example.com/customer-secret", body=b"private-response-text")
        body_gate = asyncio.Event()

        body_cancelled = asyncio.Event()

        async def never_resolving_body() -> bytes:
            try:
                await body_gate.wait()
                return b"private-response-text"
            finally:
                body_cancelled.set()

        response.body = AsyncMock(side_effect=never_resolving_body)
        _admit_response(capture, response)
        capture._on_response_event(response)
        await asyncio.sleep(0)

        with capture_logs() as logs:
            drained = await asyncio.wait_for(capture.drain(timeout_seconds=0.01), timeout=0.5)

        assert not drained
        assert body_cancelled.is_set()
        assert capture._response_tasks == set()
        assert capture._in_flight == 0
        assert capture._drained.is_set()
        assert any(log.get("event") == "Timed out waiting for XHR download response capture drainage" for log in logs)
        assert "customer-secret" not in repr(logs)
        assert "private-response-text" not in repr(logs)
        staging_dir.rmdir()
        body_gate.set()
        await asyncio.sleep(0)
        assert not staging_dir.exists()

    @pytest.mark.asyncio
    async def test_zero_budget_drain_cancels_and_awaits_pending_body_task(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response()
        body_started = asyncio.Event()
        body_cancelled = asyncio.Event()

        async def pending_body() -> bytes:
            body_started.set()
            try:
                await asyncio.Event().wait()
                return b"%PDF-1.4 unreachable"
            finally:
                body_cancelled.set()

        response.body = AsyncMock(side_effect=pending_body)
        _admit_response(capture, response)
        capture._on_response_event(response)
        await body_started.wait()

        assert not await capture.drain(timeout_seconds=0)
        assert body_cancelled.is_set()
        assert capture._response_tasks == set()
        assert capture._in_flight == 0
        assert capture._drained.is_set()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_slow_body_inside_download_budget_is_captured(self, tmp_path: Path) -> None:
        scaled_second = 0.01
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path, timeout_seconds=10 * scaled_second)
        response = _make_response()

        async def slow_body() -> bytes:
            await asyncio.sleep(5 * scaled_second)
            return b"%PDF-1.4 slow"

        response.body = AsyncMock(side_effect=slow_body)
        _admit_response(capture, response)
        capture._on_response_event(response)

        assert await capture.drain()
        assert (tmp_path / "report.pdf").read_bytes() == b"%PDF-1.4 slow"

    @pytest.mark.asyncio
    async def test_response_callback_propagates_cancellation_and_releases_in_flight(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response()
        body_gate = asyncio.Event()

        async def never_resolving_body() -> bytes:
            await body_gate.wait()
            return b"%PDF-1.4 fake"

        response.body = AsyncMock(side_effect=never_resolving_body)
        _admit_response(capture, response)
        capture._on_response_event(response)
        await asyncio.sleep(0)
        response_task = next(iter(capture._response_tasks))

        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task
        await asyncio.sleep(0)

        assert capture._response_tasks == set()
        assert capture._in_flight == 0
        assert capture._drained.is_set()

    @pytest.mark.asyncio
    async def test_drain_propagates_cancellation(self, tmp_path: Path) -> None:
        capture = ScopedXhrDownloadCapture(_make_page(), tmp_path)
        response = _make_response()
        body_gate = asyncio.Event()

        body_cancelled = asyncio.Event()

        async def never_resolving_body() -> bytes:
            try:
                await body_gate.wait()
                return b"%PDF-1.4 fake"
            finally:
                body_cancelled.set()

        response.body = AsyncMock(side_effect=never_resolving_body)
        _admit_response(capture, response)
        capture._on_response_event(response)
        await asyncio.sleep(0)
        drain_task = asyncio.create_task(capture.drain(timeout_seconds=10))
        await asyncio.sleep(0)

        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert body_cancelled.is_set()
        assert capture._response_tasks == set()
        assert capture._drained.is_set()


_ADMITTED_URL = "https://synthetic.test/authorized/statement"
_ADMITTED_HOST_CANARY = "synthetic.test"


class TestScopedXhrDownloadFailureStatusObserver:
    def _admitted_capture(
        self, *, cdp_active: bool = False, url: str = _ADMITTED_URL, status: int = 500
    ) -> tuple[ScopedXhrDownloadCapture, MagicMock]:
        page = _make_page(cdp_active=cdp_active)
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        response = _make_response(url=url, status=status, content_type="application/json", content_disposition="")
        _admit_response(capture, response)
        return capture, response

    def test_observes_5xx_for_admitted_request(self) -> None:
        capture, response = self._admitted_capture(status=500)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status == 500

    def test_observes_5xx_for_any_admitted_url(self) -> None:
        # Genericity: no site/URL filter — any admitted 5xx is recorded regardless of its URL.
        capture, response = self._admitted_capture(url="https://unrelated.example/api/export", status=503)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status == 503

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_observes_safe_5xx_variants(self, status: int) -> None:
        capture, response = self._admitted_capture(status=status)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status == status

    @pytest.mark.parametrize("status", [501, 505, 507])
    def test_does_not_observe_out_of_contract_5xx(self, status: int) -> None:
        capture, response = self._admitted_capture(status=status)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status is None

    def test_observes_5xx_on_cdp_active_interceptor_lane(self) -> None:
        capture, response = self._admitted_capture(cdp_active=True, status=503)
        # Drive the real response event: on the interceptor lane it schedules no body-capture task,
        # so observation is proven to run through the same seam that production CDP lanes hit.
        capture._on_response_event(response)
        assert capture.observed_download_failure_status == 503
        assert capture._response_tasks == set()

    def test_does_not_observe_unadmitted_5xx(self) -> None:
        page = _make_page()
        capture = ScopedXhrDownloadCapture(page, Path("/tmp/downloads"))
        capture.enable()
        # Response arrives without the request ever being admitted (e.g. before the click window).
        response = _make_response(
            url=_ADMITTED_URL, status=500, content_type="application/json", content_disposition=""
        )
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status is None

    def test_does_not_observe_4xx(self) -> None:
        capture, response = self._admitted_capture(status=404)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status is None

    def test_does_not_observe_2xx(self) -> None:
        capture, response = self._admitted_capture(status=200)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status is None

    def test_only_integer_status_retained_no_url_or_host(self) -> None:
        capture, response = self._admitted_capture(status=500)
        capture._observe_download_failure_status(response)
        assert capture.observed_download_failure_status == 500
        # Privacy canary: the observed URL/host must never be persisted on the capture.
        serialized = repr(vars(capture))
        assert _ADMITTED_URL not in serialized
        assert _ADMITTED_HOST_CANARY not in serialized
