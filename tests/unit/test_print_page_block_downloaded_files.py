"""Tests for PrintPageBlock surfacing the generated PDF in ``downloaded_files`` (SKY-9416)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    page = SimpleNamespace()
    page.pdf = AsyncMock(return_value=b"%PDF-1.4 test bytes")
    return page


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
    # capture_block_download_baseline calls get_downloaded_files first (empty here),
    # then the helper calls it again post-PDF.
    get_mock = AsyncMock(side_effect=[[], [file_info]])

    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            save_downloaded_files=save_mock,
            get_downloaded_files=get_mock,
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
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
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

    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            save_downloaded_files=AsyncMock(),
            get_downloaded_files=AsyncMock(return_value=[prev_file, new_file]),
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
        AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=page))),
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
    # Baseline-capture awaits get_downloaded_files once before save runs.
    get_mock = AsyncMock(return_value=[])

    fake_app = SimpleNamespace(
        STORAGE=SimpleNamespace(
            save_downloaded_files=save_mock,
            get_downloaded_files=get_mock,
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
        AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=page))),
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
            save_downloaded_files=AsyncMock(),
            get_downloaded_files=get_mock,
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
        AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=page))),
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
