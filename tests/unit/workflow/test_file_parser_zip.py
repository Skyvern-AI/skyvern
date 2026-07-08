"""
Tests for FileParserBlock ZIP support (SKY-11711).

Covers ZIP detection, validation, safe extraction (junk filtering, zip-bomb caps,
traversal), per-file content parsing for AI extraction, and local-path file_url
handling (applies to all file types).
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import docx
import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import InvalidFileType
from skyvern.forge.sdk.workflow.models.block import BlockType, FileParserBlock
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


def _make_file_parser_block(file_url: str, file_type: FileType = FileType.AUTO_DETECT) -> FileParserBlock:
    return FileParserBlock(
        label="test_file_parser",
        block_type=BlockType.FILE_URL_PARSER,
        output_parameter=_make_output_parameter("test_output"),
        file_url=file_url,
        file_type=file_type,
    )


def _create_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def _create_docx_bytes(paragraph: str, path: Path) -> bytes:
    doc = docx.Document()
    doc.add_paragraph(paragraph)
    doc.save(str(path))
    return path.read_bytes()


class TestZipDetection:
    def _detect(self, url: str, file_path: str | None = None) -> FileType:
        block = _make_file_parser_block(url)
        return block._detect_file_type_from_url(url, file_path=file_path)

    def test_zip_extension(self) -> None:
        assert self._detect("https://example.com/archive.zip") == FileType.ZIP

    def test_zip_extension_case_insensitive(self) -> None:
        assert self._detect("https://example.com/archive.ZIP") == FileType.ZIP

    def test_zip_extension_with_query_params(self) -> None:
        assert self._detect("https://example.com/archive.zip?token=abc") == FileType.ZIP

    def test_no_extension_with_zip_magic_bytes(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "no_ext_file", {"a.txt": b"hello"})
        assert self._detect("https://example.com/download?id=123", file_path=str(zip_path)) == FileType.ZIP

    def test_docx_magic_bytes_not_detected_as_zip(self, tmp_path: Path) -> None:
        # DOCX is a ZIP container; the OOXML matcher must win over the generic zip matcher.
        docx_path = tmp_path / "no_ext_docx"
        _create_docx_bytes("hello", docx_path)
        assert self._detect("https://example.com/download?id=123", file_path=str(docx_path)) == FileType.DOCX


class TestValidateZipFileType:
    def test_valid_zip(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "valid.zip", {"a.txt": b"hello"})
        block = _make_file_parser_block("https://example.com/valid.zip", FileType.ZIP)
        block.validate_file_type("https://example.com/valid.zip", str(zip_path))

    def test_non_zip_with_zip_extension(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "fake.zip"
        fake_path.write_text("this is not a zip")
        block = _make_file_parser_block("https://example.com/fake.zip", FileType.ZIP)
        with pytest.raises(InvalidFileType, match="not a valid ZIP"):
            block.validate_file_type("https://example.com/fake.zip", str(fake_path))


class TestIsZipJunkMember:
    @pytest.mark.parametrize(
        "member_name",
        [
            "__MACOSX/report.pdf",
            "nested/__MACOSX/._report.pdf",
            "._invoice.pdf",
            "nested/._invoice.pdf",
            ".DS_Store",
            "nested/.DS_Store",
            "Thumbs.db",
        ],
    )
    def test_junk_members(self, member_name: str) -> None:
        assert FileParserBlock._is_zip_junk_member(member_name) is True

    @pytest.mark.parametrize(
        "member_name",
        ["report.pdf", "nested/data.csv", "macosx/report.pdf", "_underscore.pdf", "a._b.pdf"],
    )
    def test_real_members(self, member_name: str) -> None:
        assert FileParserBlock._is_zip_junk_member(member_name) is False


class TestExtractZipFile:
    def _extract(self, mp: pytest.MonkeyPatch, tmp_path: Path, zip_files: dict[str, bytes]) -> list[dict[str, Any]]:
        mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
        zip_path = _create_zip(tmp_path / "archive.zip", zip_files)
        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        return block._extract_zip_file(str(zip_path), "wr_test", "wrb_test")

    def test_extracts_files_and_returns_sorted_list(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            extracted = self._extract(
                mp,
                tmp_path,
                {"b_second.txt": b"second", "nested/a_first.txt": b"first!"},
            )

        assert [f["file_name"] for f in extracted] == ["b_second.txt", "nested/a_first.txt"]
        for file_info in extracted:
            assert Path(file_info["file_path"]).is_file()
            assert file_info["file_path"].startswith(str(tmp_path / "downloads" / "wr_test"))
        assert extracted[0]["file_size"] == len(b"second")
        assert Path(extracted[1]["file_path"]).read_bytes() == b"first!"

    def test_junk_entries_are_not_extracted(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            extracted = self._extract(
                mp,
                tmp_path,
                {"real.txt": b"data", "__MACOSX/._real.txt": b"junk", ".DS_Store": b"junk"},
            )

        assert [f["file_name"] for f in extracted] == ["real.txt"]
        extract_dir = Path(extracted[0]["file_path"]).parent
        assert not (extract_dir / "__MACOSX").exists()
        assert not (extract_dir / ".DS_Store").exists()

    def test_traversal_member_stays_inside_extract_dir(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            extracted = self._extract(mp, tmp_path, {"../evil.txt": b"escape attempt"})

        assert len(extracted) == 1
        download_root = (tmp_path / "downloads").resolve()
        resolved = Path(extracted[0]["file_path"]).resolve()
        assert resolved.is_relative_to(download_root)
        assert not (tmp_path / "evil.txt").exists()

    def test_too_many_entries_raises(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "_MAX_ZIP_ENTRIES", 2)
            with pytest.raises(InvalidFileType, match="exceeding the limit"):
                self._extract(mp, tmp_path, {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})

    def test_archive_size_cap_raises_before_opening_zip(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"a.txt": b"1"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
            mp.setattr(FileParserBlock, "_MAX_ZIP_ARCHIVE_BYTES", zip_path.stat().st_size - 1)
            mp.setattr(
                zipfile,
                "ZipFile",
                MagicMock(side_effect=AssertionError("ZipFile must not be opened after archive-size preflight")),
            )
            block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)

            with pytest.raises(InvalidFileType, match="ZIP archive size"):
                block._extract_zip_file(str(zip_path), "wr_test", "wrb_test")

    def test_read_zip_total_entry_count(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})

        assert FileParserBlock._read_zip_total_entry_count(str(zip_path)) == 3

    def test_read_zip64_total_entry_count_with_max_comment(self, tmp_path: Path) -> None:
        zip64_eocd = b"PK\x06\x06" + (b"\x00" * 28) + (70_000).to_bytes(8, "little") + (b"\x00" * 16)
        zip64_locator = b"PK\x06\x07" + (b"\x00" * 16)
        classic_eocd = (
            b"PK\x05\x06"
            + (b"\x00" * 4)
            + (0xFFFF).to_bytes(2, "little")
            + (0xFFFF).to_bytes(2, "little")
            + (b"\x00" * 8)
            + (65_535).to_bytes(2, "little")
        )
        zip_path = tmp_path / "zip64_tail.zip"
        zip_path.write_bytes(b"\x00" * 4096 + zip64_eocd + zip64_locator + classic_eocd + (b"c" * 65_535))

        assert FileParserBlock._read_zip_total_entry_count(str(zip_path)) == 70_000

    def test_declared_entry_count_cap_raises_before_opening_zip(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
            mp.setattr(FileParserBlock, "_MAX_ZIP_ENTRIES", 2)
            mp.setattr(
                zipfile,
                "ZipFile",
                MagicMock(side_effect=AssertionError("ZipFile must not be opened after entry-count preflight")),
            )
            block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)

            with pytest.raises(InvalidFileType, match="ZIP archive declares 3 entries"):
                block._extract_zip_file(str(zip_path), "wr_test", "wrb_test")

    def test_uncompressed_size_cap_raises(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "_MAX_ZIP_UNCOMPRESSED_BYTES", 10)
            with pytest.raises(InvalidFileType, match="uncompressed size"):
                self._extract(mp, tmp_path, {"big.txt": b"x" * 100})

    def test_measured_uncompressed_size_helper_raises(self) -> None:
        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "_MAX_ZIP_UNCOMPRESSED_BYTES", 10)
            block._check_extracted_size_within_limit(10)
            with pytest.raises(InvalidFileType, match="uncompressed content exceeds the limit of 10 bytes"):
                block._check_extracted_size_within_limit(11)

    def test_encrypted_zip_raises(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "encrypted.zip", {"secret.txt": b"secret data"})
        # zipfile cannot write encrypted archives, so set the encryption flag bit directly
        # in the local file header (PK\x03\x04 offset 6) and central directory (PK\x01\x02 offset 8).
        data = bytearray(zip_path.read_bytes())
        data[data.find(b"PK\x03\x04") + 6] |= 0x1
        data[data.find(b"PK\x01\x02") + 8] |= 0x1
        zip_path.write_bytes(bytes(data))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
            block = _make_file_parser_block("https://example.com/encrypted.zip", FileType.ZIP)
            with pytest.raises(InvalidFileType, match="Password-protected"):
                block._extract_zip_file(str(zip_path), "wr_test", "wrb_test")

    def test_empty_zip_returns_empty_list(self, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            extracted = self._extract(mp, tmp_path, {})

        assert extracted == []

    def test_colliding_member_names_keep_last_entry_once(self, tmp_path: Path) -> None:
        # "a.txt" and "../a.txt" sanitize to the same destination; per ZIP semantics the
        # last member wins and the output must not contain duplicate entries.
        zip_path = tmp_path / "colliding.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", b"first version")
            zf.writestr("../a.txt", b"second version")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
            block = _make_file_parser_block("https://example.com/colliding.zip", FileType.ZIP)
            extracted = block._extract_zip_file(str(zip_path), "wr_test", "wrb_test")

        assert [f["file_name"] for f in extracted] == ["a.txt"]
        assert Path(extracted[0]["file_path"]).read_bytes() == b"second version"
        assert extracted[0]["file_size"] == len(b"second version")


@pytest.mark.asyncio
class TestParseZipContents:
    @staticmethod
    def _file_info(path: Path, root: Path) -> dict[str, Any]:
        return {
            "file_name": str(path.relative_to(root)),
            "file_path": str(path),
            "file_size": path.stat().st_size,
        }

    async def test_parses_supported_files_and_skips_unparseable(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("name,age\nAlice,30")
        docx_path = tmp_path / "doc.docx"
        _create_docx_bytes("Report body", docx_path)
        nested_zip = _create_zip(tmp_path / "inner.zip", {"x.txt": b"nested"})
        binary_path = tmp_path / "blob.bin"
        binary_path.write_bytes(b"\x00\x01\x02\x03")

        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        entries = await block._parse_zip_contents(
            [
                self._file_info(csv_path, tmp_path),
                self._file_info(docx_path, tmp_path),
                self._file_info(nested_zip, tmp_path),
                self._file_info(binary_path, tmp_path),
            ]
        )

        assert entries == [
            {"file_name": "data.csv", "content": [{"name": "Alice", "age": "30"}]},
            {"file_name": "doc.docx", "content": "Report body"},
        ]

    async def test_pdf_files_parsed_via_pdf_parser(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.5 fake")

        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(FileParserBlock, "_parse_pdf_file", AsyncMock(return_value="pdf text content"))
            entries = await block._parse_zip_contents([self._file_info(pdf_path, tmp_path)])

        assert entries == [{"file_name": "report.pdf", "content": "pdf text content"}]

    async def test_truncates_at_file_boundary_on_token_limit(self, tmp_path: Path) -> None:
        first = tmp_path / "a.csv"
        first.write_text("h\n1")
        second = tmp_path / "b.csv"
        second.write_text("h\n2")

        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("skyvern.forge.sdk.workflow.models.parser_blocks.count_tokens", lambda text: 10)
            mp.setattr("skyvern.forge.sdk.workflow.models.parser_blocks.MAX_FILE_PARSE_INPUT_TOKENS", 15)
            entries = await block._parse_zip_contents(
                [self._file_info(first, tmp_path), self._file_info(second, tmp_path)]
            )

        assert [entry["file_name"] for entry in entries] == ["a.csv"]

    async def test_no_parseable_files_raises(self, tmp_path: Path) -> None:
        binary_path = tmp_path / "blob.bin"
        binary_path.write_bytes(b"\x00\x01\x02\x03")

        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        with pytest.raises(InvalidFileType, match="no parseable files"):
            await block._parse_zip_contents([self._file_info(binary_path, tmp_path)])

    async def test_first_file_over_budget_raises(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("h\n1")

        block = _make_file_parser_block("https://example.com/archive.zip", FileType.ZIP)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("skyvern.forge.sdk.workflow.models.parser_blocks.count_tokens", lambda text: 100)
            mp.setattr("skyvern.forge.sdk.workflow.models.parser_blocks.MAX_FILE_PARSE_INPUT_TOKENS", 15)
            with pytest.raises(InvalidFileType, match="alone exceeds the maximum extraction input size"):
                await block._parse_zip_contents([self._file_info(csv_path, tmp_path)])


@pytest.mark.asyncio
class TestExecuteWithZipAndLocalPaths:
    @staticmethod
    def _patch_execute_dependencies(mp: pytest.MonkeyPatch, tmp_path: Path) -> AsyncMock:
        workflow_run_context = MagicMock()
        workflow_run_context.has_parameter.return_value = False
        record_output_parameter_value = AsyncMock()

        async def fake_build_block_result(self: FileParserBlock, **kwargs: Any) -> BlockResult:
            kwargs.pop("organization_id", None)
            kwargs.pop("error_codes", None)
            return BlockResult(output_parameter=self.output_parameter, **kwargs)

        mp.setattr(
            FileParserBlock, "get_workflow_run_context", staticmethod(lambda workflow_run_id: workflow_run_context)
        )
        mp.setattr(FileParserBlock, "record_output_parameter_value", record_output_parameter_value)
        mp.setattr(FileParserBlock, "build_block_result", fake_build_block_result)
        mp.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
        return record_output_parameter_value

    async def test_execute_zip_without_schema_outputs_file_list(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"a.txt": b"hello", "b.txt": b"world"})
        block = _make_file_parser_block("https://example.com/archive.zip")

        with pytest.MonkeyPatch.context() as mp:
            self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is True
        assert result.status == BlockStatus.completed
        output = result.output_parameter_value
        assert isinstance(output, list)
        assert [f["file_name"] for f in output] == ["a.txt", "b.txt"]
        for file_info in output:
            assert Path(file_info["file_path"]).is_file()

    async def test_execute_local_path_inside_download_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "downloads" / "wr_test"
        run_dir.mkdir(parents=True)
        csv_path = run_dir / "data.csv"
        csv_path.write_text("name,age\nAlice,30")
        block = _make_file_parser_block(str(csv_path))

        with pytest.MonkeyPatch.context() as mp:
            self._patch_execute_dependencies(mp, tmp_path)
            download_mock = AsyncMock(side_effect=AssertionError("download_file must not be called"))
            mp.setattr("skyvern.forge.sdk.api.files.download_file", download_mock)

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == [{"name": "Alice", "age": "30"}]
        download_mock.assert_not_awaited()

    async def test_execute_zip_with_schema_runs_combined_ai_extraction(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"data.csv": b"name,age\nAlice,30"})
        block = _make_file_parser_block("https://example.com/archive.zip")
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.parser_blocks.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: AsyncMock(return_value={"answer": 42}),
            )
            mock_load = MagicMock(return_value="prompt")
            mp.setattr("skyvern.forge.sdk.workflow.models.parser_blocks.prompt_engine.load_prompt", mock_load)

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == {"answer": 42}
        _, kwargs = mock_load.call_args
        content_entries = json.loads(kwargs["extracted_text_content"])
        assert content_entries == [{"file_name": "data.csv", "content": [{"name": "Alice", "age": "30"}]}]
        record_output.assert_awaited_once()

    async def test_execute_local_path_outside_download_dir_fails(self, tmp_path: Path) -> None:
        outside_path = tmp_path / "elsewhere" / "data.csv"
        outside_path.parent.mkdir(parents=True)
        outside_path.write_text("name\nAlice")
        block = _make_file_parser_block(str(outside_path))

        with pytest.MonkeyPatch.context() as mp:
            self._patch_execute_dependencies(mp, tmp_path)

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is False
        assert result.status == BlockStatus.failed
        assert "Failed to download or validate file" in (result.failure_reason or "")
