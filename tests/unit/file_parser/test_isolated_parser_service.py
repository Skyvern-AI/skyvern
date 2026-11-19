from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import docx
import grpc
import pandas as pd
import pytest
from PIL import Image
from pypdf import PdfWriter

from skyvern.file_parser.client import IsolatedFileParserError, parse_file_remotely
from skyvern.file_parser.proto import file_parser_pb2, file_parser_pb2_grpc
from skyvern.file_parser.service import FileParserService
from skyvern.file_parser.transport import GRPC_OPTIONS
from skyvern.forge.sdk.utils.capped_subprocess import CappedSubprocessError


@pytest.mark.asyncio
async def test_isolated_parser_contract_and_fail_closed_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            (
                "import sys; import skyvern.file_parser.service; "
                "forbidden = ('skyvern.config', 'cloud', 'skyvern.db', 'skyvern.forge.sdk.api.llm'); "
                "assert not [name for name in sys.modules if name.startswith(forbidden)]"
            ),
        ],
        check=True,
    )
    server = grpc.aio.server(options=GRPC_OPTIONS)
    file_parser_pb2_grpc.add_FileParserServicer_to_server(FileParserService(tmp_path), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    target = f"127.0.0.1:{port}"

    async def parse(path: Path, *, max_input_bytes: int = 512 * 1024 * 1024):
        return await parse_file_remotely(
            target,
            path,
            file_name=path.name,
            configured_file_type="auto_detect",
            worksheet=None,
            force_ocr=False,
            output_dir=tmp_path / f"output-{path.stem}",
            timeout_seconds=30,
            max_input_bytes=max_input_bytes,
            max_output_bytes=1024**3,
            max_tokens=256_000,
            max_pdf_ocr_pages=50,
            pdf_memory_bytes=512 * 1024 * 1024,
            pdf_result_bytes=256 * 1024 * 1024,
        )

    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("name,count\nalpha,2\n")
    assert (await parse(csv_path)).content == [{"name": "alpha", "count": "2"}]

    excel_path = tmp_path / "rows.xlsx"
    pd.DataFrame({"name": ["alpha"], "count": [2]}).to_excel(excel_path, index=False)
    assert (await parse(excel_path)).content == [{"name": "alpha", "count": 2}]

    docx_path = tmp_path / "words.docx"
    document = docx.Document()
    document.add_paragraph("synthetic text")
    document.save(docx_path)
    assert (await parse(docx_path)).content == "synthetic text"

    image_path = tmp_path / "pixel.png"
    Image.new("RGB", (2, 2), "white").save(image_path)
    image_result = await parse(image_path)
    assert image_result.kind == "images"
    assert image_result.images and image_result.images[0].startswith(b"\x89PNG\r\n\x1a\n")

    tiff_path = tmp_path / "pages.tiff"
    tiff_pages = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
    tiff_pages[0].save(tiff_path, format="TIFF", save_all=True, append_images=tiff_pages[1:])
    tiff_result = await parse(tiff_path)
    assert tiff_result.images is not None and len(tiff_result.images) == 2
    assert [Image.open(io.BytesIO(page)).getpixel((0, 0)) for page in tiff_result.images] == [
        (255, 0, 0),
        (0, 0, 255),
    ]

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as pdf_file:
        writer.write(pdf_file)
    pdf_result = await parse(pdf_path)
    assert pdf_result.file_type == "pdf"
    assert pdf_result.kind == "images"

    zip_path = tmp_path / "files.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("nested/item.txt", b"synthetic")
    zip_result = await parse(zip_path)
    assert zip_result.files == [
        {
            "file_name": "nested/item.txt",
            "file_path": str(tmp_path / "output-files" / "nested" / "item.txt"),
            "file_size": 9,
        }
    ]
    assert Path(zip_result.files[0]["file_path"]).read_bytes() == b"synthetic"

    malformed = tmp_path / "malformed.csv"
    malformed.write_bytes(b"a\x00b")
    with pytest.raises(IsolatedFileParserError, match="File contains binary data"):
        await parse(malformed)
    with pytest.raises(IsolatedFileParserError, match="parser input limit"):
        await parse(csv_path, max_input_bytes=4)

    oversized_zip = tmp_path / "oversized.zip"
    oversized_zip.write_bytes(zip_path.read_bytes())
    oversized_bytes = bytearray(oversized_zip.read_bytes())
    central_directory = oversized_bytes.index(b"PK\x01\x02")
    oversized_bytes[central_directory + 24 : central_directory + 28] = (1024**3 + 1).to_bytes(4, "little")
    oversized_zip.write_bytes(oversized_bytes)
    with pytest.raises(IsolatedFileParserError, match="uncompressed size"):
        await parse(oversized_zip)

    await server.stop(0)
    with pytest.raises(IsolatedFileParserError, match="service unavailable"):
        await parse(csv_path)

    class InvalidService(file_parser_pb2_grpc.FileParserServicer):
        async def Parse(self, request_iterator, context):
            yield file_parser_pb2.ParseResponse(
                start=file_parser_pb2.ResultStart(file_type="csv", kind="json", part_count=1)
            )
            yield file_parser_pb2.ParseResponse(chunk=file_parser_pb2.TransferChunk(sequence=0, data=b"[]"))

    invalid_server = grpc.aio.server(options=GRPC_OPTIONS)
    file_parser_pb2_grpc.add_FileParserServicer_to_server(InvalidService(), invalid_server)
    invalid_port = invalid_server.add_insecure_port("127.0.0.1:0")
    await invalid_server.start()
    target = f"127.0.0.1:{invalid_port}"
    try:
        with pytest.raises(IsolatedFileParserError, match="invalid chunk sequence"):
            await parse(csv_path)
    finally:
        await invalid_server.stop(0)

    async def requests():
        data = b"name,count\nalpha,2\n"
        yield file_parser_pb2.ParseRequest(
            start=file_parser_pb2.ParseStart(
                file_name="rows.csv",
                configured_file_type="csv",
                max_input_bytes=1024,
                max_output_bytes=1024,
                max_tokens=100,
                max_pdf_ocr_pages=1,
                pdf_memory_bytes=256 * 1024 * 1024,
                pdf_result_bytes=1024,
            )
        )
        yield file_parser_pb2.ParseRequest(chunk=file_parser_pb2.TransferChunk(sequence=0, data=data))
        yield file_parser_pb2.ParseRequest(
            end=file_parser_pb2.TransferEnd(chunk_count=1, total_bytes=len(data), sha256=hashlib.sha256(data).digest())
        )

    child_started = threading.Event()
    child_stopped = threading.Event()
    second_child_started = threading.Event()
    started_children = 0

    def cancelled_parse(*args: object, cancel_event: threading.Event, **kwargs: object) -> None:
        nonlocal started_children
        started_children += 1
        (child_started if started_children == 1 else second_child_started).set()
        assert cancel_event.wait(2)
        child_stopped.set()
        raise CappedSubprocessError("cancelled", kind="cancelled")

    monkeypatch.setattr("skyvern.file_parser.service.run_in_capped_subprocess", cancelled_parse)
    scratch = tmp_path / "admission"
    scratch.mkdir()
    service = FileParserService(scratch)
    first_call = service.Parse(requests(), None)  # type: ignore[arg-type]
    first_response = asyncio.create_task(anext(first_call))
    assert await asyncio.to_thread(child_started.wait, 2)

    second_call = service.Parse(requests(), None)  # type: ignore[arg-type]
    second_response = asyncio.create_task(anext(second_call))
    await asyncio.sleep(0)
    assert not second_response.done()
    assert len(list(scratch.glob("parse-*"))) == 1

    first_response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_response
    assert child_stopped.is_set()

    assert await asyncio.to_thread(second_child_started.wait, 2)
    second_response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_response
    await first_call.aclose()
    await second_call.aclose()
