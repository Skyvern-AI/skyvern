from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import grpc

from skyvern.constants import (
    FILE_PARSER_MAX_CELLS,
    FILE_PARSER_MAX_DECODED_BYTES,
    FILE_PARSER_MAX_ROWS,
    PDF_OCR_MAX_PAGE_IMAGE_BYTES,
    PDF_OCR_MAX_PAGE_IMAGE_EDGE_PX,
)
from skyvern.file_parser.parser import (
    PARSE_RESOURCE_LIMIT_MESSAGE,
    ParserFailure,
    ParserLimits,
    ParserPart,
    ParserResult,
    TableLimits,
    parse_file_plain,
)
from skyvern.file_parser.proto import file_parser_pb2, file_parser_pb2_grpc
from skyvern.file_parser.transport import (
    GRPC_OPTIONS,
    MAX_PARSER_INPUT_BYTES,
    MAX_PARSER_OUTPUT_BYTES,
    MAX_PARSER_PDF_MEMORY_BYTES,
    MAX_PARSER_PDF_PAGES,
    MAX_PARSER_PDF_RESULT_BYTES,
    MAX_PARSER_TIMEOUT_SECONDS,
    MAX_PARSER_TOKENS,
    TRANSFER_CHUNK_BYTES,
)
from skyvern.forge.sdk.utils.capped_subprocess import (
    CappedSubprocessError,
    hit_resource_limit,
    run_in_capped_subprocess,
)

LOG = logging.getLogger(__name__)
_file_parser_pb2 = cast(Any, file_parser_pb2)
_file_parser_pb2_grpc = cast(Any, file_parser_pb2_grpc)
FileParserServicerBase: Any = _file_parser_pb2_grpc.FileParserServicer


def _bounded(value: int, maximum: int, name: str) -> int:
    if value <= 0:
        raise ParserFailure(f"{name} must be greater than zero", code="invalid_request")
    return min(value, maximum)


async def _receive_file(
    requests: AsyncIterator[Any],
    first: Any,
    destination: Path,
) -> Any:
    if first.WhichOneof("message") != "start":
        raise ParserFailure("Parser stream must start with ParseStart", code="invalid_request")
    start = first.start
    max_input_bytes = _bounded(start.max_input_bytes, MAX_PARSER_INPUT_BYTES, "max_input_bytes")
    digest = hashlib.sha256()
    received = 0
    expected_sequence = 0
    with destination.open("wb") as output:
        async for request in requests:
            message_type = request.WhichOneof("message")
            if message_type == "chunk":
                chunk = request.chunk
                if chunk.sequence != expected_sequence:
                    raise ParserFailure("Parser input chunk arrived out of order", code="invalid_request")
                if len(chunk.data) > TRANSFER_CHUNK_BYTES:
                    raise ParserFailure("Parser input chunk exceeds the chunk limit", code="invalid_request")
                received += len(chunk.data)
                if received > max_input_bytes:
                    raise ParserFailure("Parser input exceeds the declared size limit", code="input_too_large")
                output.write(chunk.data)
                digest.update(chunk.data)
                expected_sequence += 1
                continue
            if message_type != "end":
                raise ParserFailure("Parser input stream contains an unexpected frame", code="invalid_request")
            end = request.end
            if end.chunk_count != expected_sequence or end.total_bytes != received:
                raise ParserFailure("Parser input length does not match its transfer footer", code="invalid_request")
            if end.sha256 != digest.digest():
                raise ParserFailure("Parser input digest does not match its transfer footer", code="invalid_request")
            return start
    raise ParserFailure("Parser input stream ended before its transfer footer", code="invalid_request")


def _part_chunks(part: ParserPart) -> AsyncIterator[bytes]:
    async def iterator() -> AsyncIterator[bytes]:
        if part.data is not None:
            for offset in range(0, len(part.data), TRANSFER_CHUNK_BYTES):
                yield part.data[offset : offset + TRANSFER_CHUNK_BYTES]
            return
        if part.path is None:
            return
        with part.path.open("rb") as file:
            while chunk := file.read(TRANSFER_CHUNK_BYTES):
                yield chunk

    return iterator()


class FileParserService(FileParserServicerBase):
    def __init__(self, scratch_root: Path, table_limits: TableLimits | None = None) -> None:
        self._scratch_root = scratch_root
        self._table_limits = table_limits or TableLimits(
            FILE_PARSER_MAX_ROWS, FILE_PARSER_MAX_CELLS, FILE_PARSER_MAX_DECODED_BYTES
        )
        self._admission = asyncio.Semaphore(1)

    async def Parse(
        self,
        request_iterator: AsyncIterator[Any],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[Any]:
        admitted = False
        try:
            await self._admission.acquire()
            admitted = True
            first = await anext(request_iterator)
            with tempfile.TemporaryDirectory(dir=self._scratch_root, prefix="parse-") as temp_dir:
                scratch_dir = Path(temp_dir)
                input_path = scratch_dir / "input"
                start = await _receive_file(request_iterator, first, input_path)
                limits = ParserLimits(
                    max_input_bytes=_bounded(start.max_input_bytes, MAX_PARSER_INPUT_BYTES, "max_input_bytes"),
                    max_output_bytes=_bounded(start.max_output_bytes, MAX_PARSER_OUTPUT_BYTES, "max_output_bytes"),
                    max_tokens=_bounded(start.max_tokens, MAX_PARSER_TOKENS, "max_tokens"),
                    max_pdf_ocr_pages=_bounded(start.max_pdf_ocr_pages, MAX_PARSER_PDF_PAGES, "max_pdf_ocr_pages"),
                    pdf_memory_bytes=_bounded(start.pdf_memory_bytes, MAX_PARSER_PDF_MEMORY_BYTES, "pdf_memory_bytes"),
                    pdf_result_bytes=_bounded(start.pdf_result_bytes, MAX_PARSER_PDF_RESULT_BYTES, "pdf_result_bytes"),
                    timeout_seconds=MAX_PARSER_TIMEOUT_SECONDS,
                    max_page_image_bytes=PDF_OCR_MAX_PAGE_IMAGE_BYTES,
                    max_page_image_edge_px=PDF_OCR_MAX_PAGE_IMAGE_EDGE_PX,
                    max_table_rows=self._table_limits.max_rows,
                    max_table_cells=self._table_limits.max_cells,
                    max_table_decoded_bytes=self._table_limits.max_decoded_bytes,
                )
                cancel_event = threading.Event()
                parse_task = asyncio.create_task(
                    asyncio.to_thread(
                        lambda: run_in_capped_subprocess(
                            parse_file_plain,
                            str(input_path),
                            Path(start.file_name).name,
                            start.configured_file_type,
                            start.worksheet or None,
                            start.force_ocr,
                            asdict(limits),
                            str(scratch_dir),
                            memory_limit_bytes=limits.pdf_memory_bytes,
                            max_result_bytes=limits.max_output_bytes + TRANSFER_CHUNK_BYTES,
                            timeout_seconds=limits.timeout_seconds,
                            result_memory_factor=1,
                            cancel_event=cancel_event,
                        )
                    )
                )
                try:
                    payload = await asyncio.shield(parse_task)
                except asyncio.CancelledError:
                    cancel_event.set()
                    with suppress(CappedSubprocessError):
                        await asyncio.shield(parse_task)
                    raise
                if error_code := payload.get("error_code"):
                    raise ParserFailure(payload["error_message"], code=error_code)
                result = ParserResult(
                    file_type=payload["file_type"],
                    detection_source=payload["detection_source"],
                    kind=payload["kind"],
                    metadata=payload["metadata"],
                    parts=[
                        ParserPart(
                            part["name"],
                            data=part["data"],
                            path=Path(part["path"]) if part["path"] is not None else None,
                        )
                        for part in payload["parts"]
                    ],
                )
                yield _file_parser_pb2.ParseResponse(
                    start=_file_parser_pb2.ResultStart(
                        file_type=result.file_type,
                        detection_source=result.detection_source,
                        kind=result.kind,
                        metadata_json=json.dumps(result.metadata, separators=(",", ":")),
                        part_count=len(result.parts),
                    )
                )
                total_bytes = 0
                for index, part in enumerate(result.parts):
                    yield _file_parser_pb2.ParseResponse(
                        part_start=_file_parser_pb2.PartStart(index=index, name=part.name, size=part.size)
                    )
                    digest = hashlib.sha256()
                    chunk_count = 0
                    async for chunk in _part_chunks(part):
                        total_bytes += len(chunk)
                        if total_bytes > limits.max_output_bytes:
                            raise ParserFailure(
                                "Parser output exceeds the declared size limit", code="output_too_large"
                            )
                        digest.update(chunk)
                        yield _file_parser_pb2.ParseResponse(
                            chunk=_file_parser_pb2.TransferChunk(sequence=chunk_count, data=chunk)
                        )
                        chunk_count += 1
                    yield _file_parser_pb2.ParseResponse(
                        part_end=_file_parser_pb2.PartEnd(
                            index=index,
                            chunk_count=chunk_count,
                            sha256=digest.digest(),
                        )
                    )
                yield _file_parser_pb2.ParseResponse(end=_file_parser_pb2.ResultEnd(total_bytes=total_bytes))
        except StopAsyncIteration:
            yield _file_parser_pb2.ParseResponse(
                error=_file_parser_pb2.ParseError(code="invalid_request", message="Parser input stream was empty")
            )
        except TimeoutError:
            yield _file_parser_pb2.ParseResponse(
                error=_file_parser_pb2.ParseError(code="timeout", message="File parsing exceeded its time limit")
            )
        except CappedSubprocessError as exc:
            if hit_resource_limit(exc):
                yield _file_parser_pb2.ParseResponse(
                    error=_file_parser_pb2.ParseError(code="parse_limit_exceeded", message=PARSE_RESOURCE_LIMIT_MESSAGE)
                )
            else:
                code = "timeout" if exc.kind == "timeout" else "parse_failed"
                yield _file_parser_pb2.ParseResponse(
                    error=_file_parser_pb2.ParseError(code=code, message=f"File parsing stopped ({exc.kind})")
                )
        except ParserFailure as exc:
            yield _file_parser_pb2.ParseResponse(error=_file_parser_pb2.ParseError(code=exc.code, message=str(exc)))
        except Exception as exc:
            LOG.exception("Isolated file parser failed", extra={"error_type": type(exc).__name__})
            yield _file_parser_pb2.ParseResponse(
                error=_file_parser_pb2.ParseError(
                    code="parse_failed", message=f"File parsing failed ({type(exc).__name__})"
                )
            )
        finally:
            if admitted:
                self._admission.release()


def _positive_env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


async def serve() -> None:
    host = os.environ.get("FILE_PARSER_GRPC_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("FILE_PARSER_GRPC_HOST must be 127.0.0.1 or 0.0.0.0")
    port = int(os.environ.get("FILE_PARSER_GRPC_PORT", "7820"))
    scratch_root = Path(os.environ.get("FILE_PARSER_SCRATCH_DIR", "/tmp/skyvern-file-parser"))
    scratch_root.mkdir(parents=True, exist_ok=True)
    # Same names and defaults as the worker's FILE_PARSER_MAX_* settings, read here because this process never loads
    # skyvern.config.
    table_limits = TableLimits(
        max_rows=_positive_env_int("FILE_PARSER_MAX_ROWS", FILE_PARSER_MAX_ROWS),
        max_cells=_positive_env_int("FILE_PARSER_MAX_CELLS", FILE_PARSER_MAX_CELLS),
        max_decoded_bytes=_positive_env_int("FILE_PARSER_MAX_DECODED_BYTES", FILE_PARSER_MAX_DECODED_BYTES),
    )

    server = grpc.aio.server(options=GRPC_OPTIONS)
    _file_parser_pb2_grpc.add_FileParserServicer_to_server(FileParserService(scratch_root, table_limits), server)
    server.add_insecure_port(f"{host}:{port}")
    await server.start()
    LOG.info("Isolated file parser listening", extra={"host": host, "port": port})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    await stop.wait()
    await server.stop(30)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
