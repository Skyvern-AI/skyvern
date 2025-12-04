import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.workflow.models.block import FileParserBlock, FileType
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter


@pytest.fixture(scope="module", autouse=True)
def setup_forge_app():
    start_forge_app()
    yield


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

    @pytest.fixture
    def tsv_file(self):
        """Create a temporary TSV file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tsv", delete=False) as f:
            f.write("name\tage\tcity\nJohn\t30\tNew York\nJane\t25\tBoston")
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
    async def test_parse_tsv_file(self, file_parser_block, tsv_file):
        """Test TSV file parsing."""
        result = await file_parser_block._parse_csv_file(tsv_file)

        expected = [{"name": "John", "age": "30", "city": "New York"}, {"name": "Jane", "age": "25", "city": "Boston"}]

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

        with patch.object(object.__getattribute__(app, "_inst"), "LLM_API_HANDLER") as mock_llm:
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

        with patch.object(object.__getattribute__(app, "_inst"), "LLM_API_HANDLER") as mock_llm:
            mock_llm.return_value = mock_response

            with patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt") as mock_prompt:
                mock_prompt.return_value = "mocked prompt"

                result = await file_parser_block._extract_with_ai("Some text content", MagicMock())

                assert result == mock_response
                # Should NOT mutate the instance - json_schema should remain None
                assert file_parser_block.json_schema is None
                mock_llm.assert_called_once()
                mock_prompt.assert_called_once()

    def test_detect_file_type_from_url(self, file_parser_block):
        """Test file type detection based on URL extension."""
        # Test Excel files
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.xlsx") == FileType.EXCEL
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.xls") == FileType.EXCEL
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.xlsm") == FileType.EXCEL

        # Test PDF files
        assert file_parser_block._detect_file_type_from_url("https://example.com/document.pdf") == FileType.PDF

        # Test CSV files (default)
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.csv") == FileType.CSV
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.tsv") == FileType.CSV
        assert file_parser_block._detect_file_type_from_url("https://example.com/data.txt") == FileType.CSV
        assert file_parser_block._detect_file_type_from_url("https://example.com/data") == FileType.CSV

    def test_clean_dataframe_for_json(self, file_parser_block):
        """Test DataFrame cleaning for JSON serialization."""
        # Create a DataFrame with NaN, NaT, and timestamp values
        df = pd.DataFrame(
            {
                "OrderDate": ["2018-01-01", pd.NaT, "2018-01-03"],
                "Region": ["North", "South", pd.NA],
                "Sales": [1000.0, pd.NA, 3000.0],
                "Timestamp": [pd.Timestamp("2018-01-01"), pd.NaT, pd.Timestamp("2018-01-03")],
            }
        )

        # Clean the DataFrame
        result = file_parser_block._clean_dataframe_for_json(df)

        # Check that NaN and NaT values are converted to "nan" string
        assert result[0]["OrderDate"] == "2018-01-01"
        assert result[0]["Region"] == "North"
        assert result[0]["Sales"] == 1000.0
        assert result[0]["Timestamp"] == "2018-01-01T00:00:00"

        assert result[1]["OrderDate"] == "nan"
        assert result[1]["Region"] == "South"
        assert result[1]["Sales"] == "nan"
        assert result[1]["Timestamp"] == "nan"

        assert result[2]["OrderDate"] == "2018-01-03"
        assert result[2]["Region"] == "nan"
        assert result[2]["Sales"] == 3000.0
        assert result[2]["Timestamp"] == "2018-01-03T00:00:00"
