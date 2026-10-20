"""Tests for PrintPageBlock: surfacing the generated PDF in ``downloaded_files`` (SKY-9416) and
recovering the print when the renderer crashes part-way through it (SKY-17342)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import MissingBrowserStatePage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import PrintPageBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id=f"op_{key}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.fixture
def _isolated_download_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH",
        str(download_root),
    )
    return str(download_root)


def _make_page_pdf_mock() -> AsyncMock:
    page = SimpleNamespace(url="https://example.com/listing")
    page.pdf = AsyncMock(return_value=b"%PDF-1.4 test bytes")
    return page


PRINTING_FAILED = "Page.pdf: Protocol error (Page.printToPDF): Printing failed"


def _browser_state_stub(working_page: object, *, replacements: object = None, open_pages: list | None = None):
    """A browser state whose page selection can be scripted.

    ``open_pages`` is what was open when the print started; the block rejects any replacement drawn
    from that set, so a test that wants its replacement accepted must leave it out.
    """
    state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=working_page),
        list_valid_pages=AsyncMock(return_value=open_pages if open_pages is not None else [working_page]),
    )
    if replacements is not None:
        state.must_get_working_page = (
            AsyncMock(side_effect=replacements)
            if isinstance(replacements, list)
            else AsyncMock(return_value=replacements)
        )
    return state


def _storage_stub() -> SimpleNamespace:
    return SimpleNamespace(
        STORAGE=SimpleNamespace(
            get_downloaded_file_signature_aliases=lambda _: [],
            save_downloaded_files=AsyncMock(),
            get_current_attempt_downloaded_files=AsyncMock(return_value=[]),
        ),
    )


async def _identity_record(self, workflow_run_context, workflow_run_id, value):
    self._captured_output = value


async def _identity_build_block_result(self, **kwargs):
    return SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_print_page_block_includes_downloaded_files_in_output(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """The PDF the block generates should appear in the block output's
    ``downloaded_files`` / ``downloaded_file_urls`` / ``downloaded_file_artifact_ids``
    so the UI can render the printed page like a regular download."""
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
        )
    )

    # Set modified_at so the assertion actually exercises FileInfo.model_dump() with a
    # non-None datetime — production reads this from the artifact row's created_at.
    file_info = FileInfo(
        url="https://api.example.com/v1/artifacts/a_dl_1/content?artifact_name=page.pdf",
        filename="page.pdf",
        checksum="deadbeef",
        artifact_id="a_dl_1",
        modified_at=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
    )

    save_mock = AsyncMock()
    # capture_block_download_baseline calls get_current_attempt_downloaded_files first (empty here),
    # then the helper calls it again post-PDF.
    get_mock = AsyncMock(side_effect=[[], [file_info]])

    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            get_downloaded_file_signature_aliases=lambda _: [],
            save_downloaded_files=save_mock,
            get_current_attempt_downloaded_files=get_mock,
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    block = PrintPageBlock(
        label="print",
        output_parameter=_output_parameter("print_out"),
    )

    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )

    page = _make_page_pdf_mock()
    browser_state = _browser_state_stub(page)
    monkeypatch.setattr(
        PrintPageBlock,
        "get_or_create_browser_state",
        AsyncMock(return_value=browser_state),
    )

    upload_mock = AsyncMock(return_value=("s3://artifacts/wr_1/receipt.pdf", "https://example.com/receipt.pdf"))
    monkeypatch.setattr(PrintPageBlock, "_upload_pdf_artifact", upload_mock)

    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
    )

    assert result.success is True
    save_mock.assert_awaited_once_with(organization_id="o_1", run_id="wr_1")
    assert get_mock.await_count == 2

    output = block._captured_output
    assert output["filename"].endswith(".pdf")
    assert output["downloaded_files"] == [file_info.model_dump()]
    assert output["downloaded_file_urls"] == [file_info.url]
    assert output["downloaded_file_artifact_ids"] == ["a_dl_1"]


@pytest.mark.asyncio
async def test_print_page_block_filters_downloads_to_current_loop_iteration(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """When inside a ForLoopBlock iteration, only the PDF generated this iteration
    should land in the block output — earlier iterations' files must be filtered out."""
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
            loop_internal_state={
                "downloaded_file_signatures_before_iteration": [
                    ["prev.pdf", "abc", "https://api.example.com/v1/artifacts/a_prev/content"],
                ],
            },
        )
    )

    prev_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_prev/content?artifact_name=prev.pdf",
        filename="prev.pdf",
        checksum="abc",
        artifact_id="a_prev",
    )
    new_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_new/content?artifact_name=page.pdf",
        filename="page.pdf",
        checksum="def",
        artifact_id="a_new",
    )

    # Baseline capture (block start) sees only the earlier iteration's file; the
    # post-PDF read sees both. The block must scope its output to just the new one.
    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            get_downloaded_file_signature_aliases=lambda _: [],
            save_downloaded_files=AsyncMock(),
            get_current_attempt_downloaded_files=AsyncMock(side_effect=[[prev_file], [prev_file, new_file]]),
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    block = PrintPageBlock(
        label="print",
        output_parameter=_output_parameter("print_out"),
    )
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    page = _make_page_pdf_mock()
    monkeypatch.setattr(
        PrintPageBlock,
        "get_or_create_browser_state",
        AsyncMock(return_value=_browser_state_stub(page)),
    )
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    await block.execute(
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
    )

    output = block._captured_output
    assert [fi["filename"] for fi in output["downloaded_files"]] == ["page.pdf"]
    assert output["downloaded_file_artifact_ids"] == ["a_new"]


@pytest.mark.asyncio
async def test_print_page_block_tolerates_save_failure(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """If ``save_downloaded_files`` raises, the block must still succeed —
    workflow finalization will retry the upload, and the artifact-only output
    fields (``filename`` / ``artifact_uri`` / ``artifact_url``) are still useful."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))

    save_mock = AsyncMock(side_effect=RuntimeError("S3 down"))
    # Baseline-capture awaits get_current_attempt_downloaded_files once before save runs.
    get_mock = AsyncMock(return_value=[])

    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            get_downloaded_file_signature_aliases=lambda _: [],
            save_downloaded_files=save_mock,
            get_current_attempt_downloaded_files=get_mock,
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    page = _make_page_pdf_mock()
    monkeypatch.setattr(
        PrintPageBlock,
        "get_or_create_browser_state",
        AsyncMock(return_value=_browser_state_stub(page)),
    )
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
    )

    assert result.success is True
    # Baseline call happened, but the post-save fetch did not (save failed first).
    assert get_mock.await_count == 1
    output = block._captured_output
    assert output["downloaded_files"] == []
    assert output["downloaded_file_urls"] == []
    assert output["downloaded_file_artifact_ids"] == []
    assert output["artifact_uri"] == "s3://artifacts/wr_1/page.pdf"


@pytest.mark.asyncio
async def test_print_page_block_excludes_files_downloaded_by_prior_block(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A PrintPageBlock placed after another block (e.g. TaskBlock) whose downloads
    already landed in the run's download directory must filter those out — its own
    output should only carry the PDF it just generated. Captured by mirroring the
    `capture_block_download_baseline` call TaskBlock/TaskV2Block use."""
    skyvern_context.set(
        SkyvernContext(
            organization_id="o_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
        )
    )

    prior_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_prior/content?artifact_name=prior.pdf",
        filename="prior.pdf",
        checksum="abc",
        artifact_id="a_prior",
    )
    new_file = FileInfo(
        url="https://api.example.com/v1/artifacts/a_new/content?artifact_name=page.pdf",
        filename="page.pdf",
        checksum="def",
        artifact_id="a_new",
    )

    # First call: baseline capture sees the prior block's file.
    # Second call: post-PDF read sees both prior + new.
    get_mock = AsyncMock(side_effect=[[prior_file], [prior_file, new_file]])
    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            get_downloaded_file_signature_aliases=lambda _: [],
            save_downloaded_files=AsyncMock(),
            get_current_attempt_downloaded_files=get_mock,
        ),
    )
    monkeypatch.setattr(block_module, "app", fake_app)

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    page = _make_page_pdf_mock()
    monkeypatch.setattr(
        PrintPageBlock,
        "get_or_create_browser_state",
        AsyncMock(return_value=_browser_state_stub(page)),
    )
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    await block.execute(
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
    )

    output = block._captured_output
    assert [fi["filename"] for fi in output["downloaded_files"]] == ["page.pdf"]
    assert output["downloaded_file_urls"] == [new_file.url]
    assert output["downloaded_file_artifact_ids"] == ["a_new"]


@pytest.mark.asyncio
async def test_print_page_block_retries_on_the_replacement_tab_after_a_renderer_crash(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Chromium takes the renderer down mid-print on some heavy pages, which surfaces as
    ``Printing failed`` (SKY-17342). The browser retires the crashed tab and reopens a replacement
    on the same URL, so the block must print there instead of failing the run."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_POLL_SECONDS", 0)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))
    replacement_page = SimpleNamespace(url="https://example.com/listing")
    replacement_page.pdf = AsyncMock(return_value=b"%PDF-1.4 reprinted")

    # The crash event and the print's rejection race, so the first look still finds the crashed tab.
    browser_state = _browser_state_stub(crashed_page, replacements=[crashed_page, replacement_page])

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    upload_mock = AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf"))
    monkeypatch.setattr(PrintPageBlock, "_upload_pdf_artifact", upload_mock)
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is True
    replacement_page.pdf.assert_awaited_once()
    assert upload_mock.await_args.kwargs["pdf_bytes"] == b"%PDF-1.4 reprinted"


@pytest.mark.asyncio
async def test_print_page_block_keeps_polling_when_recovery_raises_mid_flight(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Recovery can raise while it is still in flight -- the replacement tab is not open yet, so
    there is briefly no page to hand back. That is "not ready yet", not a terminal answer, so the
    block must keep polling instead of giving up on the first exception."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_POLL_SECONDS", 0)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))
    replacement_page = SimpleNamespace(url="https://example.com/listing")
    replacement_page.pdf = AsyncMock(return_value=b"%PDF-1.4 reprinted")

    browser_state = _browser_state_stub(crashed_page, replacements=[MissingBrowserStatePage(), replacement_page])

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is True
    replacement_page.pdf.assert_awaited_once()


@pytest.mark.asyncio
async def test_print_page_block_does_not_recover_from_a_non_crash_error(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Only a crash leaves a dead tab worth replacing. If the crash match were ever broadened or
    dropped, every other PDF error would spend the recovery budget before failing the same way."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())

    page = SimpleNamespace(url="https://example.com/listing")
    page.pdf = AsyncMock(side_effect=PlaywrightError("Page.pdf: some other failure"))
    must_get_working_page = AsyncMock(return_value=page)
    browser_state = _browser_state_stub(page)
    browser_state.must_get_working_page = must_get_working_page

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is False
    assert "some other failure" in result.failure_reason
    assert page.pdf.await_count == 1
    must_get_working_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_print_page_block_gives_up_when_recovery_never_completes(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """Recovery's URL restore retries navigation for minutes, and it is awaited inside the poll.
    Without a bound the block would wait all of it -- on every attempt -- before failing."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_TIMEOUT_SECONDS", 0.01)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))

    async def _never_returns() -> None:
        await asyncio.sleep(30)

    browser_state = _browser_state_stub(crashed_page)
    browser_state.must_get_working_page = _never_returns

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await asyncio.wait_for(
        block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1"),
        timeout=10,
    )

    assert result.success is False
    assert "Printing failed" in result.failure_reason


@pytest.mark.asyncio
async def test_print_page_block_fails_rather_than_printing_a_surviving_sibling_tab(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """When another tab is still alive, the browser closes the crashed tab without reopening
    anything, and page selection hands over that sibling -- on a different URL. Printing it would
    put a PDF of the wrong page into ``downloaded_files`` and report success."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_POLL_SECONDS", 0)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))
    sibling_page = SimpleNamespace(url="https://example.com/some-other-tab")
    sibling_page.pdf = AsyncMock(return_value=b"%PDF-1.4 wrong page")

    browser_state = _browser_state_stub(
        crashed_page, replacements=sibling_page, open_pages=[crashed_page, sibling_page]
    )

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is False
    assert "Printing failed" in result.failure_reason
    sibling_page.pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_print_page_block_fails_rather_than_printing_a_sibling_tab_on_the_same_url(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A surviving sibling can be on the same URL as the crashed tab -- the run may have opened the
    listing twice. Its in-page state is whatever that tab was left showing, so a URL match alone
    does not make it the page the print was asked for; only being a page that did not exist before
    the print does."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_POLL_SECONDS", 0)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))
    twin_page = SimpleNamespace(url="https://example.com/listing")
    twin_page.pdf = AsyncMock(return_value=b"%PDF-1.4 stale twin")

    browser_state = _browser_state_stub(crashed_page, replacements=twin_page, open_pages=[crashed_page, twin_page])

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is False
    assert "Printing failed" in result.failure_reason
    twin_page.pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_print_page_block_fails_rather_than_printing_a_blank_replacement_tab(
    monkeypatch: pytest.MonkeyPatch, _isolated_download_path: str
) -> None:
    """A replacement tab is published only after its URL restore has been attempted, so a blank one
    means the restore failed. Printing it would report a blank PDF as a successful print."""
    skyvern_context.set(SkyvernContext(organization_id="o_1", workflow_run_id="wr_1", run_id="wr_1"))
    monkeypatch.setattr(block_module, "app", _storage_stub())
    monkeypatch.setattr(PrintPageBlock, "CRASH_RECOVERY_POLL_SECONDS", 0)

    crashed_page = SimpleNamespace(url="https://example.com/listing")
    crashed_page.pdf = AsyncMock(side_effect=PlaywrightError(PRINTING_FAILED))
    blank_page = SimpleNamespace(url="about:blank")
    blank_page.pdf = AsyncMock(return_value=b"%PDF-1.4 blank")

    browser_state = _browser_state_stub(crashed_page, replacements=blank_page)

    block = PrintPageBlock(label="print", output_parameter=_output_parameter("print_out"))
    monkeypatch.setattr(
        PrintPageBlock,
        "get_workflow_run_context",
        lambda self, workflow_run_id: SimpleNamespace(organization_id="o_1"),
    )
    monkeypatch.setattr(PrintPageBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    # Stubbed so that a block which did print the blank tab would report success here rather than
    # erroring on an unmocked upload -- the assertions below must be what rejects it.
    monkeypatch.setattr(
        PrintPageBlock,
        "_upload_pdf_artifact",
        AsyncMock(return_value=("s3://artifacts/wr_1/page.pdf", "https://example.com/page.pdf")),
    )
    monkeypatch.setattr(PrintPageBlock, "record_output_parameter_value", _identity_record)
    monkeypatch.setattr(PrintPageBlock, "build_block_result", _identity_build_block_result)

    result = await block.execute(workflow_run_id="wr_1", workflow_run_block_id="wrb_1", organization_id="o_1")

    assert result.success is False
    assert "Printing failed" in result.failure_reason
    blank_page.pdf.assert_not_awaited()
