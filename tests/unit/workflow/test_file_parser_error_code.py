"""
Tests for FILE_PARSER_ERROR error codes on FileParserBlock failures.

Covers:
- FileParserError class construction
- BlockResult with error_codes field
- FileParserBlock.execute() returns error_codes on various failure paths
- FileParserBlock.execute() returns empty error_codes on success
- build_block_result() passes error_codes through to update_workflow_run_block()
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidFileType
from skyvern.forge.sdk.workflow.models.block import BlockType, FileParserBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockResult, BlockStatus, FileType


def _make_output_parameter(key: str = "test_output") -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test",
        output_parameter_id="test-output-id",
        workflow_id="test-workflow-id",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_file_parser_block(
    file_url: str = "https://example.com/file.csv", file_type: FileType = FileType.CSV
) -> FileParserBlock:
    return FileParserBlock(
        label="test_file_parser",
        block_type=BlockType.FILE_URL_PARSER,
        output_parameter=_make_output_parameter(),
        file_url=file_url,
        file_type=file_type,
    )


def _mock_workflow_run_context() -> MagicMock:
    ctx = MagicMock()
    ctx.has_parameter.return_value = False
    ctx.has_value.return_value = False
    # Failure paths now call record_output_parameter_value, which awaits this method.
    ctx.register_output_parameter_value_post_execution = AsyncMock()
    return ctx


class TestBlockResultErrorCodes:
    """Tests for the error_codes field on BlockResult."""

    def test_block_result_with_error_codes(self) -> None:
        result = BlockResult(
            success=False,
            output_parameter=_make_output_parameter(),
            failure_reason="File parse failed",
            error_codes=["FILE_PARSER_ERROR"],
            status=BlockStatus.failed,
        )
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert result.success is False

    def test_block_result_without_error_codes(self) -> None:
        result = BlockResult(
            success=True,
            output_parameter=_make_output_parameter(),
        )
        assert result.error_codes == []

    def test_block_result_default_empty(self) -> None:
        result = BlockResult(
            success=False,
            output_parameter=_make_output_parameter(),
            failure_reason="some error",
        )
        assert result.error_codes == []

    def test_block_result_multiple_error_codes(self) -> None:
        result = BlockResult(
            success=False,
            output_parameter=_make_output_parameter(),
            failure_reason="multiple errors",
            error_codes=["FILE_PARSER_ERROR", "DOWNLOAD_ERROR"],
            status=BlockStatus.failed,
        )
        assert result.error_codes == ["FILE_PARSER_ERROR", "DOWNLOAD_ERROR"]


class TestFileParserBlockGetFailureErrorCodes:
    """Tests for get_failure_error_codes() override."""

    def test_file_parser_block_returns_error_codes(self) -> None:
        block = _make_file_parser_block()
        assert block.get_failure_error_codes() == ["FILE_PARSER_ERROR"]


class TestFileParserBlockExecuteErrorCodes:
    """Tests for FileParserBlock.execute() returning error_codes on failure paths."""

    @pytest.mark.asyncio
    async def test_jinja_template_failure_returns_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="{{ invalid_jinja }}")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(
                FileParserBlock, "format_potential_template_parameters", side_effect=Exception("Jinja error")
            ):
                result = await block.execute(
                    workflow_run_id="wr_test",
                    workflow_run_block_id="wrb_test",
                    organization_id="org_test",
                )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert "jinja template" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_download_failure_returns_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/missing.csv")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    side_effect=Exception("Download failed"),
                ):
                    result = await block.execute(
                        workflow_run_id="wr_test",
                        workflow_run_block_id="wrb_test",
                        organization_id="org_test",
                    )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]

    @pytest.mark.asyncio
    async def test_file_validation_failure_returns_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/bad.csv", file_type=FileType.CSV)
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/bad.csv",
                ):
                    with patch.object(
                        FileParserBlock, "validate_file_type", side_effect=Exception("Invalid file format")
                    ):
                        result = await block.execute(
                            workflow_run_id="wr_test",
                            workflow_run_block_id="wrb_test",
                            organization_id="org_test",
                        )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]

    @pytest.mark.asyncio
    async def test_unsupported_file_type_returns_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/file.csv")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/test_file",
                ):
                    with patch.object(FileParserBlock, "_detect_file_type_from_url", return_value="unsupported_type"):
                        with patch.object(FileParserBlock, "validate_file_type"):
                            result = await block.execute(
                                workflow_run_id="wr_test",
                                workflow_run_block_id="wrb_test",
                                organization_id="org_test",
                            )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert "unsupported" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_parse_invalid_file_type_returns_error_codes(self) -> None:
        """SKY-11131: a file that passes validation but fails parsing (e.g. a non-PDF
        that pypdf opens but cannot extract) must surface a graceful failure, not raise."""
        block = _make_file_parser_block(file_url="https://example.com/statement.pdf", file_type=FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/statement.pdf",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(
                            FileParserBlock,
                            "_parse_pdf_file",
                            side_effect=InvalidFileType(
                                file_url="https://example.com/statement.pdf",
                                file_type=FileType.PDF,
                                error="both parsers failed",
                            ),
                        ):
                            result = await block.execute(
                                workflow_run_id="wr_test",
                                workflow_run_block_id="wrb_test",
                                organization_id="org_test",
                            )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert "parse" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_parse_unexpected_exception_returns_error_codes(self) -> None:
        """SKY-11131: a raw exception from the parse stage (e.g. the vision-LLM PDF
        fallback) must be caught and surfaced as a graceful failure."""
        block = _make_file_parser_block(file_url="https://example.com/statement.pdf", file_type=FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/statement.pdf",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(
                            FileParserBlock,
                            "_parse_pdf_file",
                            side_effect=Exception("No /Root object! - Is this really a PDF?"),
                        ):
                            result = await block.execute(
                                workflow_run_id="wr_test",
                                workflow_run_block_id="wrb_test",
                                organization_id="org_test",
                            )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert "parse" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_ai_extraction_failure_returns_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/file.csv")
        block.json_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/test.csv",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(FileParserBlock, "_parse_csv_file", return_value=[{"name": "Alice"}]):
                            with patch.object(FileParserBlock, "_extract_with_ai", side_effect=Exception("LLM error")):
                                result = await block.execute(
                                    workflow_run_id="wr_test",
                                    workflow_run_block_id="wrb_test",
                                    organization_id="org_test",
                                )

        assert result.success is False
        assert result.error_codes == ["FILE_PARSER_ERROR"]
        assert "extract data with ai" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_success_returns_empty_error_codes(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/file.csv")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/test.csv",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(FileParserBlock, "_parse_csv_file", return_value=[{"name": "Alice"}]):
                            with patch.object(FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock):
                                result = await block.execute(
                                    workflow_run_id="wr_test",
                                    workflow_run_block_id="wrb_test",
                                    organization_id="org_test",
                                )

        assert result.success is True
        assert result.error_codes == []


class TestBuildBlockResultPassesErrorCodes:
    """Tests that build_block_result() passes error_codes to update_workflow_run_block()."""

    @pytest.mark.asyncio
    async def test_error_codes_passed_to_db_update(self) -> None:
        block = _make_file_parser_block()

        from skyvern.forge import app

        app.DATABASE.observer.update_workflow_run_block.reset_mock()

        result = await block.build_block_result(
            success=False,
            failure_reason="test failure",
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
            error_codes=["FILE_PARSER_ERROR"],
        )

        app.DATABASE.observer.update_workflow_run_block.assert_called_once()
        call_kwargs = app.DATABASE.observer.update_workflow_run_block.call_args[1]
        assert call_kwargs["error_codes"] == ["FILE_PARSER_ERROR"]
        assert result.error_codes == ["FILE_PARSER_ERROR"]

    @pytest.mark.asyncio
    async def test_none_error_codes_passed_to_db_update(self) -> None:
        block = _make_file_parser_block()

        from skyvern.forge import app

        app.DATABASE.observer.update_workflow_run_block.reset_mock()

        result = await block.build_block_result(
            success=True,
            failure_reason=None,
            status=BlockStatus.completed,
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

        app.DATABASE.observer.update_workflow_run_block.assert_called_once()
        call_kwargs = app.DATABASE.observer.update_workflow_run_block.call_args[1]
        assert call_kwargs["error_codes"] is None
        assert result.error_codes == []


class TestFileParserBlockRecordsOutputOnFailure:
    """
    SKY-7939: even when the block fails, an output should be recorded so it
    appears as `outputs.<block_label>` in the workflow run response, with a
    failed status and the error info. Without this, downstream consumers
    can't tell *which* block failed from `outputs` alone.
    """

    @staticmethod
    def _recorded_value(mock_record: AsyncMock) -> dict | None:
        """Return the `value` argument from the (single) record call, or None."""
        if not mock_record.await_count:
            return None
        call = mock_record.await_args
        if call is None:
            return None
        if "value" in call.kwargs:
            return call.kwargs["value"]
        # positional: (workflow_run_context, workflow_run_id, value)
        if len(call.args) >= 3:
            return call.args[2]
        return None

    def _assert_failure_output_shape(self, value: dict | None, failure_substring: str) -> None:
        assert value is not None, "record_output_parameter_value was not called on failure"
        assert value["status"] == BlockStatus.failed.value
        assert failure_substring.lower() in value["failure_reason"].lower()
        assert value["errors"], "errors list should be non-empty"
        assert value["errors"][0]["error_code"] == "FILE_PARSER_ERROR"
        assert value["errors"][0]["reasoning"] == value["failure_reason"]

    @pytest.mark.asyncio
    async def test_jinja_failure_records_failed_output(self) -> None:
        block = _make_file_parser_block(file_url="{{ invalid }}")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(
                FileParserBlock, "format_potential_template_parameters", side_effect=Exception("Jinja error")
            ):
                with patch.object(
                    FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock
                ) as mock_record:
                    result = await block.execute(
                        workflow_run_id="wr_test",
                        workflow_run_block_id="wrb_test",
                        organization_id="org_test",
                    )

        assert result.success is False
        recorded = self._recorded_value(mock_record)
        self._assert_failure_output_shape(recorded, "jinja")
        # build_block_result must also receive the same payload so it ends up on the WorkflowRunBlock row.
        assert result.output_parameter_value == recorded

    @pytest.mark.asyncio
    async def test_download_failure_records_failed_output(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/missing.csv")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    side_effect=Exception("Download failed"),
                ):
                    with patch.object(
                        FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock
                    ) as mock_record:
                        result = await block.execute(
                            workflow_run_id="wr_test",
                            workflow_run_block_id="wrb_test",
                            organization_id="org_test",
                        )

        assert result.success is False
        recorded = self._recorded_value(mock_record)
        self._assert_failure_output_shape(recorded, "download or validate")
        assert result.output_parameter_value == recorded

    @pytest.mark.asyncio
    async def test_unsupported_file_type_records_failed_output(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/file.csv")
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/test_file",
                ):
                    with patch.object(FileParserBlock, "_detect_file_type_from_url", return_value="unsupported_type"):
                        with patch.object(FileParserBlock, "validate_file_type"):
                            with patch.object(
                                FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock
                            ) as mock_record:
                                result = await block.execute(
                                    workflow_run_id="wr_test",
                                    workflow_run_block_id="wrb_test",
                                    organization_id="org_test",
                                )

        assert result.success is False
        recorded = self._recorded_value(mock_record)
        self._assert_failure_output_shape(recorded, "unsupported")
        assert result.output_parameter_value == recorded

    @pytest.mark.asyncio
    async def test_parse_failure_records_failed_output(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/statement.pdf", file_type=FileType.PDF)
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/statement.pdf",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(
                            FileParserBlock,
                            "_parse_pdf_file",
                            side_effect=Exception("No /Root object! - Is this really a PDF?"),
                        ):
                            with patch.object(
                                FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock
                            ) as mock_record:
                                result = await block.execute(
                                    workflow_run_id="wr_test",
                                    workflow_run_block_id="wrb_test",
                                    organization_id="org_test",
                                )

        assert result.success is False
        recorded = self._recorded_value(mock_record)
        self._assert_failure_output_shape(recorded, "parse")
        assert result.output_parameter_value == recorded

    @pytest.mark.asyncio
    async def test_ai_extraction_failure_records_failed_output(self) -> None:
        block = _make_file_parser_block(file_url="https://example.com/file.csv")
        block.json_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        mock_ctx = _mock_workflow_run_context()

        with patch.object(FileParserBlock, "get_workflow_run_context", return_value=mock_ctx):
            with patch.object(FileParserBlock, "format_potential_template_parameters"):
                with patch(
                    "skyvern.forge.sdk.workflow.models.block.download_file",
                    return_value="/tmp/test.csv",
                ):
                    with patch.object(FileParserBlock, "validate_file_type"):
                        with patch.object(FileParserBlock, "_parse_csv_file", return_value=[{"name": "Alice"}]):
                            with patch.object(FileParserBlock, "_extract_with_ai", side_effect=Exception("LLM error")):
                                with patch.object(
                                    FileParserBlock, "record_output_parameter_value", new_callable=AsyncMock
                                ) as mock_record:
                                    result = await block.execute(
                                        workflow_run_id="wr_test",
                                        workflow_run_block_id="wrb_test",
                                        organization_id="org_test",
                                    )

        assert result.success is False
        # AI extraction path is special: parsed CSV success may have called record once;
        # we only require that the *last* record call carries the failure output.
        assert mock_record.await_count >= 1
        last_call = mock_record.await_args_list[-1]
        recorded = last_call.kwargs.get("value") if "value" in last_call.kwargs else last_call.args[2]
        self._assert_failure_output_shape(recorded, "extract data with ai")
        assert result.output_parameter_value == recorded
