import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from skyvern.forge.sdk.workflow.models.block import FileParserBlock, FileType
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter


class TestFileParserBlock:
    @pytest.fixture
    def file_parser_block(self):
        """Create a basic FileParserBlock instance for testing."""
        # Create a mock OutputParameter with all required fields
        mock_output_parameter = MagicMock(spec=OutputParameter)
        mock_output_parameter.parameter_type = "output"
        mock_output_parameter.key = "test_output"
        mock_output_parameter.output_parameter_id = "test_id"
        mock_output_parameter.workflow_id = "test_workflow_id"
        mock_output_parameter.created_at = datetime.now()
        mock_output_parameter.modified_at = datetime.now()
        mock_output_parameter.deleted_at = None

        return FileParserBlock(
            label="test_parser", output_parameter=mock_output_parameter, file_url="test.csv", file_type=FileType.CSV
        )

    @pytest.fixture
    def csv_file(self):
        """Create a temporary CSV file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("name,age,city\nJohn,30,New York\nJane,25,Boston")
            temp_file = f.name

        yield temp_file
        os.unlink(temp_file)

    @pytest.fixture
    def excel_file(self):
        """Create a temporary Excel file for testing."""
        df = pd.DataFrame({"name": ["John", "Jane"], "age": [30, 25], "city": ["New York", "Boston"]})

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            df.to_excel(f.name, index=False)
            temp_file = f.name

        yield temp_file
        os.unlink(temp_file)

    def test_file_type_enum_values(self):
        """Test that FileType enum has the expected values."""
        assert FileType.CSV == "csv"
        assert FileType.EXCEL == "excel"
        assert FileType.PDF == "pdf"

    def test_file_parser_block_initialization(self, file_parser_block):
        """Test that FileParserBlock initializes correctly."""
        assert file_parser_block.label == "test_parser"
        assert file_parser_block.file_url == "test.csv"
        assert file_parser_block.file_type == FileType.CSV
        assert file_parser_block.json_schema is None

    def test_file_parser_block_with_schema(self):
        """Test that FileParserBlock can be initialized with a schema."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}

        # Create a mock OutputParameter
        mock_output_parameter = MagicMock(spec=OutputParameter)
        mock_output_parameter.parameter_type = "output"
        mock_output_parameter.key = "test_output"
        mock_output_parameter.output_parameter_id = "test_id"
        mock_output_parameter.workflow_id = "test_workflow_id"
        mock_output_parameter.created_at = datetime.now()
        mock_output_parameter.modified_at = datetime.now()
        mock_output_parameter.deleted_at = None

        block = FileParserBlock(
            label="test_parser",
            output_parameter=mock_output_parameter,
            file_url="test.csv",
            file_type=FileType.CSV,
            json_schema=schema,
        )

        assert block.json_schema == schema

    @pytest.mark.asyncio
    async def test_parse_csv_file(self, file_parser_block, csv_file):
        """Test CSV file parsing."""
        result = await file_parser_block._parse_csv_file(csv_file)

        expected = [{"name": "John", "age": "30", "city": "New York"}, {"name": "Jane", "age": "25", "city": "Boston"}]

        assert result == expected

    @pytest.mark.asyncio
    async def test_parse_excel_file(self, file_parser_block, excel_file):
        """Test Excel file parsing."""
        result = await file_parser_block._parse_excel_file(excel_file)

        expected = [{"name": "John", "age": 30, "city": "New York"}, {"name": "Jane", "age": 25, "city": "Boston"}]

        assert result == expected

    @pytest.mark.asyncio
    async def test_validate_csv_file_type(self, file_parser_block, csv_file):
        """Test CSV file type validation."""
        # Should not raise an exception
        file_parser_block.validate_file_type("test.csv", csv_file)

    @pytest.mark.asyncio
    async def test_validate_excel_file_type(self, file_parser_block, excel_file):
        """Test Excel file type validation."""
        file_parser_block.file_type = FileType.EXCEL
        # Should not raise an exception
        file_parser_block.validate_file_type("test.xlsx", excel_file)

    @pytest.mark.asyncio
    async def test_validate_invalid_csv_file(self, file_parser_block):
        """Test validation of invalid CSV file."""
        # Create a binary file that's definitely not CSV
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as f:
            f.write(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f")
            temp_file = f.name

        try:
            with pytest.raises(Exception):
                file_parser_block.validate_file_type("test.csv", temp_file)
        finally:
            os.unlink(temp_file)

    @pytest.mark.asyncio
    async def test_extract_with_ai_with_schema(self, file_parser_block):
        """Test AI extraction with a provided schema."""
        schema = {
            "type": "object",
            "properties": {
                "extracted_data": {
                    "type": "object",
                    "properties": {
                        "names": {"type": "array", "items": {"type": "string"}},
                        "total_count": {"type": "integer"},
                    },
                }
            },
        }

        file_parser_block.json_schema = schema

        # Mock the LLM response
        mock_response = {"extracted_data": {"names": ["John", "Jane"], "total_count": 2}}

        with patch("skyvern.forge.sdk.workflow.models.block.app.LLM_API_HANDLER") as mock_llm:
            mock_llm.return_value = mock_response

            with patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt") as mock_prompt:
                mock_prompt.return_value = "mocked prompt"

                result = await file_parser_block._extract_with_ai([{"name": "John"}, {"name": "Jane"}], MagicMock())

                assert result == mock_response
                mock_llm.assert_called_once()
                mock_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_extract_with_ai_without_schema(self, file_parser_block):
        """Test AI extraction without a provided schema (should use default)."""
        # Mock the LLM response
        mock_response = {"output": {"summary": "Extracted data from file"}}

        with patch("skyvern.forge.sdk.workflow.models.block.app.LLM_API_HANDLER") as mock_llm:
            mock_llm.return_value = mock_response

            with patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt") as mock_prompt:
                mock_prompt.return_value = "mocked prompt"

                result = await file_parser_block._extract_with_ai("Some text content", MagicMock())

                assert result == mock_response
                # Should have set a default schema
                assert file_parser_block.json_schema is not None
                mock_llm.assert_called_once()
                mock_prompt.assert_called_once()
