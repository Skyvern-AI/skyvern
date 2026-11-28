from __future__ import annotations

import codecs
import csv
import io
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Literal

import docx
import filetype
import pandas as pd
from charset_normalizer import from_bytes
from PIL import Image

from skyvern.constants import (
    MAX_DECODED_IMAGE_PIXELS,
    MAX_IMAGE_BLOCKS,
    MAX_TOTAL_DECODED_IMAGE_PIXELS,
    PDF_OCR_FALLBACK_RENDER_RESOLUTION,
    PDF_OCR_RENDER_RESOLUTION,
)
from skyvern.forge.sdk.utils.capped_subprocess import CappedSubprocessError
from skyvern.forge.sdk.utils.pdf_parser import (
    PdfExtractStats,
    extract_pdf_file,
    render_pdf_pages_as_images,
    scrambled_text_layer_reason,
    validate_pdf_file,
)
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.utils.token_counter import count_tokens

ParserKind = Literal["text", "json", "images", "files"]
DetectionSource = Literal["explicit", "extension", "magic", "fallback"]

_CSV_SNIFF_LINES = 5
_CSV_BINARY_PREFIX_BYTES = 4096
_CSV_UTF_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)
_MAX_CSV_FIELD_SIZE_BYTES = 10 * 1024 * 1024
_MAX_ZIP_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ZIP_ENTRIES = 1000
_MAX_ZIP_UNCOMPRESSED_BYTES = 1024**3
_ZIP_JUNK_DIRS = ("__MACOSX",)
_ZIP_JUNK_FILES = (".DS_Store", "Thumbs.db")
_ZIP_EOCD_TAIL_BYTES = 65_557 + 20 + 56
_OLE_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

csv.field_size_limit(_MAX_CSV_FIELD_SIZE_BYTES)


class ParserFailure(Exception):
    def __init__(self, message: str, *, code: str = "invalid_file") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParserLimits:
    max_input_bytes: int
    max_output_bytes: int
    max_tokens: int
    max_pdf_ocr_pages: int
    pdf_memory_bytes: int
    pdf_result_bytes: int
    timeout_seconds: float
    max_page_image_bytes: int
    max_page_image_edge_px: int
    max_table_rows: int
    max_table_cells: int
    max_table_decoded_bytes: int

    @property
    def table(self) -> TableLimits:
        return TableLimits(self.max_table_rows, self.max_table_cells, self.max_table_decoded_bytes)


@dataclass(frozen=True)
class TableLimits:
    max_rows: int
    max_cells: int
    max_decoded_bytes: int


@dataclass(frozen=True)
class ParserPart:
    name: str
    data: bytes | None = None
    path: Path | None = None

    @property
    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        if self.path is None:
            return 0
        return self.path.stat().st_size


@dataclass(frozen=True)
class ParserResult:
    file_type: str
    detection_source: DetectionSource
    kind: ParserKind
    parts: list[ParserPart]
    metadata: dict[str, Any] = field(default_factory=dict)


def _detect_encoding(file_path: Path) -> str:
    with file_path.open("rb") as file:
        raw_data = file.read(65_536)
    # charset-normalizer reports a UTF-8 BOM file as utf_8, which leaves the BOM in the first header.
    if raw_data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    best_match = from_bytes(raw_data).best()
    if best_match and best_match.encoding:
        return best_match.encoding
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            raw_data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _sniff_csv_delimiter(file_path: Path, limits: TableLimits) -> tuple[str, str]:
    check_csv_size_within_limits(file_path, limits)
    with file_path.open("rb") as file:
        raw_prefix = file.read(_CSV_BINARY_PREFIX_BYTES)
    if not raw_prefix.strip():
        raise ParserFailure("File is empty")
    if b"\x00" in raw_prefix and not raw_prefix.startswith(_CSV_UTF_BOMS):
        raise ParserFailure("File contains binary data")

    encoding = _detect_encoding(file_path)
    with file_path.open(encoding=encoding, errors="replace", newline="") as file:
        sample = "".join(line for _, line in zip(range(_CSV_SNIFF_LINES), file, strict=False))
    if not sample.strip():
        raise ParserFailure("File is empty")
    try:
        delimiter = csv.Sniffer().sniff(sample).delimiter
        # The sniffer can pick a letter or digit as the delimiter of a one-column file ("email" -> "m").
        if delimiter.isalnum():
            raise csv.Error("Could not determine delimiter")
    except csv.Error:
        delimiter = "\t" if file_path.name.lower().endswith(".tsv") else ","
    return delimiter, encoding


def _detect_file_type(file_name: str, file_path: Path, configured_file_type: str) -> tuple[str, DetectionSource]:
    if configured_file_type in {"image", "excel", "pdf", "docx", "zip"}:
        return configured_file_type, "explicit"

    suffix = Path(file_name).suffix.lower()
    extensions = {
        ".xlsx": "excel",
        ".xls": "excel",
        ".xlsm": "excel",
        ".pdf": "pdf",
        ".tsv": "csv",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
        ".bmp": "image",
        ".webp": "image",
        ".tiff": "image",
        ".tif": "image",
        ".docx": "docx",
        ".zip": "zip",
        ".csv": "csv",
    }
    if suffix == ".doc":
        raise ParserFailure(
            "Legacy .doc format (Word 97-2003) is not supported. Please convert the file to .docx format."
        )
    if suffix in extensions:
        return extensions[suffix], "extension"

    kind = filetype.guess(str(file_path))
    mime = kind.mime if kind else None
    by_mime = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
        "application/vnd.ms-excel": "excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/zip": "zip",
    }
    if mime in by_mime:
        return by_mime[mime], "magic"
    if mime and mime.startswith("image/"):
        return "image", "magic"
    with file_path.open("rb") as file:
        if file.read(len(_OLE_CFB_MAGIC)) == _OLE_CFB_MAGIC:
            raise ParserFailure(
                "Legacy .doc format (Word 97-2003) is not supported. Please convert the file to .docx format."
            )
    return "csv", "fallback"


def _clean_dataframe_for_json(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    cleaned = dataframe.replace({pd.NA: "nan", pd.NaT: "nan"})
    cleaned = cleaned.where(pd.notna(cleaned), "nan")
    records = cleaned.to_dict("records")
    for record in records:
        for key, value in record.items():
            if pd.isna(value):
                record[key] = "nan"
            elif isinstance(value, (pd.Timestamp, datetime, date, time)):
                record[key] = value.isoformat()
            elif isinstance(value, pd.Timedelta):
                record[key] = str(value)
    return records


PARSE_RESOURCE_LIMIT_MESSAGE = (
    "The file is too large or complex to parse within the parser's memory and time limits. "
    "Split it into smaller files, then try again."
)


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8", errors="surrogatepass"))


def _json_size_upper_bound(value: object) -> int:
    # JSON escaping turns one character into at most 6 bytes (a \u00XX escape).
    if isinstance(value, dict) and None not in value and None not in value.values():
        try:
            # A CSV record of only string keys and values: count its characters without a Python loop.
            return 6 * (sum(map(len, value)) + sum(map(len, value.values()))) + 7 * len(value) + 2
        except TypeError:
            pass
    if isinstance(value, dict):
        return 2 + sum(7 + 6 * len(key or "null") + _json_size_upper_bound(item) for key, item in value.items())
    if isinstance(value, list):
        return 2 + sum(1 + _json_size_upper_bound(item) for item in value)
    return 6 * len(value) + 2 if isinstance(value, str) else 5


def _encoded_size_within(value: object, budget: int) -> int:
    """Size of value as the parser's compact JSON (escapes included), stopping once it passes budget."""
    # A record that surely fits is sized in one C-encoder call; one that might not is sized field by field, so an
    # oversized record is never built whole just to be measured.
    if _json_size_upper_bound(value) <= budget:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8", errors="surrogatepass"))
    if isinstance(value, dict):
        size = 2 + max(len(value) - 1, 0)
        for key, item in value.items():
            size += (6 if key is None else _json_size(key)) + 1 + _encoded_size_within(item, budget - size)
            if size > budget:
                break
        return size
    if isinstance(value, list):
        size = 2 + max(len(value) - 1, 0)
        for item in value:
            size += _encoded_size_within(item, budget - size)
            if size > budget:
                break
        return size
    return _json_size(value)


def _check_table_limit(file_kind: str, used: int, limit: int, unit: str) -> None:
    if used > limit:
        raise ParserFailure(
            f"The {file_kind} file exceeds the parse limit of {limit:,} {unit}. "
            "Split it into smaller files or remove unused rows and columns, then try again.",
            code="parse_limit_exceeded",
        )


def check_csv_size_within_limits(file_path: Path, limits: TableLimits) -> None:
    # Decoding never yields more characters than the file has bytes.
    _check_table_limit("CSV", file_path.stat().st_size, limits.max_decoded_bytes, "bytes")


def read_csv_rows_within_limits(reader: csv.DictReader[str], limits: TableLimits) -> list[dict[str, Any]]:
    # Duplicate header names collapse into one dict key, so fields are counted from the header width.
    width = len(reader.fieldnames or ())
    rows: list[dict[str, Any]] = []
    cells = 0
    # Each record is measured as the JSON object the parser emits: every record repeats the header names, and extra
    # fields sit under a None key that serializes as "null". The 2 is the array's brackets.
    output_bytes = 2
    for row in reader:
        overflow = row.get(None)
        cells += width + (len(overflow) if isinstance(overflow, list) else 0)
        # The row and cell limits come first, so an over-wide row is rejected before it is encoded.
        _check_table_limit("CSV", len(rows) + 1, limits.max_rows, "rows")
        _check_table_limit("CSV", cells, limits.max_cells, "cells")
        output_bytes += _encoded_size_within(row, limits.max_decoded_bytes - output_bytes) + (1 if rows else 0)
        _check_table_limit("CSV", output_bytes, limits.max_decoded_bytes, "bytes")
        rows.append(row)
    return rows


def check_workbook_within_limits(file_path: Path, limits: TableLimits) -> None:
    # A zipped workbook (.xlsx, .xlsm, .xlsb, .ods) decodes to its members' total size; .xls is stored uncompressed.
    if zipfile.is_zipfile(file_path):
        try:
            with zipfile.ZipFile(file_path) as archive:
                decoded_bytes = sum(info.file_size for info in archive.infolist())
        except zipfile.BadZipFile as exc:
            raise ParserFailure(f"Invalid Excel file format: {exc}") from exc
    else:
        decoded_bytes = file_path.stat().st_size
    _check_table_limit("Excel", decoded_bytes, limits.max_decoded_bytes, "bytes")


def _parse_csv(file_path: Path, limits: TableLimits) -> list[dict[str, Any]]:
    delimiter, encoding = _sniff_csv_delimiter(file_path, limits)
    with file_path.open(encoding=encoding, errors="replace", newline="") as file:
        return read_csv_rows_within_limits(csv.DictReader(file, delimiter=delimiter), limits)


def _parse_excel(file_path: Path, worksheet: str | None, limits: TableLimits) -> list[dict[str, Any]]:
    check_workbook_within_limits(file_path, limits)
    if worksheet:
        with pd.ExcelFile(file_path, engine="calamine") as workbook:
            if worksheet not in workbook.sheet_names:
                raise ParserFailure(
                    f'Worksheet "{worksheet}" was not found in the Excel file.', code="worksheet_not_found"
                )
    try:
        dataframe = pd.read_excel(
            file_path,
            sheet_name=worksheet or 0,
            engine="calamine",
            keep_default_na=False,
        ).replace("", pd.NA)
    except ParserFailure:
        raise
    except Exception as exc:
        raise ParserFailure(f"Failed to parse Excel file: {exc}") from exc
    # pandas builds the frame from A1 to the last cell holding a value, so its shape is the sheet's full extent.
    rows, columns = dataframe.shape
    _check_table_limit("Excel", rows, limits.max_rows, "rows")
    _check_table_limit("Excel", (rows + 1) * columns, limits.max_cells, "cells")
    return _clean_dataframe_for_json(dataframe)


def _rebuilt_value_size(value: object) -> int:
    # json.loads reuses CPython's small ints and the True/False/None singletons, so they add no object per cell.
    if value is None or isinstance(value, bool) or (isinstance(value, int) and -5 <= value <= 256):
        return 0
    return sys.getsizeof(value)


def _excel_json(file_path: Path, worksheet: str | None, limits: TableLimits) -> bytes:
    rows = _parse_excel(file_path, worksheet, limits)
    # The worker rebuilds these rows from the JSON, and short cells take far more memory as objects than as JSON.
    decoded = sum(sys.getsizeof(row) + sum(_rebuilt_value_size(value) for value in row.values()) for row in rows)
    _check_table_limit("Excel", decoded, limits.max_decoded_bytes, "bytes")
    data = json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()
    # Cells sharing one stored string each carry their own copy here, so the output can outgrow the file.
    _check_table_limit("Excel", len(data), limits.max_decoded_bytes, "bytes")
    return data


def validate_excel_plain(file_path: str, worksheet: str | None, limits: dict[str, int]) -> dict[str, Any]:
    """Capped-subprocess entry: check the limits that need no parse, then read one row of the sheet to be parsed."""
    try:
        check_workbook_within_limits(Path(file_path), TableLimits(**limits))
        with pd.ExcelFile(file_path, engine="calamine") as workbook:
            # A missing worksheet is reported by the parse step, which names only the requested sheet.
            if worksheet and worksheet not in workbook.sheet_names:
                return {}
            pd.read_excel(workbook, sheet_name=worksheet or 0, nrows=1)
    except ParserFailure as exc:
        return {"error_code": exc.code, "error_message": str(exc)}
    except Exception as exc:
        return {"error_code": "invalid_file", "error_message": str(exc)}
    return {}


def parse_excel_plain(file_path: str, worksheet: str | None, limits: dict[str, int]) -> dict[str, Any]:
    """Capped-subprocess entry: the parsed rows as JSON bytes, or the failure as plain data."""
    try:
        return {"data": _excel_json(Path(file_path), worksheet, TableLimits(**limits))}
    except ParserFailure as exc:
        return {"error_code": exc.code, "error_message": str(exc)}


def _parse_docx(file_path: Path, max_tokens: int) -> tuple[str, bool, int]:
    try:
        document = docx.Document(file_path)
        text_parts: list[str] = []
        current_tokens = 0
        truncated = False
        for paragraph in document.paragraphs:
            if not paragraph.text.strip():
                continue
            paragraph_tokens = count_tokens(paragraph.text)
            if max_tokens and current_tokens + paragraph_tokens > max_tokens:
                truncated = True
                break
            text_parts.append(paragraph.text)
            current_tokens += paragraph_tokens
        if not truncated:
            for table in document.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if not row_text:
                        continue
                    row_tokens = count_tokens(row_text)
                    if max_tokens and current_tokens + row_tokens > max_tokens:
                        truncated = True
                        break
                    text_parts.append(row_text)
                    current_tokens += row_tokens
                if truncated:
                    break
        return sanitize_postgres_text("\n".join(text_parts)), truncated, current_tokens
    except ParserFailure:
        raise
    except Exception as exc:
        raise ParserFailure(f"Failed to parse DOCX file: {exc}") from exc


def _page_image_over_caps(image: bytes, limits: ParserLimits) -> bool:
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    return len(image) > limits.max_page_image_bytes or max(width, height) > limits.max_page_image_edge_px


def _render_pdf_pages(file_path: Path, limits: ParserLimits) -> list[bytes]:
    started_at = monotonic()
    try:
        images = render_pdf_pages_as_images(
            str(file_path),
            file_identifier=file_path.name,
            max_pages=limits.max_pdf_ocr_pages,
            resolution=PDF_OCR_RENDER_RESOLUTION,
            timeout_seconds=limits.timeout_seconds,
            memory_limit_bytes=limits.pdf_memory_bytes,
            max_result_bytes=limits.pdf_result_bytes,
        )
    except CappedSubprocessError as exc:
        remaining = limits.timeout_seconds - (monotonic() - started_at)
        if exc.kind in {"timeout", "no_headroom"} or remaining <= 0:
            raise
    else:
        if not any(_page_image_over_caps(image, limits) for image in images):
            return images
        remaining = limits.timeout_seconds - (monotonic() - started_at)
        if remaining <= 0:
            return images
    return render_pdf_pages_as_images(
        str(file_path),
        file_identifier=file_path.name,
        max_pages=limits.max_pdf_ocr_pages,
        resolution=PDF_OCR_FALLBACK_RENDER_RESOLUTION,
        timeout_seconds=remaining,
        memory_limit_bytes=limits.pdf_memory_bytes,
        max_result_bytes=limits.pdf_result_bytes,
    )


def _parse_pdf(
    file_path: Path, force_ocr: bool, limits: ParserLimits
) -> tuple[ParserKind, list[ParserPart], dict[str, Any]]:
    stats = PdfExtractStats()
    extracted_text = ""
    ocr_reason: str | None = "configured" if force_ocr else None
    if not force_ocr:
        extracted_text = extract_pdf_file(
            str(file_path),
            file_identifier=file_path.name,
            max_tokens=limits.max_tokens,
            stats=stats,
            memory_limit_bytes=limits.pdf_memory_bytes,
        )
        if not extracted_text.strip():
            ocr_reason = "token_limit" if stats.truncated else "empty"
        else:
            ocr_reason = scrambled_text_layer_reason(extracted_text)
        if ocr_reason is None:
            return (
                "text",
                [ParserPart("content.txt", data=extracted_text.encode())],
                {
                    "truncated": stats.truncated,
                    "total_pages": stats.page_count,
                },
            )
    else:
        validate_pdf_file(
            str(file_path),
            file_identifier=file_path.name,
            memory_limit_bytes=limits.pdf_memory_bytes,
        )

    images = _render_pdf_pages(file_path, limits)
    if not images:
        return (
            "text",
            [ParserPart("content.txt", data=extracted_text.encode())],
            {
                "truncated": stats.truncated,
                "total_pages": stats.page_count,
            },
        )
    return (
        "images",
        [ParserPart(f"page-{index}.png", data=image) for index, image in enumerate(images, 1)],
        {
            "ocr_reason": ocr_reason,
            "total_pages": stats.page_count or None,
        },
    )


def _normalize_image(file_path: Path, limits: ParserLimits) -> list[bytes]:
    try:
        with Image.open(file_path) as image:
            frame_count = getattr(image, "n_frames", 1)
            if frame_count > 1 and image.format != "TIFF":
                raise ParserFailure("Only TIFF images may contain multiple frames")
            if frame_count > MAX_IMAGE_BLOCKS:
                raise ParserFailure(f"Image has more than {MAX_IMAGE_BLOCKS} frames")
            total_pixels = 0
            total_bytes = 0
            images: list[bytes] = []
            for frame_index in range(frame_count):
                image.seek(frame_index)
                if image.mode.startswith("I") or image.mode == "F":
                    raise ParserFailure("Image stores more than 8 bits per channel")
                frame_pixels = image.width * image.height
                if frame_pixels > MAX_DECODED_IMAGE_PIXELS:
                    raise ParserFailure(f"Image decodes to more than {MAX_DECODED_IMAGE_PIXELS} pixels")
                total_pixels += frame_pixels
                if total_pixels > MAX_TOTAL_DECODED_IMAGE_PIXELS:
                    raise ParserFailure(
                        f"Image decodes to more than {MAX_TOTAL_DECODED_IMAGE_PIXELS} pixels across frames"
                    )
                normalized = image.convert("RGBA") if image.has_transparency_data else image.convert("RGB")
                output = io.BytesIO()
                normalized.save(output, format="PNG", optimize=False)
                data = output.getvalue()
                total_bytes += len(data)
                if total_bytes > limits.max_output_bytes:
                    raise ParserFailure("Decoded image exceeds the parser output limit", code="output_too_large")
                images.append(data)
    except ParserFailure:
        raise
    except Exception as exc:
        raise ParserFailure(f"File is not a valid image: {exc}") from exc
    return images


def _is_zip_junk_member(member_name: str) -> bool:
    parts = PurePosixPath(member_name).parts
    return (
        not parts
        or any(part in _ZIP_JUNK_DIRS for part in parts)
        or parts[-1] in _ZIP_JUNK_FILES
        or parts[-1].startswith("._")
    )


def _read_zip_entry_count(file_path: Path) -> int | None:
    try:
        tail_size = min(file_path.stat().st_size, _ZIP_EOCD_TAIL_BYTES)
        with file_path.open("rb") as file:
            file.seek(-tail_size, os.SEEK_END)
            tail = file.read(tail_size)
        eocd_index = tail.rfind(b"PK\x05\x06")
        if eocd_index < 0 or eocd_index + 22 > len(tail):
            return None
        count = int.from_bytes(tail[eocd_index + 10 : eocd_index + 12], "little")
        if count != 0xFFFF:
            return count
        zip64_index = tail.rfind(b"PK\x06\x06")
        return None if zip64_index < 0 else int.from_bytes(tail[zip64_index + 32 : zip64_index + 40], "little")
    except (OSError, ValueError):
        return None


def _extract_zip(file_path: Path, scratch_dir: Path) -> list[ParserPart]:
    archive_size = file_path.stat().st_size
    if archive_size > _MAX_ZIP_ARCHIVE_BYTES:
        raise ParserFailure(
            f"ZIP archive size ({archive_size} bytes) exceeds the limit of {_MAX_ZIP_ARCHIVE_BYTES} bytes"
        )
    declared_entries = _read_zip_entry_count(file_path)
    if declared_entries is not None and declared_entries > _MAX_ZIP_ENTRIES:
        raise ParserFailure(
            f"ZIP archive declares {declared_entries} entries, exceeding the limit of {_MAX_ZIP_ENTRIES}"
        )

    extract_dir = scratch_dir / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(file_path) as archive:
        members = [
            member for member in archive.infolist() if not member.is_dir() and not _is_zip_junk_member(member.filename)
        ]
        if len(members) > _MAX_ZIP_ENTRIES:
            raise ParserFailure(f"ZIP archive contains {len(members)} files, exceeding the limit of {_MAX_ZIP_ENTRIES}")
        declared_size = sum(member.file_size for member in members)
        if declared_size > _MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ParserFailure(
                f"ZIP archive uncompressed size ({declared_size} bytes) exceeds the limit of {_MAX_ZIP_UNCOMPRESSED_BYTES} bytes"
            )
        if any(member.flag_bits & 0x1 for member in members):
            raise ParserFailure("Password-protected ZIP archives are not supported")

        measured = 0
        files: dict[Path, ParserPart] = {}
        for member in members:
            if member.file_size > _MAX_ZIP_UNCOMPRESSED_BYTES - measured:
                raise ParserFailure(
                    f"ZIP archive uncompressed content exceeds the limit of {_MAX_ZIP_UNCOMPRESSED_BYTES} bytes"
                )
            extracted = Path(archive.extract(member, path=extract_dir))
            measured += extracted.stat().st_size
            if measured > _MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ParserFailure(
                    f"ZIP archive uncompressed content exceeds the limit of {_MAX_ZIP_UNCOMPRESSED_BYTES} bytes"
                )
            files[extracted] = ParserPart(str(extracted.relative_to(extract_dir)), path=extracted)
    return sorted(files.values(), key=lambda part: part.name)


def parse_file(
    file_path: Path,
    *,
    file_name: str,
    configured_file_type: str,
    worksheet: str | None,
    force_ocr: bool,
    limits: ParserLimits,
    scratch_dir: Path,
) -> ParserResult:
    file_size = file_path.stat().st_size
    if file_size > limits.max_input_bytes:
        raise ParserFailure(
            f"File size ({file_size} bytes) exceeds the parser input limit of {limits.max_input_bytes} bytes",
            code="input_too_large",
        )
    file_type, detection_source = _detect_file_type(file_name, file_path, configured_file_type)

    if file_type == "csv":
        data = json.dumps(_parse_csv(file_path, limits.table), separators=(",", ":"), ensure_ascii=False).encode()
        result = ParserResult(file_type, detection_source, "json", [ParserPart("content.json", data=data)])
    elif file_type == "excel":
        data = _excel_json(file_path, worksheet, limits.table)
        result = ParserResult(file_type, detection_source, "json", [ParserPart("content.json", data=data)])
    elif file_type == "pdf":
        kind, parts, metadata = _parse_pdf(file_path, force_ocr, limits)
        result = ParserResult(file_type, detection_source, kind, parts, metadata)
    elif file_type == "image":
        images = _normalize_image(file_path, limits)
        result = ParserResult(
            file_type,
            detection_source,
            "images",
            [ParserPart(f"page-{index}.png", data=image) for index, image in enumerate(images, 1)],
            {"total_pages": len(images)},
        )
    elif file_type == "docx":
        text, truncated, tokens = _parse_docx(file_path, limits.max_tokens)
        result = ParserResult(
            file_type,
            detection_source,
            "text",
            [ParserPart("content.txt", data=text.encode())],
            {"truncated": truncated, "tokens_included": tokens},
        )
    elif file_type == "zip":
        result = ParserResult(file_type, detection_source, "files", _extract_zip(file_path, scratch_dir))
    else:
        raise ParserFailure(f"Unsupported file type: {file_type}", code="unsupported_type")

    output_size = sum(part.size for part in result.parts)
    if output_size > limits.max_output_bytes:
        raise ParserFailure(
            f"Parser output size ({output_size} bytes) exceeds the limit of {limits.max_output_bytes} bytes",
            code="output_too_large",
        )
    return result


def parse_file_plain(
    file_path: str,
    file_name: str,
    configured_file_type: str,
    worksheet: str | None,
    force_ocr: bool,
    limits: dict[str, Any],
    scratch_dir: str,
) -> dict[str, Any]:
    """Subprocess boundary: return only inert builtins accepted by the capped-process reader."""
    try:
        result = parse_file(
            Path(file_path),
            file_name=file_name,
            configured_file_type=configured_file_type,
            worksheet=worksheet,
            force_ocr=force_ocr,
            limits=ParserLimits(**limits),
            scratch_dir=Path(scratch_dir),
        )
    except ParserFailure as exc:
        return {"error_code": exc.code, "error_message": str(exc)}
    return {
        "file_type": result.file_type,
        "detection_source": result.detection_source,
        "kind": result.kind,
        "metadata": result.metadata,
        "parts": [
            {
                "name": part.name,
                "data": part.data,
                "path": str(part.path) if part.path is not None else None,
            }
            for part in result.parts
        ],
    }
