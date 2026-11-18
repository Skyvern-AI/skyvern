from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import aiofiles
import grpc

from skyvern.file_parser.proto import file_parser_pb2, file_parser_pb2_grpc
from skyvern.file_parser.transport import GRPC_OPTIONS, TRANSFER_CHUNK_BYTES

_file_parser_pb2 = cast(Any, file_parser_pb2)
_file_parser_pb2_grpc = cast(Any, file_parser_pb2_grpc)


class IsolatedFileParserError(Exception):
    pass


@dataclass(frozen=True)
class IsolatedParserResult:
    file_type: str
    detection_source: str
    kind: str
    metadata: dict[str, Any]
    content: str | list[dict[str, Any]] | None = None
    images: list[bytes] | None = None
    files: list[dict[str, Any]] | None = None


async def _request_stream(
    file_path: Path,
    *,
    file_name: str,
    configured_file_type: str,
    worksheet: str | None,
    force_ocr: bool,
    max_input_bytes: int,
    max_output_bytes: int,
    max_tokens: int,
    max_pdf_ocr_pages: int,
    pdf_memory_bytes: int,
    pdf_result_bytes: int,
) -> AsyncIterator[Any]:
    yield _file_parser_pb2.ParseRequest(
        start=_file_parser_pb2.ParseStart(
            file_name=file_name,
            configured_file_type=configured_file_type,
            worksheet=worksheet or "",
            force_ocr=force_ocr,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            max_tokens=max_tokens,
            max_pdf_ocr_pages=max_pdf_ocr_pages,
            pdf_memory_bytes=pdf_memory_bytes,
            pdf_result_bytes=pdf_result_bytes,
        )
    )
    digest = hashlib.sha256()
    total_bytes = 0
    sequence = 0
    async with aiofiles.open(file_path, "rb") as file:
        while chunk := await file.read(TRANSFER_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > max_input_bytes:
                raise IsolatedFileParserError(
                    f"File size ({total_bytes} bytes) exceeds the parser input limit of {max_input_bytes} bytes"
                )
            digest.update(chunk)
            yield _file_parser_pb2.ParseRequest(chunk=_file_parser_pb2.TransferChunk(sequence=sequence, data=chunk))
            sequence += 1
    yield _file_parser_pb2.ParseRequest(
        end=_file_parser_pb2.TransferEnd(
            chunk_count=sequence,
            total_bytes=total_bytes,
            sha256=digest.digest(),
        )
    )


def _safe_destination(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise IsolatedFileParserError("Parser service returned an unsafe file name")
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


async def parse_file_remotely(
    target: str,
    file_path: Path,
    *,
    file_name: str,
    configured_file_type: str,
    worksheet: str | None,
    force_ocr: bool,
    output_dir: Path,
    timeout_seconds: float,
    max_input_bytes: int,
    max_output_bytes: int,
    max_tokens: int,
    max_pdf_ocr_pages: int,
    pdf_memory_bytes: int,
    pdf_result_bytes: int,
) -> IsolatedParserResult:
    file_size = file_path.stat().st_size
    if file_size > max_input_bytes:
        raise IsolatedFileParserError(
            f"File size ({file_size} bytes) exceeds the parser input limit of {max_input_bytes} bytes"
        )
    requests = _request_stream(
        file_path,
        file_name=file_name,
        configured_file_type=configured_file_type,
        worksheet=worksheet,
        force_ocr=force_ocr,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
        max_tokens=max_tokens,
        max_pdf_ocr_pages=max_pdf_ocr_pages,
        pdf_memory_bytes=pdf_memory_bytes,
        pdf_result_bytes=pdf_result_bytes,
    )
    channel = grpc.aio.insecure_channel(target, options=GRPC_OPTIONS)
    parts: list[bytes] = []
    files: list[dict[str, Any]] = []
    current_data = bytearray()
    current_file = None
    current_path: Path | None = None
    current_digest = hashlib.sha256()
    current_size = 0
    part_open = False
    expected_part = 0
    expected_sequence = 0
    total_bytes = 0
    start = None
    try:
        stub = _file_parser_pb2_grpc.FileParserStub(channel)
        async for response in stub.Parse(requests, timeout=timeout_seconds):
            message_type = response.WhichOneof("message")
            if message_type == "error":
                raise IsolatedFileParserError(f"{response.error.code}: {response.error.message}")
            if message_type == "start":
                if start is not None:
                    raise IsolatedFileParserError("Parser service returned duplicate result metadata")
                start = response.start
                continue
            if start is None:
                raise IsolatedFileParserError("Parser service returned data before result metadata")
            if message_type == "part_start":
                part = response.part_start
                if part.index != expected_part or part_open:
                    raise IsolatedFileParserError("Parser service returned an invalid part sequence")
                part_open = True
                expected_sequence = 0
                current_size = part.size
                current_digest = hashlib.sha256()
                if start.kind == "files":
                    current_path = _safe_destination(output_dir, part.name)
                    current_file = await aiofiles.open(current_path, "wb")
                continue
            if message_type == "chunk":
                chunk = response.chunk
                if not part_open or chunk.sequence != expected_sequence or len(chunk.data) > TRANSFER_CHUNK_BYTES:
                    raise IsolatedFileParserError("Parser service returned an invalid chunk sequence")
                total_bytes += len(chunk.data)
                if total_bytes > max_output_bytes:
                    raise IsolatedFileParserError("Parser service output exceeds the configured limit")
                current_digest.update(chunk.data)
                if current_file is not None:
                    await current_file.write(chunk.data)
                else:
                    current_data.extend(chunk.data)
                expected_sequence += 1
                continue
            if message_type == "part_end":
                part_end = response.part_end
                if not part_open or part_end.index != expected_part or part_end.chunk_count != expected_sequence:
                    raise IsolatedFileParserError("Parser service returned an invalid part footer")
                if part_end.sha256 != current_digest.digest():
                    raise IsolatedFileParserError("Parser service returned a part with an invalid digest")
                if current_file is not None:
                    await current_file.close()
                    if current_path is None or current_path.stat().st_size != current_size:
                        raise IsolatedFileParserError("Parser service returned a file with an invalid length")
                    files.append(
                        {
                            "file_name": str(current_path.relative_to(output_dir)),
                            "file_path": str(current_path),
                            "file_size": current_size,
                        }
                    )
                    current_file = None
                    current_path = None
                else:
                    if len(current_data) != current_size:
                        raise IsolatedFileParserError("Parser service returned a part with an invalid length")
                    parts.append(bytes(current_data))
                    current_data.clear()
                part_open = False
                expected_part += 1
                continue
            if message_type == "end":
                if part_open or response.end.total_bytes != total_bytes or expected_part != start.part_count:
                    raise IsolatedFileParserError("Parser service returned an invalid result footer")
                metadata = json.loads(start.metadata_json or "{}")
                if start.kind == "text":
                    content: str | list[dict[str, Any]] | None = parts[0].decode() if parts else ""
                    return IsolatedParserResult(
                        start.file_type, start.detection_source, start.kind, metadata, content=content
                    )
                if start.kind == "json":
                    content = json.loads(parts[0]) if parts else []
                    return IsolatedParserResult(
                        start.file_type, start.detection_source, start.kind, metadata, content=content
                    )
                if start.kind == "images":
                    return IsolatedParserResult(
                        start.file_type, start.detection_source, start.kind, metadata, images=parts
                    )
                if start.kind == "files":
                    return IsolatedParserResult(
                        start.file_type, start.detection_source, start.kind, metadata, files=files
                    )
                raise IsolatedFileParserError(f"Parser service returned unsupported result kind: {start.kind}")
        raise IsolatedFileParserError("Parser service ended before returning a result")
    except grpc.aio.AioRpcError as exc:
        raise IsolatedFileParserError(f"Parser service unavailable ({exc.code().name})") from None
    finally:
        if current_file is not None:
            await current_file.close()
        await channel.close()
        if start is None or expected_part != start.part_count:
            shutil.rmtree(output_dir, ignore_errors=True)
