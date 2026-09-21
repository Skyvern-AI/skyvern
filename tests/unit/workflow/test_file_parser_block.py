"""
Tests for FileParserBlock DOCX support.

Covers file type detection, validation, text extraction (paragraphs + tables),
token truncation, and error handling for DOCX files.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock

import docx
import pandas as pd
import pytest
import structlog

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.workflow.exceptions import FileParseTimeout, InvalidFileType
from skyvern.forge.sdk.workflow.models.block import BlockType, FileParserBlock, PDFParserBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockResult, BlockStatus, FileType


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


def _make_pdf_parser_block(file_url: str) -> PDFParserBlock:
    return PDFParserBlock(
        label="test_pdf_parser",
        block_type=BlockType.PDF_PARSER,
        output_parameter=_make_output_parameter("test_output"),
        file_url=file_url,
    )


def _mock_workflow_run_context() -> MagicMock:
    ctx = MagicMock()
    ctx.has_parameter.return_value = False
    ctx.has_value.return_value = False
    ctx.register_output_parameter_value_post_execution = AsyncMock()
    return ctx


async def _execute_with_downloaded_file(
    block: FileParserBlock, file_path: Path, monkeypatch: pytest.MonkeyPatch
) -> BlockResult:
    workflow_run_context = _mock_workflow_run_context()
    monkeypatch.setattr(
        FileParserBlock,
        "get_workflow_run_context",
        lambda _self, _workflow_run_id: workflow_run_context,
    )
    monkeypatch.setattr(
        block_module,
        "resolve_local_or_download_file",
        AsyncMock(return_value=str(file_path)),
    )
    return await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
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


_OLE_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class TestDetectFileTypeFromUrl:
    """Tests for _detect_file_type_from_url with DOCX extensions."""

    def _detect(self, url: str, file_path: str | None = None) -> FileType:
        block = _make_file_parser_block(url, FileType.CSV)
        return block._detect_file_type_from_url(url, file_path=file_path)

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

    def test_no_extension_without_file_path_falls_back_to_csv(self) -> None:
        assert self._detect("https://example.com/34371136523") == FileType.CSV

    def test_no_extension_with_pdf_file_detected_as_pdf(self, tmp_path: Path) -> None:
        # Create a minimal valid PDF file
        pdf_path = tmp_path / "no_ext_file"
        pdf_path.write_bytes(b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF")
        assert self._detect("https://example.com/34371136523", file_path=str(pdf_path)) == FileType.PDF

    def test_no_extension_with_unknown_file_falls_back_to_csv(self, tmp_path: Path) -> None:
        # Plain text file — filetype.guess returns None for text
        txt_path = tmp_path / "unknown_file"
        txt_path.write_text("just,some,csv,data\n1,2,3,4")
        assert self._detect("https://example.com/some_file", file_path=str(txt_path)) == FileType.CSV

    def test_query_params_only_url_with_pdf_file(self, tmp_path: Path) -> None:
        # URL like /download?id=123 — no file extension visible
        pdf_path = tmp_path / "downloaded"
        pdf_path.write_bytes(b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF")
        assert self._detect("https://example.com/download?id=123", file_path=str(pdf_path)) == FileType.PDF

    def test_no_extension_with_legacy_doc_magic_raises(self, tmp_path: Path) -> None:
        # OLE CFB header + the Word marker at byte 512: filetype reports application/msword
        doc_path = tmp_path / "drive_download"
        doc_path.write_bytes(_OLE_CFB_MAGIC + b"\x00" * 504 + b"\xec\xa5\xc1\x00" + b"\x00" * 60)
        with pytest.raises(InvalidFileType, match="Legacy .doc format"):
            self._detect("https://example.com/download?id=123", file_path=str(doc_path))

    def test_no_extension_with_generic_ole_header_raises(self, tmp_path: Path) -> None:
        # OLE CFB header without a recognizable sector marker: filetype.guess returns None
        ole_path = tmp_path / "generic_ole"
        ole_path.write_bytes(_OLE_CFB_MAGIC + b"\x00" * 568)
        with pytest.raises(InvalidFileType, match="Legacy .doc format"):
            self._detect("https://example.com/download?id=123", file_path=str(ole_path))

    def test_no_extension_with_legacy_xls_detected_as_excel(self, tmp_path: Path) -> None:
        # Legacy .xls is also an OLE container but has a dedicated parser, so it must not be rejected
        xls_path = tmp_path / "legacy_xls"
        xls_path.write_bytes(_OLE_CFB_MAGIC + b"\x00" * 504 + b"\x09\x08\x10\x00\x00\x06\x05\x00" + b"\x00" * 56)
        assert self._detect("https://example.com/download?id=123", file_path=str(xls_path)) == FileType.EXCEL


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
class TestFileParserBlockAutoDetectExecution:
    async def test_docx_downloaded_from_extensionless_url_is_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _create_docx(tmp_path / "downloaded", paragraphs=["Hello", "World"])
        block = _make_file_parser_block("https://example.com/download?id=123", FileType.AUTO_DETECT)

        result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        assert result.output_parameter_value == {"value": "Hello\nWorld"}

    async def test_legacy_doc_reports_conversion_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "document.doc"
        path.write_bytes(b"not a real legacy document")
        block = _make_file_parser_block("https://example.com/document.doc", FileType.AUTO_DETECT)

        result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is False
        assert "Legacy .doc format" in result.failure_reason
        assert "convert the file to .docx" in result.failure_reason

    async def test_unknown_binary_reports_auto_detect_not_csv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "downloaded"
        path.write_bytes(b"\x0cplain text with a null byte\x00")
        block = _make_file_parser_block("https://example.com/download?id=123", FileType.AUTO_DETECT)

        result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is False
        assert "not a valid auto-detect file" in result.failure_reason
        assert "not a valid csv file" not in result.failure_reason
        assert "File contains binary data" in result.failure_reason

    async def test_legacy_csv_default_unknown_binary_reports_auto_detect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "downloaded"
        path.write_bytes(b"\x0cplain text with a null byte\x00")
        block = _make_file_parser_block("https://example.com/download?id=123", FileType.CSV)

        result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is False
        assert "not a valid auto-detect file" in result.failure_reason
        assert "not a valid csv file" not in result.failure_reason


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


class TestExtractFileUrlFromBlockOutput:
    """Tests for _extract_file_url_from_block_output – unstructured block output parsing."""

    def _extract(self, value: object) -> str | None:
        return FileParserBlock._extract_file_url_from_block_output(value)

    # --- dict inputs ---

    def test_dict_with_downloaded_files_returns_first_url(self) -> None:
        value = {"downloaded_files": [{"url": "https://example.com/file.pdf", "checksum": None}]}
        assert self._extract(value) == "https://example.com/file.pdf"

    def test_dict_multiple_downloaded_files_returns_first(self) -> None:
        value = {
            "downloaded_files": [
                {"url": "https://example.com/first.pdf"},
                {"url": "https://example.com/second.pdf"},
            ]
        }
        assert self._extract(value) == "https://example.com/first.pdf"

    def test_dict_with_extra_fields_still_extracts_url(self) -> None:
        value = {
            "extracted_information": {"key": "value"},
            "downloaded_files": [{"url": "https://s3.amazonaws.com/bucket/report.xlsx", "filename": "report.xlsx"}],
        }
        assert self._extract(value) == "https://s3.amazonaws.com/bucket/report.xlsx"

    def test_dict_empty_downloaded_files_returns_none(self) -> None:
        assert self._extract({"downloaded_files": []}) is None

    def test_dict_missing_downloaded_files_returns_none(self) -> None:
        assert self._extract({"extracted_information": {"foo": "bar"}}) is None

    def test_dict_downloaded_files_item_missing_url_returns_none(self) -> None:
        assert self._extract({"downloaded_files": [{"filename": "file.pdf"}]}) is None

    def test_dict_downloaded_files_item_empty_url_returns_none(self) -> None:
        assert self._extract({"downloaded_files": [{"url": ""}]}) is None

    # --- JSON string inputs ---

    def test_json_string_with_downloaded_files_returns_url(self) -> None:
        value = json.dumps({"downloaded_files": [{"url": "https://example.com/file.csv"}]})
        assert self._extract(value) == "https://example.com/file.csv"

    def test_json_string_without_downloaded_files_returns_none(self) -> None:
        value = json.dumps({"extracted_information": {"k": "v"}})
        assert self._extract(value) is None

    # --- Python dict repr strings (produced by Jinja {{ block_output }} rendering) ---

    def test_python_repr_string_with_downloaded_files_returns_url(self) -> None:
        value = "{'downloaded_files': [{'url': 'https://example.com/report.pdf', 'checksum': None}]}"
        assert self._extract(value) == "https://example.com/report.pdf"

    def test_python_repr_string_without_downloaded_files_returns_none(self) -> None:
        value = "{'extracted_information': {'a': 1}}"
        assert self._extract(value) is None

    # --- Plain URL strings (should not be extracted, returns None) ---

    def test_plain_url_string_returns_none(self) -> None:
        assert self._extract("https://example.com/file.pdf") is None

    def test_plain_string_returns_none(self) -> None:
        assert self._extract("not a url or dict") is None

    # --- Other types ---

    def test_none_returns_none(self) -> None:
        assert self._extract(None) is None

    def test_list_returns_none(self) -> None:
        assert self._extract([{"url": "https://example.com/file.pdf"}]) is None

    def test_integer_returns_none(self) -> None:
        assert self._extract(42) is None


@pytest.mark.asyncio
class TestExtractWithAiSerialization:
    """Tests for _extract_with_ai content serialization."""

    async def test_list_content_serialized_as_compact_json(self) -> None:
        """CSV/Excel data (list[dict]) must use compact JSON to minimize tokens."""
        block = _make_file_parser_block("https://example.com/data.xlsx", FileType.EXCEL)
        block.json_schema = {"type": "object"}
        records = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]

        with pytest.MonkeyPatch.context() as mp:
            mock_handler = AsyncMock(return_value={})
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: mock_handler,
            )
            mock_load = MagicMock(return_value="prompt")
            mp.setattr("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", mock_load)

            await block._extract_with_ai(records, MagicMock())

            _, kwargs = mock_load.call_args
            content_str = kwargs["extracted_text_content"]

            assert content_str == json.dumps(records, separators=(",", ":"))
            assert json.loads(content_str) == records

    async def test_string_content_passed_unchanged(self) -> None:
        """Non-list content (PDF/DOCX text) must pass through unchanged."""
        block = _make_file_parser_block("https://example.com/doc.pdf", FileType.PDF)
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            mock_handler = AsyncMock(return_value={})
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: mock_handler,
            )
            mock_load = MagicMock(return_value="prompt")
            mp.setattr("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", mock_load)

            await block._extract_with_ai("Hello\nWorld", MagicMock())

            _, kwargs = mock_load.call_args
            assert kwargs["extracted_text_content"] == "Hello\nWorld"


@pytest.mark.asyncio
class TestExtractWithAiTokenBounding:
    """Oversized extraction input must be bounded before it reaches the LLM (SKY-13641)."""

    @staticmethod
    def _patch_llm(mp: pytest.MonkeyPatch) -> MagicMock:
        mock_handler = AsyncMock(return_value={})
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
            lambda *a, **kw: mock_handler,
        )
        mock_load = MagicMock(return_value="prompt")
        mp.setattr("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", mock_load)
        return mock_load

    async def test_oversized_list_content_truncated_before_llm_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {"type": "object"}
        monkeypatch.setattr(block_module, "MAX_FILE_PARSE_INPUT_TOKENS", 50)
        records = [{"column": f"value {i}", "filler": "x" * 50} for i in range(500)]
        raw_dump = json.dumps(records, separators=(",", ":"))

        with pytest.MonkeyPatch.context() as mp:
            mock_load = self._patch_llm(mp)
            await block._extract_with_ai(records, MagicMock())

        _, kwargs = mock_load.call_args
        content_str = kwargs["extracted_text_content"]
        assert len(content_str) < len(raw_dump)
        assert raw_dump.startswith(content_str)

    async def test_oversized_string_content_truncated_before_llm_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block = _make_file_parser_block("https://example.com/doc.pdf", FileType.PDF)
        block.json_schema = {"type": "object"}
        monkeypatch.setattr(block_module, "MAX_FILE_PARSE_INPUT_TOKENS", 50)
        oversized_text = "some repeated file text content " * 2000

        with pytest.MonkeyPatch.context() as mp:
            mock_load = self._patch_llm(mp)
            await block._extract_with_ai(oversized_text, MagicMock())

        _, kwargs = mock_load.call_args
        content_str = kwargs["extracted_text_content"]
        assert len(content_str) < len(oversized_text)
        assert oversized_text.startswith(content_str)

    async def test_content_within_limit_passes_through_unchanged(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            mock_load = self._patch_llm(mp)
            await block._extract_with_ai("name\nAlice", MagicMock())

        _, kwargs = mock_load.call_args
        assert kwargs["extracted_text_content"] == "name\nAlice"


@pytest.mark.asyncio
class TestExtractWithAiSchemaValidation:
    """Tests for schema adherence in FileParserBlock AI extraction."""

    @staticmethod
    def _patch_prompt_and_handler(mp: pytest.MonkeyPatch, handler: AsyncMock, prompt: str = "base prompt") -> None:
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
            lambda *a, **kw: handler,
        )
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            MagicMock(return_value=prompt),
        )

    async def test_array_schema_does_not_force_dict(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
        captured: dict[str, object] = {}

        async def fake_handler(**kwargs: Any) -> list[dict[str, str]]:
            captured["force_dict"] = kwargs["force_dict"]
            return [{"name": "Alice"}]

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_prompt_and_handler(mp, handler)

            result = await block._extract_with_ai("name\nAlice", MagicMock())

        assert captured["force_dict"] is False
        assert result == [{"name": "Alice"}]

    async def test_object_schema_wrong_root_retries_without_prevalidation_coercion(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        responses: list[Any] = [[{"name": "Alice"}], {"name": "Alice"}]
        prompts: list[str] = []
        force_dict_values: list[bool] = []

        async def fake_handler(**kwargs: Any) -> Any:
            prompts.append(kwargs["prompt"])
            force_dict_values.append(kwargs["force_dict"])
            return responses.pop(0)

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_prompt_and_handler(mp, handler)

            result = await block._extract_with_ai("name\nAlice", MagicMock())

        assert result == {"name": "Alice"}
        assert handler.await_count == 2
        assert force_dict_values == [False, False]
        assert "previous response failed JSON schema validation" in prompts[1]
        assert "expected type object, got array" in prompts[1]

    async def test_invalid_schema_fails_before_llm_retry(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {"type": 123}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock()
            self._patch_prompt_and_handler(mp, handler)

            with pytest.raises(ValueError, match="File parser JSON schema is invalid"):
                await block._extract_with_ai("name\nAlice", MagicMock())

        handler.assert_not_awaited()

    async def test_schema_validation_failure_retries_with_sanitized_prompt(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
        secret_like_value = "customer-private-value-" + ("x" * 300)
        responses: list[Any] = [{"name": secret_like_value}, [{"name": "Alice"}]]
        prompts: list[str] = []

        async def fake_handler(**kwargs: Any) -> Any:
            prompts.append(kwargs["prompt"])
            return responses.pop(0)

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_prompt_and_handler(mp, handler)

            result = await block._extract_with_ai("name\nAlice", MagicMock())

        assert result == [{"name": "Alice"}]
        assert handler.await_count == 2
        assert prompts[0] == "base prompt"
        assert "previous response failed JSON schema validation" in prompts[1]
        assert "expected type array, got object" in prompts[1]
        assert secret_like_value not in prompts[1]
        assert "customer-private-value" not in prompts[1]

    async def test_response_format_failure_retries_with_sanitized_prompt(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        raw_bad_response = "not-json-customer-private-value-" + ("x" * 300)
        prompts: list[str] = []

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            prompts.append(kwargs["prompt"])
            if len(prompts) == 1:
                raise InvalidLLMResponseFormat(raw_bad_response)
            return {"name": "Alice"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_prompt_and_handler(mp, handler)

            result = await block._extract_with_ai("name\nAlice", MagicMock())

        assert result == {"name": "Alice"}
        assert handler.await_count == 2
        assert "InvalidLLMResponseFormat" in prompts[1]
        assert raw_bad_response not in prompts[1]
        assert "customer-private-value" not in prompts[1]


@pytest.mark.asyncio
class TestPDFParserSchemaValidation:
    """Tests for schema adherence in the deprecated PDFParserBlock."""

    @staticmethod
    def _patch_execute_dependencies(mp: pytest.MonkeyPatch, block: PDFParserBlock, handler: AsyncMock) -> AsyncMock:
        workflow_run_context = MagicMock()
        workflow_run_context.has_parameter.return_value = False
        record_output_parameter_value = AsyncMock()

        async def fake_build_block_result(self: PDFParserBlock, **kwargs: Any) -> BlockResult:
            kwargs.pop("organization_id", None)
            return BlockResult(output_parameter=self.output_parameter, **kwargs)

        mp.setattr(
            PDFParserBlock, "get_workflow_run_context", staticmethod(lambda workflow_run_id: workflow_run_context)
        )
        mp.setattr(PDFParserBlock, "record_output_parameter_value", record_output_parameter_value)
        mp.setattr(PDFParserBlock, "build_block_result", fake_build_block_result)
        mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value="/tmp/test.pdf"))
        mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", MagicMock(return_value="name\nAlice"))
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            MagicMock(return_value="base prompt"),
        )
        mp.setattr("skyvern.forge.sdk.workflow.models.block.app.LLM_API_HANDLER", handler)
        return record_output_parameter_value

    async def test_execute_retries_schema_validation_without_prevalidation_coercion(self) -> None:
        block = _make_pdf_parser_block("https://example.com/data.pdf")
        block.json_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        responses: list[Any] = [[{"name": "Alice"}], {"name": "Alice"}]
        prompts: list[str] = []
        force_dict_values: list[bool] = []

        async def fake_handler(**kwargs: Any) -> Any:
            prompts.append(kwargs["prompt"])
            force_dict_values.append(kwargs["force_dict"])
            return responses.pop(0)

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            record_output_parameter_value = self._patch_execute_dependencies(mp, block, handler)

            result = await block.execute("workflow-run", "workflow-run-block", organization_id="org-1")

        assert result.success is True
        assert result.status == BlockStatus.completed
        assert result.output_parameter_value == {"name": "Alice"}
        assert handler.await_count == 2
        assert force_dict_values == [False, False]
        assert "previous response failed JSON schema validation" in prompts[1]
        assert "expected type object, got array" in prompts[1]
        record_output_parameter_value.assert_awaited_once()


@pytest.mark.asyncio
class TestOcrPdfPages:
    """Tests for the per-page vision-LLM OCR path for scanned PDFs (SKY-10960)."""

    @staticmethod
    def _patch_handler(mp: pytest.MonkeyPatch, handler: AsyncMock) -> None:
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
            lambda *a, **kw: handler,
        )
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            MagicMock(return_value="prompt"),
        )

    async def test_each_page_transcribed_and_concatenated_in_order(self) -> None:
        """One LLM call per page; every page is concatenated in page order with markers."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)
        page_text = {
            b"img-1": "Cover sheet",
            b"img-2": "Demographics",
            b"img-3": "Provider: JORDAN SAMPLE MD",
        }

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            return {"extracted_text": page_text[image]}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            result = await block._ocr_pdf_pages([b"img-1", b"img-2", b"img-3"])

        # One call per page — the multi-page document is never sent as a single collapsed call.
        assert handler.await_count == 3
        for call in handler.await_args_list:
            assert len(call.kwargs["screenshots"]) == 1
        # Late-page content survives the single-call collapse.
        assert "Provider: JORDAN SAMPLE MD" in result
        assert "Cover sheet" in result and "Demographics" in result
        assert result.index("--- Page 1 ---") < result.index("--- Page 2 ---") < result.index("--- Page 3 ---")

    async def test_invalid_ocr_response_retries_with_sanitized_prompt(self) -> None:
        """A missing extracted_text field is retried without echoing invalid content."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)
        secret_like_value = "customer-private-value-" + ("x" * 300)
        prompts: list[str] = []
        force_dict_values: list[bool] = []

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            prompts.append(kwargs["prompt"])
            force_dict_values.append(kwargs["force_dict"])
            if len(prompts) == 1:
                return {"wrong_field": secret_like_value}
            return {"extracted_text": "Recovered page text"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            result = await block._ocr_pdf_pages([b"img-1"])

        assert handler.await_count == 2
        assert force_dict_values == [False, False]
        assert "Recovered page text" in result
        assert "previous OCR response failed JSON validation" in prompts[1]
        assert "must include extracted_text as a string" in prompts[1]
        assert secret_like_value not in prompts[1]
        assert "customer-private-value" not in prompts[1]

    async def test_order_preserved_when_a_later_page_resolves_first(self) -> None:
        """Output stays in page order even if an earlier page's call finishes last."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            if image == b"slow-1":
                await asyncio.sleep(0.05)
                return {"extracted_text": "first page body"}
            return {"extracted_text": "second page body"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            result = await block._ocr_pdf_pages([b"slow-1", b"fast-2"])

        assert result.index("first page body") < result.index("second page body")

    async def test_failed_page_is_skipped_not_fatal(self) -> None:
        """A per-page OCR failure is logged and skipped; the other pages still extract."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            if image == b"bad":
                raise RuntimeError("vision model timeout")
            return {"extracted_text": f"text for {image.decode()}"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            result = await block._ocr_pdf_pages([b"good-1", b"bad", b"good-3"])

        assert handler.await_count == 3
        assert "text for good-1" in result and "text for good-3" in result
        assert "--- Page 2 ---" not in result

    async def test_all_pages_failed_raises(self) -> None:
        """A total OCR outage (every page errors) propagates instead of returning empty text."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            raise RuntimeError("vision model outage")

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            with pytest.raises(RuntimeError, match="vision model outage"):
                await block._ocr_pdf_pages([b"p1", b"p2", b"p3"])

    async def test_empty_pages_contribute_nothing(self) -> None:
        """Pages that OCR to empty text add neither a marker nor content."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            return {"extracted_text": "" if image == b"blank" else "real content"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)

            result = await block._ocr_pdf_pages([b"blank", b"page-2"])

        assert "--- Page 1 ---" not in result
        assert "--- Page 2 ---" in result and "real content" in result

    async def test_truncates_at_page_boundary_on_token_limit(self) -> None:
        """Concatenation stops at a page boundary once the token budget is exceeded."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            return {"extracted_text": f"content {image.decode()}"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)
            # Each page chunk counts as 10 tokens; a 15-token budget admits only the first page.
            mp.setattr("skyvern.forge.sdk.workflow.models.block.count_tokens", lambda text: 10)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.MAX_FILE_PARSE_INPUT_TOKENS", 15)

            result = await block._ocr_pdf_pages([b"page-1", b"page-2", b"page-3"])

        assert "--- Page 1 ---" in result
        assert "--- Page 2 ---" not in result and "--- Page 3 ---" not in result

    async def test_token_cap_counts_pages_that_reached_the_model_with_an_unknown_total(self) -> None:
        """`pages_included` counts pages whose content is in the produced text (D3-fields-amend).

        A page that rendered but transcribed to nothing is not included, so the count matches
        SKY-15830's record rather than the number of pages iterated over. `total_pages` stays
        unknown: the render step caps at MAX_PDF_OCR_PAGES without reporting the document's page
        count, so any number this code could produce would be the rendered count (D7-review F1,
        option B), and SKY-15830 supplies the real one.
        """
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            return {"extracted_text": "" if image == b"blank" else "real content"}

        with pytest.MonkeyPatch.context() as mp:
            self._patch_handler(mp, AsyncMock(side_effect=fake_handler))
            mp.setattr("skyvern.forge.sdk.workflow.models.block.count_tokens", lambda text: 10)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.MAX_FILE_PARSE_INPUT_TOKENS", 15)
            with structlog.testing.capture_logs() as logs:
                await block._ocr_pdf_pages([b"blank", b"page-2", b"page-3"])

        # Page 1 transcribed to nothing and page 3 was cut, so one page's content is in the output.
        assert block._telemetry.pages_included == 1
        assert block._telemetry.total_pages is None
        assert block._telemetry.limit == "token_limit"
        assert block._telemetry.truncated is True
        # The adjacent WARNING reports the same count from its own expression; without this the two
        # can drift and a dashboard reading the WARNING disagrees with one reading the telemetry.
        (page_cap_warning,) = _events(logs, "PDF OCR text exceeds token limit, truncating at page boundary")
        assert page_cap_warning["pages_included"] == block._telemetry.pages_included
        assert page_cap_warning["pages_rendered"] == 3

    async def test_untruncated_ocr_marks_no_truncation(self) -> None:
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_handler(mp, AsyncMock(return_value={"extracted_text": "content"}))
            await block._ocr_pdf_pages([b"page-1", b"page-2"])

        assert block._telemetry.truncated is False
        assert block._telemetry.limit is None
        assert block._telemetry.truncation_source is None

    async def test_parse_pdf_file_routes_empty_text_to_per_page_ocr(self) -> None:
        """A scanned PDF (no extractable text layer) is routed through per-page OCR."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        async def fake_handler(**kwargs: Any) -> dict[str, str]:
            (image,) = kwargs["screenshots"]
            return {"extracted_text": f"page {image.decode()}"}

        with pytest.MonkeyPatch.context() as mp:
            handler = AsyncMock(side_effect=fake_handler)
            self._patch_handler(mp, handler)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", lambda *a, **kw: "")
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.render_pdf_pages_as_images",
                lambda *a, **kw: [b"A", b"B"],
            )

            result = await block._parse_pdf_file("/tmp/scan.pdf")

        assert handler.await_count == 2
        assert "page A" in result and "page B" in result


BLOCKING_PARSE_SECONDS = 0.3
TICK_INTERVAL_SECONDS = 0.01


async def _count_event_loop_ticks_during(awaitable: Awaitable[Any]) -> tuple[Any, int]:
    """Await ``awaitable`` while a sibling task ticks; returns (result, tick_count).

    Zero ticks means the awaited work held the event loop for its entire duration — the
    same starvation that stops the workflow activity's heartbeat task.
    """
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    # Let the ticker reach its first await before the blocking work starts.
    await asyncio.sleep(0)
    try:
        result = await awaitable
    finally:
        stop.set()
        ticker_task.cancel()
        with suppress(asyncio.CancelledError):
            await ticker_task
    return result, ticks


def _blocking(return_value: Any = None, seconds: float = BLOCKING_PARSE_SECONDS) -> Any:
    def _call(*args: Any, **kwargs: Any) -> Any:
        time.sleep(seconds)
        return return_value

    return _call


def _cpu_bound(return_value: Any = None, seconds: float = BLOCKING_PARSE_SECONDS) -> Any:
    """Like _blocking, but burns CPU in pure Python so it holds the GIL.

    time.sleep releases the GIL; the PDF text extractors do not. Only a busy loop shows
    that offloading keeps the loop responsive against the real workload.
    """

    def _call(*args: Any, **kwargs: Any) -> Any:
        deadline = time.time() + seconds
        while time.time() < deadline:
            pass
        return return_value

    return _call


@pytest.mark.asyncio
class TestParsingDoesNotBlockEventLoop:
    """A slow parse must not hold the event loop.

    The workflow activity heartbeats from an asyncio task on the same loop. Parsing
    synchronously starves that task, and Temporal reaps the whole run once the heartbeat
    timeout elapses, so every blocking parse step has to run off the loop.
    """

    async def test_pdf_text_extraction_yields_to_event_loop(self) -> None:
        block = _make_file_parser_block("https://example.com/large.pdf", FileType.PDF)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.extract_pdf_file",
                _blocking("extracted text"),
            )
            text, ticks = await _count_event_loop_ticks_during(block._parse_pdf_file("/tmp/large.pdf"))

        assert text == "extracted text"
        assert ticks > 0

    async def test_gil_holding_pdf_extraction_yields_to_event_loop(self) -> None:
        """The extractors are pure Python, so they hold the GIL for the whole parse."""
        block = _make_file_parser_block("https://example.com/large.pdf", FileType.PDF)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.extract_pdf_file",
                _cpu_bound("extracted text"),
            )
            text, ticks = await _count_event_loop_ticks_during(block._parse_pdf_file("/tmp/large.pdf"))

        assert text == "extracted text"
        assert ticks > 0

    async def test_docx_parse_yields_to_event_loop(self) -> None:
        block = _make_file_parser_block("https://example.com/large.docx", FileType.DOCX)
        document = MagicMock(paragraphs=[], tables=[])

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("skyvern.forge.sdk.workflow.models.block.docx.Document", _blocking(document))
            _, ticks = await _count_event_loop_ticks_during(block._parse_docx_file("/tmp/large.docx"))

        assert ticks > 0

    async def test_excel_parse_yields_to_event_loop(self) -> None:
        block = _make_file_parser_block("https://example.com/large.xlsx", FileType.EXCEL)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.pd.read_excel",
                _blocking(pd.DataFrame([{"name": "Alice"}])),
            )
            rows, ticks = await _count_event_loop_ticks_during(block._parse_excel_file("/tmp/large.xlsx"))

        assert rows == [{"name": "Alice"}]
        assert ticks > 0

    async def test_csv_parse_yields_to_event_loop(self, tmp_path: Path) -> None:
        block = _make_file_parser_block("https://example.com/large.csv", FileType.CSV)
        csv_path = tmp_path / "large.csv"
        csv_path.write_text("name,city\nAlice,Denver\n")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "_sniff_csv_delimiter", _blocking((",", "utf-8")))
            rows, ticks = await _count_event_loop_ticks_during(block._parse_csv_file(str(csv_path)))

        assert rows == [{"name": "Alice", "city": "Denver"}]
        assert ticks > 0

    async def test_pdf_page_rendering_yields_to_event_loop(self) -> None:
        """The scanned-PDF fallback renders every page to PNG before any LLM call."""
        block = _make_file_parser_block("https://example.com/scan.pdf", FileType.PDF)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", lambda *a, **kw: "")
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.render_pdf_pages_as_images",
                _blocking([]),
            )
            _, ticks = await _count_event_loop_ticks_during(block._parse_pdf_file("/tmp/scan.pdf"))

        assert ticks > 0

    async def test_execute_validation_yields_to_event_loop(self) -> None:
        """Validation opens the document too, so it is as slow as the parse on a large file."""
        block = _make_file_parser_block("https://example.com/large.pdf", FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "get_workflow_run_context", lambda *a, **kw: mock_ctx)
            mp.setattr(FileParserBlock, "format_potential_template_parameters", lambda *a, **kw: None)
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.resolve_local_or_download_file",
                AsyncMock(return_value="/tmp/large.pdf"),
            )
            mp.setattr(FileParserBlock, "validate_file_type", _blocking(None))
            mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", lambda *a, **kw: "text")
            mp.setattr(FileParserBlock, "record_output_parameter_value", AsyncMock())
            mp.setattr(FileParserBlock, "build_block_result", AsyncMock(return_value=MagicMock(success=True)))

            result, ticks = await _count_event_loop_ticks_during(
                block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="org_test")
            )

        assert result.success is True
        assert ticks > 0

    async def test_deprecated_pdf_parser_block_yields_to_event_loop(self) -> None:
        block = _make_pdf_parser_block("https://example.com/large.pdf")
        mock_ctx = _mock_workflow_run_context()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(PDFParserBlock, "get_workflow_run_context", lambda *a, **kw: mock_ctx)
            mp.setattr(PDFParserBlock, "format_potential_template_parameters", lambda *a, **kw: None)
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.resolve_local_or_download_file",
                AsyncMock(return_value="/tmp/large.pdf"),
            )
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.extract_pdf_file",
                _blocking("extracted text"),
            )
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: AsyncMock(return_value={"extracted_information": "ok"}),
            )
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
                MagicMock(return_value="prompt"),
            )
            mp.setattr(PDFParserBlock, "record_output_parameter_value", AsyncMock())
            mp.setattr(PDFParserBlock, "build_block_result", AsyncMock(return_value=MagicMock(success=True)))

            _, ticks = await _count_event_loop_ticks_during(
                block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="org_test")
            )

        assert ticks > 0


@pytest.mark.asyncio
class TestParseStepTimeout:
    """A pathological document must fail its own block instead of consuming the whole run."""

    async def test_pdf_text_extraction_times_out(self) -> None:
        block = _make_file_parser_block("https://example.com/huge.pdf", FileType.PDF)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("skyvern.forge.sdk.workflow.models.block.FILE_PARSE_STEP_TIMEOUT_SECONDS", 0.05)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", _blocking("never returned"))

            with pytest.raises(FileParseTimeout) as exc_info:
                await block._parse_pdf_file("/tmp/huge.pdf")

        assert "huge.pdf" in str(exc_info.value)

    async def test_execute_records_timeout_as_block_failure(self) -> None:
        """The block fails with FILE_PARSER_ERROR; the run survives to its next block."""
        block = _make_file_parser_block("https://example.com/huge.pdf", FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "get_workflow_run_context", lambda *a, **kw: mock_ctx)
            mp.setattr(FileParserBlock, "format_potential_template_parameters", lambda *a, **kw: None)
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.resolve_local_or_download_file",
                AsyncMock(return_value="/tmp/huge.pdf"),
            )
            mp.setattr(FileParserBlock, "validate_file_type", lambda *a, **kw: None)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.FILE_PARSE_STEP_TIMEOUT_SECONDS", 0.05)
            mp.setattr("skyvern.forge.sdk.workflow.models.block.extract_pdf_file", _blocking("never returned"))
            mp.setattr(FileParserBlock, "record_output_parameter_value", AsyncMock())

            captured: dict[str, Any] = {}

            async def fake_build_block_result(*args: Any, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return MagicMock(success=kwargs.get("success"))

            mp.setattr(FileParserBlock, "build_block_result", fake_build_block_result)

            result = await block.execute(
                workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="org_test"
            )

        assert result.success is False
        assert captured["error_codes"] == ["FILE_PARSER_ERROR"]
        assert "timed out" in captured["failure_reason"].lower()

    async def test_validation_timeout_fails_the_block(self) -> None:
        block = _make_file_parser_block("https://example.com/huge.pdf", FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "get_workflow_run_context", lambda *a, **kw: mock_ctx)
            mp.setattr(FileParserBlock, "format_potential_template_parameters", lambda *a, **kw: None)
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.resolve_local_or_download_file",
                AsyncMock(return_value="/tmp/huge.pdf"),
            )
            mp.setattr("skyvern.forge.sdk.workflow.models.block.FILE_PARSE_STEP_TIMEOUT_SECONDS", 0.05)
            mp.setattr(FileParserBlock, "validate_file_type", _blocking(None))
            mp.setattr(FileParserBlock, "record_output_parameter_value", AsyncMock())

            captured: dict[str, Any] = {}

            async def fake_build_block_result(*args: Any, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return MagicMock(success=kwargs.get("success"))

            mp.setattr(FileParserBlock, "build_block_result", fake_build_block_result)

            result = await block.execute(
                workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="org_test"
            )

        assert result.success is False
        assert "timed out" in captured["failure_reason"].lower()
        assert "file type validation" in captured["failure_reason"]


FAILURE_EVENT = "FileParserBlock failed"
COMPLETED_EVENT = "FileParserBlock parse completed"
SCHEMA_FAILED_EVENT = "FileParserBlock extraction LLM response failed schema validation"
SCHEMA_SUCCEEDED_EVENT = "FileParserBlock schema validation succeeded"
CSV_FALLBACK_EVENT = "FileParserBlock fell back to CSV for unrecognized content"


def _events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [log for log in logs if log.get("event") == event]


def _assert_no_content_in_fields(log: dict[str, Any], forbidden: tuple[str, ...]) -> None:
    for key, value in log.items():
        if key in ("event", "log_level"):
            continue
        rendered = json.dumps(value, default=str)
        for needle in forbidden:
            assert needle not in rendered, f"{needle!r} leaked into log field {key!r}"


@pytest.mark.asyncio
class TestFileParserTelemetryLogs:
    """Field-name contracts for the countable parser log lines (SKY-15828)."""

    @staticmethod
    def _patch_extraction(mp: pytest.MonkeyPatch, handler: AsyncMock) -> None:
        # A real key, so `llm_key` assertions discriminate: an AsyncMock's auto-attribute is not a
        # str, so `_handler_llm_key` would return None and a null assertion would prove nothing.
        handler.llm_key = "test-llm-key"
        mp.setattr(FileParserBlock, "_resolve_file_parser_handler", AsyncMock(return_value=handler))
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
            lambda *a, **kw: handler,
        )
        mp.setattr(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            MagicMock(return_value="base prompt"),
        )

    async def test_failure_log_carries_url_free_family_and_run_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "downloaded"
        path.write_bytes(b"\x0cplain text with a null byte\x00")
        block = _make_file_parser_block("https://example.com/private/download?token=abc123", FileType.AUTO_DETECT)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is False
        (failure_log,) = _events(logs, FAILURE_EVENT)
        assert {
            "failure_family",
            "error_codes",
            "configured_file_type",
            "file_type_detected",
            "detection_source",
            "workflow_run_id",
            "workflow_run_block_id",
            "workflow_permanent_id",
            "organization_id",
        } <= failure_log.keys()
        assert failure_log["error_codes"] == ["FILE_PARSER_ERROR"]
        assert failure_log["workflow_run_id"] == "wr_test"
        assert failure_log["detection_source"] == "fallback"
        assert len(failure_log["failure_family"]) <= 60
        assert failure_log["failure_family"].startswith("Failed to download or validate file: ")
        _assert_no_content_in_fields(failure_log, ("example.com", "token=abc123", "plain text"))
        assert not _events(logs, COMPLETED_EVENT)

    async def test_parse_completed_log_fields_without_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data.csv"
        path.write_text("name,city\nAlice,Paris\n")
        block = _make_file_parser_block("https://example.com/export/data.csv", FileType.AUTO_DETECT)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert {
            "configured_file_type",
            "file_type_detected",
            "detection_source",
            "content_tokens",
            "content_tokens_sent",
            "truncated",
            "truncation_source",
            "limit",
            "pages_included",
            "total_pages",
            "tokens_included",
            "token_limit",
            "had_schema",
            "extraction_attempts",
            "llm_key",
            "workflow_run_id",
            "workflow_run_block_id",
            "organization_id",
        } <= completed.keys()
        assert completed["file_type_detected"] == FileType.CSV
        assert completed["configured_file_type"] == FileType.AUTO_DETECT
        assert completed["detection_source"] == "extension"
        assert completed["had_schema"] is False
        assert completed["extraction_attempts"] == 0
        assert completed["truncated"] is False
        assert completed["truncation_source"] is None
        assert completed["content_tokens"] is None
        assert not _events(logs, FAILURE_EVENT)
        _assert_no_content_in_fields(completed, ("example.com", "Alice", "Paris"))

    async def test_explicit_file_type_is_reported_as_explicit_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _create_docx(tmp_path / "downloaded", paragraphs=["Hello"])
        block = _make_file_parser_block("https://example.com/download?id=1", FileType.DOCX)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert completed["file_type_detected"] == FileType.DOCX
        assert completed["detection_source"] == "explicit"

    async def test_parse_completed_with_schema_reports_attempts_tokens_and_truncation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data.csv"
        path.write_text("name,city\n" + "\n".join(f"person{i},city{i}" for i in range(200)) + "\n")
        block = _make_file_parser_block("https://example.com/export/data.csv", FileType.AUTO_DETECT)
        block.json_schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        monkeypatch.setattr(block_module, "MAX_FILE_PARSE_INPUT_TOKENS", 50)
        handler = AsyncMock(side_effect=[{"name": None}, {"name": "person0"}])

        with pytest.MonkeyPatch.context() as mp:
            self._patch_extraction(mp, handler)
            with structlog.testing.capture_logs() as logs:
                result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert completed["had_schema"] is True
        assert completed["extraction_attempts"] == 2
        assert completed["truncated"] is True
        assert completed["truncation_source"] == "platform_cap"
        assert completed["content_tokens"] > 50
        assert completed["content_tokens_sent"] == 50
        assert completed["tokens_included"] == 50
        assert completed["token_limit"] == 50
        assert completed["pages_included"] is None

        (warning,) = _events(logs, SCHEMA_FAILED_EVENT)
        assert warning["failure_class"] == "null_for_non_nullable"
        assert warning["content_truncated"] is True
        assert warning["attempt"] == 1

        (succeeded,) = _events(logs, SCHEMA_SUCCEEDED_EVENT)
        assert {
            "attempt",
            "schema_sha256",
            "recovered_from_failure_class",
            "llm_key",
            "response_root_type",
            "content_truncated",
        } <= succeeded.keys()
        assert succeeded["attempt"] == 2
        assert succeeded["recovered_from_failure_class"] == "null_for_non_nullable"
        # A null here is indistinguishable from a handler that exposes no key, so pin the value.
        assert completed["llm_key"] == "test-llm-key"
        assert succeeded["llm_key"] == "test-llm-key"
        assert succeeded["response_root_type"] == "object"
        _assert_no_content_in_fields(completed, ("person0", "city0"))
        _assert_no_content_in_fields(succeeded, ("person0", "city0"))

    async def test_schema_warning_classifies_required_not_in_properties_without_values(self) -> None:
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = {
            "type": "object",
            "required": ["date_issued", "account_number"],
            "properties": {"account_number_extracted": {"type": "string"}},
        }
        secret_value = "customer-private-value-9f8e7d"
        handler = AsyncMock(return_value={"account_number_extracted": secret_value})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_extraction(mp, handler)
            with structlog.testing.capture_logs() as logs:
                with pytest.raises(ValueError, match="does not match file parser JSON schema"):
                    await block._extract_with_ai("name\nAlice", MagicMock())

        warnings = _events(logs, SCHEMA_FAILED_EVENT)
        assert [w["attempt"] for w in warnings] == [1, 2]
        for warning in warnings:
            assert {
                "failure_class",
                "schema_sha256",
                "required_count",
                "properties_count",
                "response_root_type",
                "response_top_level_keys",
                "llm_key",
                "content_truncated",
                "content_tokens",
                "attempt",
                "will_retry",
            } <= warning.keys()
            assert warning["failure_class"] == "required_not_in_properties"
            assert warning["llm_key"] == "test-llm-key"
            assert warning["required_count"] == 2
            assert warning["properties_count"] == 1
            assert warning["response_root_type"] == "object"
            assert warning["response_top_level_keys"] == ["account_number_extracted"]
            assert warning["unexpected_key_count"] == 0
            assert warning["undeclared_required_count"] == 2
            assert re.fullmatch(r"[0-9a-f]{64}", warning["schema_sha256"])
            assert warning["content_truncated"] is False
            _assert_no_content_in_fields(warning, (secret_value, "Alice"))
        assert not _events(logs, SCHEMA_SUCCEEDED_EVENT)

    async def test_truncated_run_emits_the_truncation_fields_on_the_log_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated run must SAY it was truncated, on the line Datadog actually receives.

        REBASE TRIPWIRE (D21). The three `mark_platform_cap` call sites are the same three hunks
        SKY-15830 rewrites: the OCR token cap, the DOCX cap, and `_bound_extraction_input_tokens`.
        A "keep mine" conflict resolution drops the wiring, and `parse completed` then reports
        `truncated=false` with null truncation fields on every truncated run -- the exact inverse
        of what both PRs exist to produce, invisible to either suite. Asserting on the emitted log
        line rather than on `_telemetry` is what makes that severed wiring fail here.
        """
        path = tmp_path / "data.csv"
        path.write_text("name,city\n" + "\n".join(f"person{i},city{i}" for i in range(200)) + "\n")
        block = _make_file_parser_block("https://example.com/export/data.csv", FileType.AUTO_DETECT)
        block.json_schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        monkeypatch.setattr(block_module, "MAX_FILE_PARSE_INPUT_TOKENS", 50)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_extraction(mp, AsyncMock(return_value={"name": "person0"}))
            with structlog.testing.capture_logs() as logs:
                result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert completed["truncated"] is True
        assert completed["truncation_source"] == "platform_cap"
        # `limit` is D3-amend item 5's canonical name: SKY-15830 keys on it and QA groups by it.
        assert completed["limit"] == "token_limit"
        assert completed["tokens_included"] == 50
        assert completed["token_limit"] == 50

    async def test_a_limit_value_this_pr_never_emits_still_reaches_the_log_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ocr_page_failure` arrives with SKY-15830, so nothing here emits it yet.

        It is asserted through the real log path anyway: if the value is dropped from
        `TruncationLimit` at that rebase, or the telemetry stops reaching `parse completed`,
        this fails rather than the value silently never appearing on a dashboard.
        """
        path = tmp_path / "data.csv"
        path.write_text("name,city\nAlice,Paris\n")
        block = _make_file_parser_block("https://example.com/export/data.csv", FileType.AUTO_DETECT)

        async def parse_and_mark(self: FileParserBlock, file_path: str) -> str:
            self._telemetry.mark_platform_cap(
                limit="ocr_page_failure", tokens_included=7, token_limit=99, pages_included=3
            )
            return "name,city\nAlice,Paris\n"

        monkeypatch.setattr(FileParserBlock, "_parse_csv_file", parse_and_mark)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert completed["limit"] == "ocr_page_failure"
        assert completed["truncation_source"] == "platform_cap"
        assert completed["truncated"] is True
        assert completed["pages_included"] == 3

    async def test_both_parser_lines_carry_the_file_type_join_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The join between failures and completions is these two names on both lines (F7).

        A query that counts fallback share or failure rate by file type reads them off whichever
        line fired, so a null on either side silently drops that run from the denominator.
        """
        good = tmp_path / "data.csv"
        good.write_text("name,city\nAlice,Paris\n")
        bad = tmp_path / "downloaded"
        bad.write_bytes(b"\x0cplain text with a null byte\x00")

        with structlog.testing.capture_logs() as logs:
            ok = await _execute_with_downloaded_file(
                _make_file_parser_block("https://example.com/export/data.csv", FileType.AUTO_DETECT),
                good,
                monkeypatch,
            )
        (completed,) = _events(logs, COMPLETED_EVENT)

        with structlog.testing.capture_logs() as logs:
            failed_result = await _execute_with_downloaded_file(
                _make_file_parser_block("https://example.com/private/download", FileType.AUTO_DETECT),
                bad,
                monkeypatch,
            )
        (failure,) = _events(logs, FAILURE_EVENT)

        assert ok.success is True and failed_result.success is False
        assert completed["configured_file_type"] == FileType.AUTO_DETECT
        assert completed["file_type_detected"] == FileType.CSV
        assert failure["configured_file_type"] == FileType.AUTO_DETECT
        assert failure["file_type_detected"] == FileType.CSV

    async def _warning_for_schema(self, schema: dict[str, Any], response: Any) -> dict[str, Any]:
        """Drive the real extraction path and return the schema-validation WARNING it emitted."""
        block = _make_file_parser_block("https://example.com/data.csv", FileType.CSV)
        block.json_schema = schema
        with pytest.MonkeyPatch.context() as mp:
            self._patch_extraction(mp, AsyncMock(return_value=response))
            with structlog.testing.capture_logs() as logs:
                with pytest.raises(ValueError):
                    await block._extract_with_ai("name\nAlice", MagicMock())
        warnings = _events(logs, SCHEMA_FAILED_EVENT)
        assert warnings
        return warnings[0]

    async def test_the_emitted_schema_sha256_discriminates_between_schemas(self) -> None:
        """Asserting the hash function discriminates does not prove the FIELD does.

        A constant substituted at the call site leaves the function untouched and satisfies the
        hex-shape assertion, so the value that reaches Datadog has to be compared across schemas.
        """
        one = await self._warning_for_schema(
            {"type": "object", "required": ["alpha"], "properties": {"other": {}}}, {"other": 1}
        )
        another = await self._warning_for_schema(
            {"type": "object", "required": ["beta"], "properties": {"other": {}}}, {"other": 1}
        )
        assert one["schema_sha256"] != another["schema_sha256"]

    async def test_an_unreadable_schema_emits_null_counts_not_zero(self) -> None:
        """The honest-null half of the resolver, asserted on the fields that carry it.

        A dangling local reference passes `validate_schema`, so this reaches the real logging path.
        Zero here would be indistinguishable from a schema that genuinely declares nothing --
        the false zero the resolver exists to remove, on the fields it was added for.
        """
        warning = await self._warning_for_schema({"$ref": "#/$defs/Missing", "$defs": {}}, {"a": 1})

        assert warning["required_count"] is None
        assert warning["properties_count"] is None
        # The same line must not claim the schema was readable for the response-key fields.
        assert warning["response_top_level_keys"] is None
        assert warning["unexpected_key_count"] is None

    async def test_a_readable_schema_declaring_nothing_still_emits_zero(self) -> None:
        """Null means "not measured"; a schema that really declares nothing must still say zero."""
        warning = await self._warning_for_schema({"type": "object", "required": ["a"]}, {})

        assert warning["required_count"] == 1
        assert warning["properties_count"] == 0
        assert warning["response_top_level_keys"] == []
        assert warning["unexpected_key_count"] == 0

    async def test_schema_warning_leaks_neither_response_keys_nor_signing_material(self) -> None:
        """This line fires only when the model ignored the schema, i.e. when its keys came from the file."""
        block = _make_file_parser_block(
            "https://artifacts.example.com/org/run/file.pdf?sig=deadbeef&expiry=1788800400", FileType.CSV
        )
        block.json_schema = {"type": "object", "properties": {"rows": {"type": "array"}}, "required": ["rows"]}
        leaked_person = "Jane Q Doe (SSN 123-45-6789)"
        leaked_account = "acct 4111111111111111"
        handler = AsyncMock(return_value={leaked_person: 1, leaked_account: 2})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_extraction(mp, handler)
            with structlog.testing.capture_logs() as logs:
                with pytest.raises(ValueError, match="does not match file parser JSON schema"):
                    await block._extract_with_ai("name\nAlice", MagicMock())

        warnings = _events(logs, SCHEMA_FAILED_EVENT)
        assert warnings
        for warning in warnings:
            assert warning["response_top_level_keys"] == []
            assert warning["unexpected_key_count"] == 2
            assert warning["file_url"] == "https://artifacts.example.com/org/run/file.pdf"
            _assert_no_content_in_fields(warning, (leaked_person, "123-45-6789", "4111111111111111", "sig=", "expiry="))

    async def test_docx_truncation_sets_truncated_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _create_docx(tmp_path / "downloaded.docx", paragraphs=["word " * 200, "more words " * 200])
        block = _make_file_parser_block("https://example.com/report.docx", FileType.AUTO_DETECT)

        async def parse_with_small_budget(self: FileParserBlock, file_path: str) -> str:
            return self._parse_docx_file_sync(file_path, max_tokens=50)

        monkeypatch.setattr(FileParserBlock, "_parse_docx_file", parse_with_small_budget)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (completed,) = _events(logs, COMPLETED_EVENT)
        assert completed["truncated"] is True
        assert completed["truncation_source"] == "platform_cap"
        assert completed["token_limit"] == 50
        assert 0 <= completed["tokens_included"] <= 50  # first paragraph alone exceeds the budget
        assert completed["had_schema"] is False


class TestClassifySchemaValidationFailure:
    """Contract for failure_class; eng-schema-fix keys its no-retry decision on these values (SKY-15829)."""

    @pytest.mark.parametrize(
        ("schema", "response", "expected"),
        [
            (
                {"type": "object", "required": ["a", "b"], "properties": {"c": {"type": "string"}}},
                {"c": "x"},
                "required_not_in_properties",
            ),
            (
                {"type": "array", "items": {"type": "object", "required": ["n"], "properties": {"z": {}}}},
                [{"z": 1}],
                "required_not_in_properties",
            ),
            (
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {},
                "missing_required_defined",
            ),
            (
                {"type": "object", "required": ["a", "b"], "properties": {"b": {"type": "string"}}},
                {},
                "missing_required_defined",
            ),
            (
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {"a": None},
                "null_for_non_nullable",
            ),
            (
                {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "string"}}}},
                {"rows": [None, None]},
                "null_for_non_nullable",
            ),
            (
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {"a": 5},
                "type_mismatch_other",
            ),
            (
                {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
                {"a": None, "b": 5},
                "type_mismatch_other",
            ),
            (
                {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}},
                {"a": "ok", "extra": 1},
                "type_mismatch_other",
            ),
        ],
    )
    def test_classes(self, schema: dict[str, Any], response: Any, expected: str) -> None:
        failure = block_module._validate_response_against_json_schema(response, schema, "File parser")
        assert failure is not None
        assert block_module._classify_schema_validation_failure(failure, response, schema) == expected

    def test_schema_configuration_failures_are_schema_invalid(self) -> None:
        schema = {"type": "object"}
        assert (
            block_module._classify_schema_validation_failure("File parser JSON schema is invalid.", {}, schema)
            == "schema_invalid"
        )
        assert (
            block_module._classify_schema_validation_failure(
                "File parser JSON schema validation failed (TypeError).", {}, schema
            )
            == "schema_invalid"
        )

    def test_undeclared_required_count_survives_a_mixed_failure(self) -> None:
        """The same broken schema falls into either required bucket depending on what the model returned.

        `failure_class` must keep the strict rule eng-schema-fix's customer-visible message keys on
        (D4-text-amend), so the undeclared family is sized by its own field instead.
        """
        schema = {
            "type": "object",
            "required": ["date_issued", "vendor"],
            "properties": {"vendor": {"type": "string"}},
        }
        omitted_everything: dict[str, Any] = {}
        returned_the_declared_key = {"vendor": "v"}

        for response, expected_class in (
            (omitted_everything, "missing_required_defined"),
            (returned_the_declared_key, "required_not_in_properties"),
        ):
            failure = block_module._validate_response_against_json_schema(response, schema, "File parser")
            assert failure is not None
            facts = block_module._schema_validation_facts(failure, response, schema)
            assert facts["failure_class"] == expected_class
            # Same defective schema, same one undeclared required key, either way.
            assert facts["undeclared_required_count"] == 1

    def test_a_slash_in_a_property_name_does_not_collapse_two_violations(self) -> None:
        """A property named "a/b" and the nested path a -> b flatten to the same string.

        Both omitting the same undeclared key would dedupe to one, silently under-reporting the
        count that sizes this family.
        """
        schema = {
            "type": "object",
            "required": ["a/b", "a"],
            "properties": {
                "a/b": {"type": "object", "required": ["x"], "properties": {}},
                "a": {"type": "object", "properties": {"b": {"type": "object", "required": ["x"], "properties": {}}}},
            },
        }
        response = {"a/b": {}, "a": {"b": {}}}
        failure = block_module._validate_response_against_json_schema(response, schema, "File parser")
        assert failure is not None

        facts = block_module._schema_validation_facts(failure, response, schema)

        assert facts["undeclared_required_count"] == 2

    def test_undeclared_required_count_is_none_when_the_class_is_schema_invalid(self) -> None:
        facts = block_module._schema_validation_facts(
            "File parser JSON schema validation failed (TypeError).", {}, {"type": "object"}
        )
        assert facts["failure_class"] == "schema_invalid"
        assert facts["undeclared_required_count"] is None


class TestSchemaShapeFacts:
    """`required_count` / `properties_count` must never report a false zero (Codex P2)."""

    REF_ROOTED = {
        "$ref": "#/$defs/Invoice",
        "$defs": {
            "Invoice": {
                "type": "object",
                "required": ["total", "date"],
                "properties": {"total": {}, "date": {}, "vendor": {}},
            }
        },
    }

    def test_two_schemas_hash_differently(self) -> None:
        """`schema_sha256` exists to tell schemas apart, and the shape assertions cannot check that.

        A hex-shape regex is satisfied by a constant, so a hash degraded to one fixed value would
        group every schema together and pass every other test — silently collapsing the grouping
        the residual analysis depends on.
        """
        one = block_module._schema_sha256({"type": "object", "required": ["a"]})
        another = block_module._schema_sha256({"type": "object", "required": ["b"]})
        assert one != another

    def test_hash_is_stable_across_key_order(self) -> None:
        """Grouping by it requires the same schema to hash the same however it was serialised."""
        assert block_module._schema_sha256({"type": "object", "required": ["a"]}) == block_module._schema_sha256(
            {"required": ["a"], "type": "object"}
        )

    def test_ref_rooted_schema_reports_the_referenced_object(self) -> None:
        node = block_module._schema_object_node(self.REF_ROOTED)
        assert node is not None
        assert len(node["required"]) == 2
        assert len(node["properties"]) == 3

    def test_ref_rooted_schema_does_not_look_like_an_empty_one(self) -> None:
        """A zero is a legitimate value, so a false zero is indistinguishable from a real one."""
        empty = block_module._schema_object_node({"type": "object"})
        ref_rooted = block_module._schema_object_node(self.REF_ROOTED)
        assert empty is not None and ref_rooted is not None
        assert len(ref_rooted.get("required") or []) != len(empty.get("required") or [])

    @staticmethod
    def _ref_chain(length: int) -> dict[str, Any]:
        defs: dict[str, Any] = {f"r{i}": {"$ref": f"#/$defs/r{i + 1}"} for i in range(length - 1)}
        defs[f"r{length - 1}"] = {"type": "object", "required": ["a"], "properties": {"a": {}, "b": {}}}
        return {"$ref": "#/$defs/r0", "$defs": defs}

    @pytest.mark.parametrize(
        "schema",
        [
            pytest.param({"$ref": "https://example.com/schema.json"}, id="remote"),
            pytest.param({"$ref": "#/$defs/Missing", "$defs": {}}, id="dangling"),
            pytest.param({"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/A"}}}, id="direct-cycle"),
            pytest.param(
                {"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}},
                id="indirect-cycle",
            ),
            pytest.param({"$ref": "#"}, id="whole-document"),
            pytest.param({"$ref": "#/"}, id="empty-pointer"),
            pytest.param({"$ref": "#/$defs/A", "$defs": {"A": "not an object"}}, id="resolves-to-string"),
            pytest.param({"$ref": "#/$defs/A", "$defs": {"A": [1, 2]}}, id="resolves-to-list"),
            # Rejected upstream by validate_schema today; the invariant must not depend on that.
            pytest.param({"$ref": 42}, id="non-string-ref"),
            pytest.param({"$ref": None}, id="null-ref"),
        ],
    )
    def test_unresolvable_ref_reports_unknown_rather_than_zero(self, schema: dict[str, Any]) -> None:
        """A node that HAS a `$ref` we cannot follow is unknown, never empty.

        Returning the wrapper would report zero required and zero properties -- the exact false
        zero this resolver exists to remove, reappearing inside its own fix.
        """
        assert block_module._schema_object_node(schema) is None

    def test_a_chain_at_the_depth_cap_still_resolves(self) -> None:
        """`_SCHEMA_REF_MAX_DEPTH` is a promise about how many refs are followed, not one fewer."""
        at_cap = block_module._schema_object_node(self._ref_chain(block_module._SCHEMA_REF_MAX_DEPTH))
        assert at_cap is not None
        assert len(at_cap["required"]) == 1

    def test_a_chain_past_the_depth_cap_reports_unknown(self) -> None:
        assert block_module._schema_object_node(self._ref_chain(block_module._SCHEMA_REF_MAX_DEPTH + 1)) is None

    def test_json_pointer_escapes_are_honoured(self) -> None:
        schema = {
            "$ref": "#/$defs/a~1b",
            "$defs": {"a/b": {"type": "object", "required": ["x"], "properties": {"x": {}}}},
        }
        node = block_module._schema_object_node(schema)
        assert node is not None and len(node["required"]) == 1

    def test_an_unresolvable_ref_reports_unknown_response_key_facts(self) -> None:
        """An unreadable schema makes "undeclared" meaningless, so both fields are null.

        Naming nothing is safe in the privacy direction, but a definite count would say every key
        was undeclared -- over-counting on every unresolvable-reference schema, and disagreeing
        with the null shape fields sitting on the same line.
        """
        facts = block_module._response_key_facts(
            {"Jane Q Doe (SSN 123-45-6789)": 1, "acct 4111111111111111": 2}, {"$ref": "https://x/s.json"}
        )
        assert facts["response_top_level_keys"] is None
        assert facts["unexpected_key_count"] is None

    def test_a_readable_schema_still_names_only_declared_keys(self) -> None:
        """The privacy guarantee is unchanged when the schema IS readable."""
        facts = block_module._response_key_facts({"a": 1, "Jane Q Doe (SSN 1)": 2}, self._ref_chain(2))
        assert facts["response_top_level_keys"] == ["a"]
        assert facts["unexpected_key_count"] == 1

    def test_ref_rooted_array_root_resolves_its_items(self) -> None:
        schema = {
            "type": "array",
            "items": {"$ref": "#/$defs/Row"},
            "$defs": {"Row": {"type": "object", "required": ["a"], "properties": {"a": {}}}},
        }
        node = block_module._schema_object_node(schema)
        assert node is not None and len(node["required"]) == 1

    def test_declared_keys_are_recognised_through_a_ref(self) -> None:
        """Without resolution every declared key counts as unexpected, inflating the leak metric."""
        facts = block_module._response_key_facts({"total": 1, "date": "x"}, self.REF_ROOTED)
        assert facts["response_top_level_keys"] == ["date", "total"]
        assert facts["unexpected_key_count"] == 0


class TestFailureFamily:
    """failure_family is the aggregation key for FILE_PARSER_ERROR volume (D7-fields item 1)."""

    # Verbatim from SKY-15831 (D8-text / D8-text-amend2): the quoted reference is the customer's
    # own parameter key and sits inside the 60-char window this field cuts at.
    EMPTY_INPUT_MESSAGE = (
        'File URL is empty: "{ref}" resolved to no downloaded file ({detail}). '
        'Check the upstream block\'s output, or set "On block failure" on this block '
        "to skip the iteration."
    )

    @pytest.mark.parametrize(
        "detail",
        [
            "its downloaded_files list was empty",
            "no value was set for it",
            "its value is not a downloaded file URL",
        ],
    )
    def test_quoted_references_collapse_so_one_failure_is_one_family(self, detail: str) -> None:
        refs = ["download_block_output", "invoice_pdf", "{{ download_2_output }}", "a_much_longer_parameter_name"]
        families = {block_module._failure_family(self.EMPTY_INPUT_MESSAGE.format(ref=r, detail=detail)) for r in refs}

        assert len(families) == 1, f"one failure split into {len(families)} families: {families}"
        (family,) = families
        assert family.startswith("File URL is empty: <ref> resolved to")
        assert len(family) <= 60
        for ref in refs:
            assert ref not in family

    def test_urls_are_still_stripped_before_truncation(self) -> None:
        family = block_module._failure_family(
            "Failed to download or validate file: File URL https://x.example.com/a?sig=1 is not valid"
        )
        assert "x.example.com" not in family
        assert "<url>" in family


class TestTruncationSourceDerivation:
    """`truncation_source` is derived from `limit`, never stored (D3-fields dropped `source`)."""

    def test_limit_vocabulary_is_the_shared_one(self) -> None:
        """`TruncationLimit` is a cross-PR contract; SKY-15830 emits values this PR never does.

        `Literal` is checked by mypy, not at runtime, so dropping a value at rebase would leave
        every test green and the value would simply never appear on a dashboard. Assert the set.
        """
        assert set(get_args(block_module.TruncationLimit)) == {
            "token_limit",
            "ocr_page_limit",
            "ocr_page_failure",
            "max_pages",
        }

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            ("token_limit", "platform_cap"),
            ("ocr_page_limit", "platform_cap"),
            # SKY-15830's fourth value: pages lost to OCR failure is not the user's doing.
            ("ocr_page_failure", "platform_cap"),
            ("max_pages", "max_pages"),
        ],
    )
    def test_source_follows_limit(self, limit: str, expected: str) -> None:
        telemetry = block_module.FileParserTelemetry()
        assert telemetry.truncation_source is None
        assert telemetry.truncated is False

        telemetry.mark_platform_cap(limit=limit, tokens_included=10, token_limit=20)

        assert telemetry.truncation_source == expected
        assert telemetry.truncated is True

    def test_first_cut_wins(self) -> None:
        telemetry = block_module.FileParserTelemetry()
        telemetry.mark_platform_cap(limit="ocr_page_limit", tokens_included=10, token_limit=20, pages_included=3)
        telemetry.mark_platform_cap(limit="token_limit", tokens_included=99, token_limit=99)

        assert telemetry.limit == "ocr_page_limit"
        assert telemetry.tokens_included == 10
        assert telemetry.pages_included == 3


class TestCsvFallbackWarning:
    """Shadow log for the silent CSV fallback (D5); detection behavior is unchanged."""

    @staticmethod
    def _detect_with_logs(url: str, path: Path | None) -> tuple[FileType, list[dict[str, Any]]]:
        block = _make_file_parser_block(url, FileType.AUTO_DETECT)
        with structlog.testing.capture_logs() as logs:
            detected = block._detect_file_type_from_url(url, file_path=str(path) if path else None)
        return detected, _events(logs, CSV_FALLBACK_EVENT)

    def test_html_page_is_flagged_without_logging_content(self, tmp_path: Path) -> None:
        path = tmp_path / "downloaded"
        path.write_text("<!DOCTYPE html><html><head><title>Sign in</title></head><body>secret-body-text</body></html>")

        detected, events = self._detect_with_logs("https://portal.example.com/download?id=42", path)

        assert detected == FileType.CSV
        (event,) = events
        assert {
            "url_host",
            "url_suffix",
            "configured_file_type",
            "delimiter",
            "delimiter_sniffed",
            "column_count",
            "consistent_columns",
            "sampled_rows",
            "looks_like_html",
            "looks_binary",
        } <= event.keys()
        assert event["url_host"] == "portal.example.com"
        assert event["url_suffix"] == ""
        assert event["looks_like_html"] is True
        assert event["looks_binary"] is False
        _assert_no_content_in_fields(event, ("secret-body-text", "Sign in", "id=42", "https://"))

    def test_consistent_delimited_text(self, tmp_path: Path) -> None:
        path = tmp_path / "downloaded"
        path.write_text("a,b,c,d\n1,2,3,4\n5,6,7,8\n")

        _, events = self._detect_with_logs("https://example.com/export", path)

        (event,) = events
        assert event["delimiter"] == ","
        assert event["delimiter_sniffed"] is True
        assert event["column_count"] == 4
        assert event["consistent_columns"] is True
        assert event["sampled_rows"] == 2
        assert event["looks_like_html"] is False

    def test_ragged_delimited_text(self, tmp_path: Path) -> None:
        path = tmp_path / "downloaded"
        path.write_text("a,b\n1,2,3\n4,5\n")

        _, events = self._detect_with_logs("https://example.com/export", path)

        (event,) = events
        assert event["delimiter"] == ","
        assert event["delimiter_sniffed"] is False
        assert event["column_count"] == 2
        assert event["consistent_columns"] is False

    def test_utf16_csv_is_decoded_the_way_the_parser_will_read_it(self, tmp_path: Path) -> None:
        """A UTF-16 CSV decoded as UTF-8 reports false ragged columns and biases the D5 decision."""
        path = tmp_path / "downloaded"
        path.write_bytes("a,b\n1,2\n3,4\n".encode("utf-16"))

        _, events = self._detect_with_logs("https://example.com/export", path)

        (event,) = events
        assert event["looks_binary"] is False
        assert event["column_count"] == 2
        assert event["consistent_columns"] is True
        assert event["sampled_rows"] == 2
        assert event["sniff_error"] is None

    def test_binary_content_is_flagged(self, tmp_path: Path) -> None:
        path = tmp_path / "downloaded"
        path.write_bytes(b"\x00\x01\x02binary\x00")

        _, events = self._detect_with_logs("https://example.com/export", path)

        (event,) = events
        assert event["looks_binary"] is True
        assert event["delimiter"] is None

    def test_no_file_path_logs_fallback_without_content_facts(self) -> None:
        _, events = self._detect_with_logs("https://example.com/data.txt", None)

        (event,) = events
        assert event["url_suffix"] == ".txt"
        assert event["looks_like_html"] is None
        assert event["column_count"] is None

    @pytest.mark.asyncio
    async def test_a_raising_sniffer_cannot_fail_the_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shadow log runs inside execute()'s try, whose except is a customer-visible failure."""
        path = tmp_path / "downloaded"
        path.write_text("a,b\n1,2\n")
        block = _make_file_parser_block("https://example.com/export", FileType.AUTO_DETECT)

        def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sniffer blew up")

        monkeypatch.setattr(FileParserBlock, "_fill_fallback_sniff_facts", explode)

        with structlog.testing.capture_logs() as logs:
            result = await _execute_with_downloaded_file(block, path, monkeypatch)

        assert result.success is True
        (event,) = _events(logs, CSV_FALLBACK_EVENT)
        assert event["sniff_error"] == "RuntimeError"
        assert event["column_count"] is None
        assert not _events(logs, FAILURE_EVENT)

    def test_recognised_extension_does_not_log_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        path.write_text("a,b\n1,2\n")

        detected, events = self._detect_with_logs("https://example.com/data.csv", path)

        assert detected == FileType.CSV
        assert events == []
