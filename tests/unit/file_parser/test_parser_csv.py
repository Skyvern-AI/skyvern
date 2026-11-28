from __future__ import annotations

import codecs
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from skyvern.constants import PDF_OCR_MAX_PAGE_IMAGE_BYTES, PDF_OCR_MAX_PAGE_IMAGE_EDGE_PX
from skyvern.file_parser.parser import ParserLimits, parse_file_plain
from skyvern.file_parser.transport import (
    MAX_PARSER_INPUT_BYTES,
    MAX_PARSER_OUTPUT_BYTES,
    MAX_PARSER_PDF_MEMORY_BYTES,
    MAX_PARSER_PDF_PAGES,
    MAX_PARSER_PDF_RESULT_BYTES,
    MAX_PARSER_TIMEOUT_SECONDS,
    MAX_PARSER_TOKENS,
)

_LIMITS = asdict(
    ParserLimits(
        max_input_bytes=MAX_PARSER_INPUT_BYTES,
        max_output_bytes=MAX_PARSER_OUTPUT_BYTES,
        max_tokens=MAX_PARSER_TOKENS,
        max_pdf_ocr_pages=MAX_PARSER_PDF_PAGES,
        pdf_memory_bytes=MAX_PARSER_PDF_MEMORY_BYTES,
        pdf_result_bytes=MAX_PARSER_PDF_RESULT_BYTES,
        timeout_seconds=MAX_PARSER_TIMEOUT_SECONDS,
        max_page_image_bytes=PDF_OCR_MAX_PAGE_IMAGE_BYTES,
        max_page_image_edge_px=PDF_OCR_MAX_PAGE_IMAGE_EDGE_PX,
    )
)

_UTF8_BOM = b"\xef\xbb\xbf"

# (file name, raw bytes, rows the parser must return)
_CSV_PARSE_CASES: list[tuple[str, bytes, list[dict[str, str]]]] = [
    (
        "emails.csv",
        b"email\nana@x.com\nbo@y.com\ncy@z.com\n",
        [{"email": "ana@x.com"}, {"email": "bo@y.com"}, {"email": "cy@z.com"}],
    ),
    ("names.csv", b"name\nAna Smith\nBo Jones\n", [{"name": "Ana Smith"}, {"name": "Bo Jones"}]),
    ("names.tsv", b"name\nAna Smith\nBo Jones\n", [{"name": "Ana Smith"}, {"name": "Bo Jones"}]),
    ("bom_comma.csv", _UTF8_BOM + b"name,email\nAna,ana@x.com\n", [{"name": "Ana", "email": "ana@x.com"}]),
    ("bom_semicolon.csv", _UTF8_BOM + b"name;city\nAna;Paris\n", [{"name": "Ana", "city": "Paris"}]),
    ("bom_one_column.csv", _UTF8_BOM + b"email\nana@x.com\n", [{"email": "ana@x.com"}]),
    ("bom_non_ascii.csv", _UTF8_BOM + "name,city\nJosé,Zürich\n".encode(), [{"name": "José", "city": "Zürich"}]),
    (
        "people.csv",
        b"full name\nAna Maria Smith\nBo Jo Jones\n",
        [{"full name": "Ana Maria Smith"}, {"full name": "Bo Jo Jones"}],
    ),
    ("files.csv", b"file\na.pdf\nb.pdf\n", [{"file": "a.pdf"}, {"file": "b.pdf"}]),
    ("accented.csv", "name\nJosé Núñez\nZoë Ågren\n".encode(), [{"name": "José Núñez"}, {"name": "Zoë Ågren"}]),
    (
        "bom_quoted_header.csv",
        _UTF8_BOM + b'"name","email"\n"Ana","ana@x.com"\n',
        [{"name": "Ana", "email": "ana@x.com"}],
    ),
    # Controls: files the parser already read correctly must not change.
    ("spaces.csv", b"a b\n1 2\n3 4\n", [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]),
    ("colons.csv", b"a:b\n1:2\n3:4\n", [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]),
    ("caret.csv", b"a^b\n1^2\n3^4\n", [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]),
    ("tilde.csv", b"a~b\n1~2\n3~4\n", [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]),
    ("header_only.csv", b"email\n", []),
    ("comma.csv", b"name,email\nAna,ana@x.com\n", [{"name": "Ana", "email": "ana@x.com"}]),
    ("semicolon.csv", b"name;amount\nAna;1,50\n", [{"name": "Ana", "amount": "1,50"}]),
    ("tabs.tsv", b"name\tcity\nAna\tParis\n", [{"name": "Ana", "city": "Paris"}]),
    ("pipe.csv", b"name|city\nAna|Paris\n", [{"name": "Ana", "city": "Paris"}]),
    ("quoted.csv", b'name,address\n"Ana","1 Main St, Apt 2"\n', [{"name": "Ana", "address": "1 Main St, Apt 2"}]),
    ("urls.csv", b"url\nhttps://a.com/p\nhttps://b.com/q\n", [{"url": "https://a.com/p"}, {"url": "https://b.com/q"}]),
    ("utf16.csv", "name,city\nJosé,Zürich\n".encode("utf-16"), [{"name": "José", "city": "Zürich"}]),
]


def _parse(tmp_path: Path, file_name: str, content: bytes) -> dict[str, Any]:
    path = tmp_path / file_name
    path.write_bytes(content)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    return parse_file_plain(str(path), file_name, "csv", None, False, _LIMITS, str(scratch_dir))


@pytest.mark.parametrize(
    ("file_name", "content", "expected_rows"), _CSV_PARSE_CASES, ids=[case[0] for case in _CSV_PARSE_CASES]
)
def test_csv_parse_returns_the_rows_in_the_file(
    tmp_path: Path, file_name: str, content: bytes, expected_rows: list[dict[str, str]]
) -> None:
    result = _parse(tmp_path, file_name, content)

    assert result["file_type"] == "csv"
    (part,) = result["parts"]
    assert json.loads(part["data"]) == expected_rows


@pytest.mark.parametrize("bom", [_UTF8_BOM, codecs.BOM_UTF16_LE], ids=["utf8", "utf16"])
def test_bom_only_csv_is_rejected_as_empty(tmp_path: Path, bom: bytes) -> None:
    """A BOM with no text is an empty file, as a 0-byte file already is."""
    result = _parse(tmp_path, "empty.csv", bom)

    assert result == {"error_code": "invalid_file", "error_message": "File is empty"}
