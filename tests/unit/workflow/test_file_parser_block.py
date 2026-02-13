"""
Tests for FileParserBlock DOCX support.

Covers file type detection, validation, text extraction (paragraphs + tables),
token truncation, and error handling for DOCX files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import docx
import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidFileType
from skyvern.forge.sdk.workflow.models.block import BlockType, FileParserBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import FileType


def _make_output_parameter(key: str) -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test",
        output_parameter_id="test-output-id",
        workflow_id="test-workflow-id",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_file_parser_block(file_url: str, file_type: FileType) -> FileParserBlock:
    return FileParserBlock(
        label="test_file_parser",
        block_type=BlockType.FILE_URL_PARSER,
        output_parameter=_make_output_parameter("test_output"),
        file_url=file_url,
        file_type=file_type,
    )


def _create_docx(
    path: Path,
    paragraphs: list[str] | None = None,
    table_rows: list[list[str]] | None = None,
) -> Path:
    """Create a DOCX file with optional paragraphs and tables."""
    doc = docx.Document()
    if paragraphs:
        for text in paragraphs:
            doc.add_paragraph(text)
    if table_rows:
        cols = len(table_rows[0])
        table = doc.add_table(rows=len(table_rows), cols=cols)
        for i, row_data in enumerate(table_rows):
            for j, cell_text in enumerate(row_data):
                table.rows[i].cells[j].text = cell_text
    doc.save(str(path))
    return path


class TestDetectFileTypeFromUrl:
    """Tests for _detect_file_type_from_url with DOCX extensions."""

    def _detect(self, url: str) -> FileType:
        block = _make_file_parser_block(url, FileType.CSV)
        return block._detect_file_type_from_url(url)

    def test_docx_extension(self) -> None:
        assert self._detect("https://example.com/file.docx") == FileType.DOCX

    def test_doc_extension_raises_error(self) -> None:
        # Legacy .doc (Word 97-2003) is not supported by python-docx
        with pytest.raises(InvalidFileType, match="Legacy .doc format"):
            self._detect("https://example.com/file.doc")

    def test_docx_with_query_params(self) -> None:
        assert self._detect("https://example.com/file.docx?token=abc&v=1") == FileType.DOCX

    def test_docx_case_insensitive(self) -> None:
        assert self._detect("https://example.com/file.DOCX") == FileType.DOCX

    def test_other_extensions_unchanged(self) -> None:
        assert self._detect("https://example.com/file.pdf") == FileType.PDF
        assert self._detect("https://example.com/file.xlsx") == FileType.EXCEL
        assert self._detect("https://example.com/file.csv") == FileType.CSV
        assert self._detect("https://example.com/file.png") == FileType.IMAGE


class TestValidateFileType:
    """Tests for validate_file_type with DOCX files."""

    def test_valid_docx(self, tmp_path: Path) -> None:
        path = _create_docx(tmp_path / "valid.docx", paragraphs=["Hello"])
        block = _make_file_parser_block("https://example.com/valid.docx", FileType.DOCX)
        # Should not raise
        block.validate_file_type("https://example.com/valid.docx", str(path))

    def test_plain_text_with_docx_extension(self, tmp_path: Path) -> None:
        path = tmp_path / "fake.docx"
        path.write_text("This is plain text, not a DOCX file.")
        block = _make_file_parser_block("https://example.com/fake.docx", FileType.DOCX)
        with pytest.raises(InvalidFileType):
            block.validate_file_type("https://example.com/fake.docx", str(path))

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.docx"
        path.write_bytes(b"")
        block = _make_file_parser_block("https://example.com/empty.docx", FileType.DOCX)
        with pytest.raises(InvalidFileType):
            block.validate_file_type("https://example.com/empty.docx", str(path))


@pytest.mark.asyncio
class TestParseDocxFile:
    """Tests for _parse_docx_file text extraction."""

    async def test_paragraphs_joined_by_newline(self, tmp_path: Path) -> None:
        path = _create_docx(tmp_path / "paras.docx", paragraphs=["Hello", "World"])
        block = _make_file_parser_block("https://example.com/paras.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == "Hello\nWorld"

    async def test_empty_paragraphs_skipped(self, tmp_path: Path) -> None:
        path = _create_docx(tmp_path / "blanks.docx", paragraphs=["Hello", "", "   ", "World"])
        block = _make_file_parser_block("https://example.com/blanks.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == "Hello\nWorld"

    async def test_table_rows_formatted_with_pipe(self, tmp_path: Path) -> None:
        path = _create_docx(
            tmp_path / "table.docx",
            table_rows=[["Name", "Age"], ["Alice", "30"]],
        )
        block = _make_file_parser_block("https://example.com/table.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == "Name | Age\nAlice | 30"

    async def test_mixed_paragraphs_and_tables(self, tmp_path: Path) -> None:
        path = _create_docx(
            tmp_path / "mixed.docx",
            paragraphs=["Intro"],
            table_rows=[["Col1", "Col2"], ["A", "B"]],
        )
        block = _make_file_parser_block("https://example.com/mixed.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == "Intro\nCol1 | Col2\nA | B"

    async def test_empty_document(self, tmp_path: Path) -> None:
        path = _create_docx(tmp_path / "empty.docx")
        block = _make_file_parser_block("https://example.com/empty.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == ""

    async def test_empty_table_cells_skipped(self, tmp_path: Path) -> None:
        path = _create_docx(
            tmp_path / "sparse.docx",
            table_rows=[["Name", "", "Age"], ["", "", ""]],
        )
        block = _make_file_parser_block("https://example.com/sparse.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        # First row: "Name" and "Age" (empty cell skipped), second row: all empty -> skipped
        assert result == "Name | Age"

    async def test_multiple_tables(self, tmp_path: Path) -> None:
        doc = docx.Document()
        t1 = doc.add_table(rows=1, cols=2)
        t1.rows[0].cells[0].text = "T1C1"
        t1.rows[0].cells[1].text = "T1C2"
        t2 = doc.add_table(rows=1, cols=2)
        t2.rows[0].cells[0].text = "T2C1"
        t2.rows[0].cells[1].text = "T2C2"
        path = tmp_path / "multi_table.docx"
        doc.save(str(path))

        block = _make_file_parser_block("https://example.com/multi_table.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path))
        assert result == "T1C1 | T1C2\nT2C1 | T2C2"


@pytest.mark.asyncio
class TestParseDocxFileTokenTruncation:
    """Tests for _parse_docx_file token limit enforcement."""

    async def test_paragraphs_truncated(self, tmp_path: Path) -> None:
        # Create many paragraphs that will exceed a small token limit
        paragraphs = [f"This is paragraph number {i} with some text content." for i in range(100)]
        path = _create_docx(tmp_path / "long.docx", paragraphs=paragraphs)
        block = _make_file_parser_block("https://example.com/long.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path), max_tokens=20)
        lines = result.split("\n")
        assert len(lines) < len(paragraphs)
        # Each included line should be a valid paragraph
        for line in lines:
            assert line.startswith("This is paragraph number")

    async def test_tables_truncated(self, tmp_path: Path) -> None:
        table_rows = [[f"R{i}C1", f"R{i}C2", f"R{i}C3"] for i in range(100)]
        path = _create_docx(tmp_path / "big_table.docx", table_rows=table_rows)
        block = _make_file_parser_block("https://example.com/big_table.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path), max_tokens=20)
        lines = result.split("\n")
        assert len(lines) < len(table_rows)

    async def test_tables_skipped_when_paragraphs_exhaust_budget(self, tmp_path: Path) -> None:
        paragraphs = [f"Long paragraph {i} with lots of content to fill tokens." for i in range(100)]
        table_rows = [["Should", "Not", "Appear"]]
        path = _create_docx(tmp_path / "para_heavy.docx", paragraphs=paragraphs, table_rows=table_rows)
        block = _make_file_parser_block("https://example.com/para_heavy.docx", FileType.DOCX)
        result = await block._parse_docx_file(str(path), max_tokens=20)
        assert "Should" not in result
        assert "Not" not in result
        assert "Appear" not in result


@pytest.mark.asyncio
class TestParseDocxFileErrorHandling:
    """Tests for _parse_docx_file error handling."""

    async def test_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.docx"
        path.write_bytes(b"\x00\x01\x02\x03random bytes")
        block = _make_file_parser_block("https://example.com/corrupt.docx", FileType.DOCX)
        with pytest.raises(InvalidFileType):
            await block._parse_docx_file(str(path))

    async def test_nonexistent_file(self, tmp_path: Path) -> None:
        block = _make_file_parser_block("https://example.com/missing.docx", FileType.DOCX)
        with pytest.raises(InvalidFileType):
            await block._parse_docx_file(str(tmp_path / "nonexistent.docx"))
