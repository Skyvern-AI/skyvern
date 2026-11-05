from __future__ import annotations

import asyncio
import codecs
import hashlib
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

import skyvern.forge.sdk.workflow.document_text as document_text
import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.utils.pdf_parser import extract_pdf_file
from skyvern.forge.sdk.workflow.code_block_authorized_files import (
    BlockDownloadLog,
    MaterializedAuthorizedFile,
    RegisteredDownloadSource,
    capture_authorized_file,
)
from skyvern.forge.sdk.workflow.document_text import (
    READ_DOCUMENT_TEXT_MAX_BYTES,
    READ_DOCUMENT_TEXT_MAX_CALLS,
    READ_DOCUMENT_TEXT_MAX_FILE_BYTES,
    ClaimedDocuments,
    DocumentTextReader,
)
from skyvern.forge.sdk.workflow.exceptions import FileParseTimeout
from skyvern.forge.sdk.workflow.models.block import (
    PDF_FIRST_PAGE_OVER_TOKEN_LIMIT_REASON,
    DownloadClaimOutcomeRecorder,
    _bind_code_block_download_claim,
    read_pdf_text_layer,
)
from skyvern.webeye.browser_artifacts import DownloadBinding
from tests.unit.workflow.test_file_parser_block import _INVOICE_LINES, _build_pdf

RUN_ID = "wr_document_text"
ORG_ID = "o_document_text"


def _loader(content: bytes) -> Callable[[int], Awaitable[bytes]]:
    async def load(max_bytes: int) -> bytes:
        return content

    return load


def _claimed_documents(download_root: Path) -> ClaimedDocuments:
    return ClaimedDocuments(
        read_pdf_text_layer,
        download_root=download_root,
        download_run_id=RUN_ID,
        organization_id=ORG_ID,
        inherited_files=frozenset(),
        download_log=BlockDownloadLog(None),
    )


def _materialize(download_root: Path, name: str, content: bytes) -> MaterializedAuthorizedFile:
    run_directory = download_root / RUN_ID
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / name).write_bytes(content)
    return capture_authorized_file(
        run_directory / name,
        download_root=download_root,
        workflow_run_id=RUN_ID,
        organization_id=ORG_ID,
        max_bytes=1024 * 1024,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "expected_text"),
    [
        ("notes.txt", codecs.BOM_UTF8 + "Amount due: 42.10 €".encode(), "Amount due: 42.10 €"),
        ("rows.CSV", b"name,amount\nAcme,12\n", "name,amount\nAcme,12\n"),
        ("bill.json", b'{"amount": 12}', '{"amount": 12}'),
    ],
)
async def test_text_files_decode_as_utf8(filename: str, content: bytes, expected_text: str) -> None:
    result = await DocumentTextReader(read_pdf_text_layer).read(filename, _loader(content))

    assert result == {
        "text": expected_text,
        "page_count": None,
        "char_count": len(expected_text),
        "truncated": False,
        "text_layer_problem": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "names"),
    [("latin.txt", "café".encode("latin-1"), "UTF-8"), ("bill.docx", b"PK", ".pdf")],
)
async def test_invalid_utf8_and_unsupported_types_are_refused(filename: str, content: bytes, names: str) -> None:
    result = await DocumentTextReader(read_pdf_text_layer).read(filename, _loader(content))

    assert result.keys() == {"error"}
    assert names in result["error"]  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_a_first_page_over_the_token_limit_is_reported_as_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(block_module, "extract_pdf_file", partial(extract_pdf_file, max_tokens=1))

    result = await DocumentTextReader(read_pdf_text_layer).read("bill.pdf", _loader(_build_pdf(_INVOICE_LINES)))

    assert result == {
        "text": "",
        "page_count": 1,
        "char_count": 0,
        "truncated": True,
        "text_layer_problem": PDF_FIRST_PAGE_OVER_TOKEN_LIMIT_REASON,
    }


@pytest.mark.asyncio
async def test_a_call_past_the_cap_is_refused_without_loading() -> None:
    reader = DocumentTextReader(read_pdf_text_layer)
    loads: list[int] = []

    async def load(max_bytes: int) -> bytes:
        loads.append(max_bytes)
        return b"x"

    for _ in range(READ_DOCUMENT_TEXT_MAX_CALLS):
        assert "text" in await reader.read("a.txt", load)
    over = await reader.read("a.txt", load)

    assert over.keys() == {"error"}
    assert str(READ_DOCUMENT_TEXT_MAX_CALLS) in over["error"]  # type: ignore[typeddict-item]
    assert len(loads) == READ_DOCUMENT_TEXT_MAX_CALLS


@pytest.mark.asyncio
async def test_reads_run_one_at_a_time_so_each_load_sees_the_bytes_already_spent() -> None:
    reader = DocumentTextReader(read_pdf_text_layer)
    budgets: list[int] = []
    release = asyncio.Event()

    async def first_load(max_bytes: int) -> bytes:
        budgets.append(max_bytes)
        await release.wait()
        return b"x" * READ_DOCUMENT_TEXT_MAX_FILE_BYTES

    async def second_load(max_bytes: int) -> bytes:
        budgets.append(max_bytes)
        return b"y"

    full = b"x" * READ_DOCUMENT_TEXT_MAX_FILE_BYTES
    for _ in range(READ_DOCUMENT_TEXT_MAX_BYTES // READ_DOCUMENT_TEXT_MAX_FILE_BYTES - 1):
        await reader.read("a.txt", _loader(full))
    first = asyncio.create_task(reader.read("a.txt", first_load))
    second = asyncio.create_task(reader.read("b.txt", second_load))
    await asyncio.sleep(0.01)
    assert len(budgets) == 1
    release.set()
    await asyncio.gather(first, second)

    assert budgets == [READ_DOCUMENT_TEXT_MAX_FILE_BYTES, 0]


@pytest.mark.asyncio
async def test_a_parse_timeout_stops_further_reads() -> None:
    async def timed_out_parse(*args: object, **kwargs: object) -> tuple[str, str | None]:
        raise FileParseTimeout("read_document_text", "pdf", 1)

    reader = DocumentTextReader(timed_out_parse)

    timed_out = await reader.read("bill.pdf", _loader(b"%PDF"))
    after = await reader.read("notes.txt", _loader(b"text"))

    assert timed_out.keys() == {"error"}
    assert after.keys() == {"error"}
    assert str(READ_DOCUMENT_TEXT_MAX_CALLS) in after["error"]  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_a_read_queued_behind_a_busy_parse_pool_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    pool.submit(release.wait)
    monkeypatch.setattr(document_text, "_PARSE_EXECUTOR", pool)
    monkeypatch.setattr(block_module, "FILE_PARSE_STEP_TIMEOUT_SECONDS", 0.2)
    try:
        result = await DocumentTextReader(read_pdf_text_layer).read("bill.pdf", _loader(_build_pdf(_INVOICE_LINES)))
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert result == {"error": document_text._PARSE_TIMEOUT_ERROR}


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["other.pdf", "/etc/passwd", f"../{RUN_ID}/bill.pdf", {"file_id": "bill.pdf"}, None])
async def test_a_handle_the_block_was_not_issued_is_refused_without_reading(
    handle: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = _claimed_documents(tmp_path)
    documents.record(
        "bill.pdf", _materialize(tmp_path, "bill.pdf", _build_pdf(_INVOICE_LINES)), outcome="returned_proven"
    )

    def no_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unissued handle must not reach the file")

    monkeypatch.setattr(document_text, "read_authorized_file", no_read)

    result = await documents.read(handle)

    assert result.keys() == {"error"}
    assert "click_and_claim_download" in result["error"]  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_a_claim_materialized_by_the_worker_is_read_without_another_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = _claimed_documents(tmp_path)
    documents.record("bill.txt", _materialize(tmp_path, "bill.txt", b"Total 12.50"), outcome="returned_proven")

    async def no_capture(*args: object, **kwargs: object) -> None:
        raise AssertionError("a materialized claim must not be captured again")

    monkeypatch.setattr(document_text, "capture_claimed_download", no_capture)
    monkeypatch.setattr(document_text, "capture_registered_download", no_capture)

    first = await documents.read("bill.txt")
    second = await documents.read("bill.txt")

    assert first == second
    assert first["text"] == "Total 12.50"  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_two_reads_of_one_download_claim_capture_it_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    materialized = _materialize(tmp_path, "bill.txt", b"Total 12.50")
    captures: list[object] = []

    async def capture(download: object, **kwargs: object) -> MaterializedAuthorizedFile:
        captures.append(download)
        return materialized

    monkeypatch.setattr(document_text, "capture_claimed_download", capture)
    download = SimpleNamespace(suggested_filename="bill.txt")
    documents = _claimed_documents(tmp_path)
    documents.record("bill.txt", download, outcome="returned_proven")  # type: ignore[arg-type]

    results = [await documents.read("bill.txt"), await documents.read("bill.txt")]

    assert captures == [download]
    assert [result["text"] for result in results] == ["Total 12.50", "Total 12.50"]  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_two_claims_returning_one_name_are_refused_rather_than_guessed(tmp_path: Path) -> None:
    documents = _claimed_documents(tmp_path)
    documents.record("bill.txt", _materialize(tmp_path, "bill.txt", b"first"), outcome="returned_proven")
    documents.record("bill.txt", _materialize(tmp_path, "bill (1).txt", b"second"), outcome="returned_proven")

    result = await documents.read("bill.txt")

    assert result == {"error": document_text._AMBIGUOUS_CLAIM_ERROR}


@pytest.mark.asyncio
async def test_a_claim_with_no_download_event_reads_the_file_registered_under_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"name,amount\nAcme,77.00\n"
    earlier = FileInfo(url="s3://earlier", filename="other.csv", checksum="0" * 64, file_size=3, artifact_id="a_0")
    claimed = FileInfo(
        url="s3://claimed",
        filename="statement.csv",
        checksum=hashlib.sha256(content).hexdigest(),
        file_size=len(content),
        artifact_id="a_1",
    )

    async def probe() -> tuple[list[FileInfo] | None, set[str]]:
        return [earlier, claimed], set()

    async def fetch(self: RegisteredDownloadSource, artifact_id: str) -> bytes | None:
        return content if artifact_id == "a_1" else None

    monkeypatch.setattr(RegisteredDownloadSource, "fetch", fetch)
    registered = RegisteredDownloadSource(
        probe=probe, organization_id=ORG_ID, run_id=RUN_ID, baseline={(earlier.filename, earlier.checksum): "other.csv"}
    )
    documents = _claimed_documents(tmp_path)
    documents.record("statement.csv", None, outcome="returned_proven", registered=registered)

    result = await documents.read("statement.csv")

    assert result["text"] == content.decode()  # type: ignore[typeddict-item]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "fires_event", "readable"),
    [
        ("returned_proven", True, True),
        ("returned_proven", False, True),
        ("returned_unproven", True, True),
        ("returned_unproven", False, False),
    ],
)
async def test_an_inline_claim_is_readable_only_when_its_delivery_was_proven(
    outcome: str, fires_event: bool, readable: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = SimpleNamespace(suggested_filename="bill.txt")
    documents = _claimed_documents(tmp_path)
    materialized = _materialize(tmp_path, "bill.txt", b"Total 12.50")
    registered = RegisteredDownloadSource(
        probe=None,  # type: ignore[arg-type]
        organization_id=ORG_ID,
        run_id=RUN_ID,
        baseline={},
    )
    captured: list[object] = []
    baselines: list[object] = []

    async def registered_source(*args: object, **kwargs: object) -> RegisteredDownloadSource:
        return registered

    async def claim(
        *args: object,
        outcome_recorder: DownloadClaimOutcomeRecorder,
        on_claimed: Callable[[object], None],
        registered_downloads: RegisteredDownloadSource | None,
        **kwargs: object,
    ) -> str:
        baselines.append(registered_downloads)
        if fires_event:
            on_claimed(download)
        outcome_recorder.record(outcome)  # type: ignore[arg-type]
        return "bill.txt"

    async def capture_download(source: object, **kwargs: object) -> MaterializedAuthorizedFile:
        captured.append(source)
        return materialized

    async def capture_registered(
        name: str, *, registered: RegisteredDownloadSource, **kwargs: object
    ) -> MaterializedAuthorizedFile:
        captured.append(registered)
        return materialized

    monkeypatch.setattr(block_module, "_registered_download_source", registered_source)
    monkeypatch.setattr(block_module, "_code_block_click_and_claim_download_builtin", claim)
    monkeypatch.setattr(document_text, "capture_claimed_download", capture_download)
    monkeypatch.setattr(document_text, "capture_registered_download", capture_registered)
    block_outcome = DownloadClaimOutcomeRecorder()
    click_and_claim_download = _bind_code_block_download_claim(
        organization_id=ORG_ID,
        workflow_run_id=RUN_ID,
        download_binding=DownloadBinding.RUN_DIR,
        outcome_recorder=block_outcome,
        claimed_documents=documents,
    )

    assert await click_and_claim_download(SimpleNamespace(), "#statement") == "bill.txt"  # type: ignore[arg-type]
    result = await documents.read("bill.txt")

    assert block_outcome.outcome == outcome
    assert baselines == [registered]
    if readable:
        assert result["text"] == "Total 12.50"  # type: ignore[typeddict-item]
        assert captured == [download if fires_event else registered]
    else:
        assert result.keys() == {"error"}
        assert captured == []
