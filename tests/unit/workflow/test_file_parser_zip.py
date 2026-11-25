"""
Tests for FileParserBlock ZIP support (SKY-11711).

Covers ZIP detection, validation, safe extraction (junk filtering, zip-bomb caps,
traversal), unzip-only execution, and local-path file_url handling (applies to
all file types).
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import docx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.config import settings
from skyvern.file_parser.client import IsolatedParserResult
from skyvern.forge import app
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import InvalidFileType
from skyvern.forge.sdk.workflow.models.block import BlockType, FileParserBlock, FileUploadBlock
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

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/archive.zip",
            "https://example.com/archive.ZIP",
            "https://example.com/archive.zip?token=synthetic",
        ],
    )
    def test_zip_extension(self, url: str) -> None:
        assert self._detect(url) == FileType.ZIP

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
class TestExecuteWithZipAndLocalPaths:
    @staticmethod
    def _patch_execute_dependencies(mp: pytest.MonkeyPatch, tmp_path: Path) -> AsyncMock:
        workflow_run_context = MagicMock()
        workflow_run_context.resolve_block_notes_section.return_value = None
        workflow_run_context.has_parameter.return_value = False
        run_context = WorkflowRunContext("Workflow", "w_test", "wpid_test", "wr_test", aws_client=MagicMock())
        run_context.organization_id = "org-1"
        workflow_run_context.run_files = run_context.run_files
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

    @pytest.mark.parametrize("parser_service", [False, True], ids=["in_worker", "parser_service"])
    async def test_zip_members_become_run_files_where_they_were_unzipped(
        self, tmp_path: Path, sqlite_engine: AsyncEngine, parser_service: bool
    ) -> None:
        zip_path = _create_zip(
            tmp_path / "bundle.zip", {"docs/a.csv": b"name\nAlice", "other/a.csv": b"name\nBob", "b.txt": b"plain"}
        )
        run_dir = tmp_path / "downloads" / "wr_test"
        run_dir.mkdir(parents=True)
        (run_dir / "report.txt").write_text("already here")
        upload = FileUploadBlock(label="upload", output_parameter=_make_output_parameter("upload_output"))

        def top_level_uploads() -> list[str]:
            return upload._get_files_to_upload_from_download_dir(download_files_path=str(run_dir), max_file_count=10)

        before = top_level_uploads()

        async def remote_parse(_target: str, path: Path, *, output_dir: Path, **_: Any) -> IsolatedParserResult:
            with zipfile.ZipFile(path) as archive:
                archive.extractall(output_dir)
                names = sorted(archive.namelist())
            files = [
                {
                    "file_name": name,
                    "file_path": str(output_dir / name),
                    "file_size": (output_dir / name).stat().st_size,
                }
                for name in names
            ]
            return IsolatedParserResult(
                file_type="zip", detection_source="magic", kind="files", metadata={}, files=files
            )

        async def parse(file_url: str, workflow_run_block_id: str) -> BlockResult:
            block = _make_file_parser_block(file_url)
            return await block.execute("wr_test", workflow_run_block_id, organization_id="org-1")

        with pytest.MonkeyPatch.context() as mp:
            self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr(app, "DATABASE", AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine))
            mp.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
            mp.setattr(app, "STORAGE", LocalStorage(str(tmp_path / "artifacts")))
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            if parser_service:
                mp.setattr(settings, "FILE_PARSER_SERVICE_TARGET", "file-parser.test.svc:7820")
                mp.setattr(block_module, "parse_file_remotely", remote_parse)
            first = await parse("https://example.com/bundle.zip", "wrb_first")
            second = await parse("https://example.com/bundle.zip", "wrb_second")
            listed = await app.STORAGE.get_downloaded_files("org-1", "wr_test")
            member = next(entry for entry in first.output_parameter_value if entry["file_name"] == "docs/a.csv")
            mp.setattr(settings, "FILE_PARSER_SERVICE_TARGET", "")
            member_parse = await parse(member["file_path"], "wrb_member")

        # Members keep their names and paths inside the zip, and same-named members with other bytes get "(n)".
        expected = {"b.txt": "b.txt", "docs/a.csv": "a.csv", "other/a.csv": "a (1).csv"}
        assert {entry["file_name"]: entry["run_file"] for entry in first.output_parameter_value} == expected
        assert {entry["file_name"]: entry["run_file"] for entry in second.output_parameter_value} == expected
        assert all(
            Path(entry["file_path"]).is_relative_to(run_dir / "unzipped" / "bundle_wrb_first")
            and Path(entry["file_path"]).is_file()
            for entry in first.output_parameter_value
        )
        assert sorted(file.filename for file in listed if file.filename != "report.txt") == [
            "a (1).csv",
            "a.csv",
            "b.txt",
        ]
        assert member_parse.output_parameter_value == [{"name": "Alice"}]
        assert top_level_uploads() == before == [str(run_dir / "report.txt")]

    async def test_execute_zip_with_schema_outputs_file_list_without_llm(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", {"data.csv": b"name,age\nAlice,30"})
        block = _make_file_parser_block("https://example.com/archive.zip")
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            handler = AsyncMock()
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: handler,
            )

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == [
            {
                "file_name": "data.csv",
                "file_path": str(tmp_path / "downloads" / "wr_test" / "unzipped" / "archive_wrb_test" / "data.csv"),
                "file_size": len(b"name,age\nAlice,30"),
            }
        ]
        handler.assert_not_awaited()
        record_output.assert_awaited_once()
        assert record_output.await_args.args[2] == result.output_parameter_value

    async def test_execute_explicit_zip_with_schema_outputs_file_list_without_llm(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "explicit.zip", {"nested/data.csv": b"name,age\nAlice,30"})
        block = _make_file_parser_block("https://example.com/explicit.zip", FileType.ZIP)
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            handler = AsyncMock()
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: handler,
            )

            result = await block.execute("wr_test", "wrb_explicit", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == [
            {
                "file_name": "nested/data.csv",
                "file_path": str(
                    tmp_path / "downloads" / "wr_test" / "unzipped" / "explicit_wrb_explicit" / "nested" / "data.csv"
                ),
                "file_size": len(b"name,age\nAlice,30"),
            }
        ]
        handler.assert_not_awaited()
        record_output.assert_awaited_once()
        assert record_output.await_args.args[2] == result.output_parameter_value

    async def test_execute_auto_detected_zip_with_schema_outputs_file_list_without_llm(self, tmp_path: Path) -> None:
        zip_path = _create_zip(tmp_path / "download", {"report.pdf": b"not parsed"})
        block = _make_file_parser_block("https://example.com/download?id=123")
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            handler = AsyncMock()
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: handler,
            )

            result = await block.execute("wr_test", "wrb_auto", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == [
            {
                "file_name": "report.pdf",
                "file_path": str(tmp_path / "downloads" / "wr_test" / "unzipped" / "download_wrb_auto" / "report.pdf"),
                "file_size": len(b"not parsed"),
            }
        ]
        handler.assert_not_awaited()
        record_output.assert_awaited_once()
        assert record_output.await_args.args[2] == result.output_parameter_value

    async def test_execute_run_local_zip_with_schema_outputs_file_list_without_llm(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "downloads" / "wr_test"
        run_dir.mkdir(parents=True)
        zip_path = _create_zip(run_dir / "local.zip", {"report.pdf": b"not parsed"})
        block = _make_file_parser_block(str(zip_path))
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            download_mock = AsyncMock(side_effect=AssertionError("download_file must not be called"))
            mp.setattr("skyvern.forge.sdk.api.files.download_file", download_mock)
            handler = AsyncMock()
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: handler,
            )

            result = await block.execute("wr_test", "wrb_local", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == [
            {
                "file_name": "report.pdf",
                "file_path": str(tmp_path / "downloads" / "wr_test" / "unzipped" / "local_wrb_local" / "report.pdf"),
                "file_size": len(b"not parsed"),
            }
        ]
        download_mock.assert_not_awaited()
        handler.assert_not_awaited()
        record_output.assert_awaited_once()
        assert record_output.await_args.args[2] == result.output_parameter_value

    @pytest.mark.parametrize(
        "zip_files",
        [{}, {"__MACOSX/._report.pdf": b"junk", ".DS_Store": b"junk"}],
        ids=["empty", "junk-only"],
    )
    async def test_execute_empty_or_junk_only_zip_with_schema_outputs_empty_list(
        self, tmp_path: Path, zip_files: dict[str, bytes]
    ) -> None:
        zip_path = _create_zip(tmp_path / "archive.zip", zip_files)
        block = _make_file_parser_block("https://example.com/archive.zip")
        block.json_schema = {"type": "object"}

        with pytest.MonkeyPatch.context() as mp:
            record_output = self._patch_execute_dependencies(mp, tmp_path)
            mp.setattr("skyvern.forge.sdk.api.files.download_file", AsyncMock(return_value=str(zip_path)))
            handler = AsyncMock()
            mp.setattr(
                "skyvern.forge.sdk.workflow.models.block.LLMAPIHandlerFactory.get_override_llm_api_handler",
                lambda *a, **kw: handler,
            )

            result = await block.execute("wr_test", "wrb_test", organization_id="org-1")

        assert result.success is True
        assert result.output_parameter_value == []
        handler.assert_not_awaited()
        record_output.assert_awaited_once()
        assert record_output.await_args.args[2] == result.output_parameter_value

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
