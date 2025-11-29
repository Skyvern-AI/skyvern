from typing import Any

import pytest

from skyvern.forge.sdk.api.llm.schema_validator import (
    fill_missing_fields,
    get_default_value_for_type,
    validate_and_fill_extraction_result,
    validate_data_against_schema,
    validate_schema,
)


class TestSchemaValidator:
    @pytest.fixture
    def medication_schema(self) -> dict[str, Any]:
        """Schema for medication extraction data."""
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Medication Name": {"type": "string"},
                    "NDC": {"type": "string"},
                    "quantity": {"type": "string"},
                    "facility": {"type": "string"},
                    "recoverydate": {"type": ["string", "null"]},
                    "isAllocation": {"type": "boolean"},
                    "ErrorMessage": {"type": ["string", "null"]},
                },
                "required": [
                    "Medication Name",
                    "NDC",
                    "quantity",
                    "facility",
                    "recoverydate",
                    "isAllocation",
                    "ErrorMessage",
                ],
            },
        }

    @pytest.fixture
    def complete_medication_data(self) -> list[dict[str, Any]]:
        """Complete medication data with all required fields."""
        return [
            {
                "Medication Name": "ACETAMINOPHEN 1000MG-30ML CHRY LIQ 237ML",
                "NDC": "00904-7481-59",
                "quantity": "0",
                "facility": "CHI-IL",
                "recoverydate": None,
                "isAllocation": False,
                "ErrorMessage": None,
            },
            {
                "Medication Name": "ACETAMINOPHEN CHERRY 160MG/5ML SOL 473ML",
                "NDC": "00904-7014-16",
                "quantity": "100",
                "facility": "CHI-IL",
                "recoverydate": None,
                "isAllocation": False,
                "ErrorMessage": None,
            },
        ]

    @pytest.fixture
    def incomplete_medication_data(self) -> list[dict[str, Any]]:
        """Incomplete medication data missing some required fields."""
        return [
            {
                "Medication Name": "ACETAMINOPHEN 1000MG-30ML CHRY LIQ 237ML",
                "NDC": "00904-7481-59",
                "quantity": "0",
                "facility": "CHI-IL",
                # Missing: recoverydate, isAllocation, ErrorMessage
            },
            {
                "Medication Name": "AMOXICILLIN 500MG CAPSULE 500",
                # Missing: NDC, quantity, facility, recoverydate, isAllocation, ErrorMessage
            },
        ]

    def test_get_default_value_for_string(self) -> None:
        """Test default value generation for string type."""
        assert get_default_value_for_type("string") is None

    def test_get_default_value_for_boolean(self) -> None:
        """Test default value generation for boolean type."""
        assert get_default_value_for_type("boolean") is False

    def test_get_default_value_for_array(self) -> None:
        """Test default value generation for array type."""
        assert get_default_value_for_type("array") == []

    def test_get_default_value_for_object(self) -> None:
        """Test default value generation for object type."""
        assert get_default_value_for_type("object") == {}

    def test_get_default_value_for_null(self) -> None:
        """Test default value generation for null type."""
        assert get_default_value_for_type("null") is None

    def test_get_default_value_for_type_list_with_null(self) -> None:
        """Test default value generation for type list containing null."""
        assert get_default_value_for_type(["string", "null"]) is None
        assert get_default_value_for_type(["null", "string"]) is None

    def test_get_default_value_for_type_list_all_null(self) -> None:
        """Test default value generation for type list with only null."""
        assert get_default_value_for_type(["null"]) is None

    def test_get_default_value_for_uppercase_type(self) -> None:
        """Test default value generation for uppercase type names."""
        assert get_default_value_for_type("STRING") is None
        assert get_default_value_for_type("NUMBER") == 0
        assert get_default_value_for_type("INTEGER") == 0
        assert get_default_value_for_type("BOOLEAN") is False
        assert get_default_value_for_type("ARRAY") == []
        assert get_default_value_for_type("OBJECT") == {}
        assert get_default_value_for_type("NULL") is None

    def test_get_default_value_for_mixed_case_type(self) -> None:
        """Test default value generation for mixed case type names."""
        assert get_default_value_for_type("String") is None
        assert get_default_value_for_type("Boolean") is False
        assert get_default_value_for_type(["STRING", "null"]) is None
        assert get_default_value_for_type(["NULL", "STRING"]) is None

    def test_fill_missing_fields_complete_data(
        self, medication_schema: dict[str, Any], complete_medication_data: list[dict[str, Any]]
    ) -> None:
        """Test that complete data passes through unchanged."""
        result = fill_missing_fields(complete_medication_data, medication_schema)
        assert result == complete_medication_data

    def test_fill_missing_fields_incomplete_data(
        self, medication_schema: dict[str, Any], incomplete_medication_data: list[dict[str, Any]]
    ) -> None:
        """Test that missing required fields are filled with defaults."""
        result = fill_missing_fields(incomplete_medication_data, medication_schema)

        # First item should have missing fields filled
        assert result[0]["Medication Name"] == "ACETAMINOPHEN 1000MG-30ML CHRY LIQ 237ML"
        assert result[0]["NDC"] == "00904-7481-59"
        assert result[0]["quantity"] == "0"
        assert result[0]["facility"] == "CHI-IL"
        assert result[0]["recoverydate"] is None  # Default for ["string", "null"]
        assert result[0]["isAllocation"] is False  # Default for boolean
        assert result[0]["ErrorMessage"] is None  # Default for ["string", "null"]

        # Second item should have all missing fields filled
        assert result[1]["Medication Name"] == "AMOXICILLIN 500MG CAPSULE 500"
        assert result[1]["NDC"] is None  # Default for string
        assert result[1]["quantity"] is None  # Default for string
        assert result[1]["facility"] is None  # Default for string
        assert result[1]["recoverydate"] is None  # Default for ["string", "null"]
        assert result[1]["isAllocation"] is False  # Default for boolean
        assert result[1]["ErrorMessage"] is None  # Default for ["string", "null"]

    def test_fill_missing_fields_with_error_message(self, medication_schema: dict[str, Any]) -> None:
        """Test filling fields when ErrorMessage has a value."""
        data = [
            {
                "Medication Name": "TEST MEDICATION",
                "NDC": "12345-678-90",
                "quantity": "50",
                "facility": "TEST-FACILITY",
                "recoverydate": "2024-01-01",
                "isAllocation": True,
                "ErrorMessage": "Some error occurred",
            }
        ]

        result = fill_missing_fields(data, medication_schema)
        assert result[0]["ErrorMessage"] == "Some error occurred"

    def test_fill_missing_fields_empty_array(self, medication_schema: dict[str, Any]) -> None:
        """Test handling of empty array."""
        result = fill_missing_fields([], medication_schema)
        assert result == []

    def test_fill_missing_fields_invalid_data_type(self, medication_schema: dict[str, Any]) -> None:
        """Test handling when data is not an array."""
        # When data is not a list, it should be converted to empty array
        result = fill_missing_fields("not an array", medication_schema)
        assert result == []

    def test_fill_missing_fields_nested_object_missing_fields(self, medication_schema: dict[str, Any]) -> None:
        """Test that nested objects have missing fields filled."""
        data = [
            {
                "Medication Name": "TEST MEDICATION",
                # All other fields missing
            }
        ]

        result = fill_missing_fields(data, medication_schema)
        assert len(result) == 1
        assert "NDC" in result[0]
        assert "quantity" in result[0]
        assert "facility" in result[0]
        assert "recoverydate" in result[0]
        assert "isAllocation" in result[0]
        assert "ErrorMessage" in result[0]

    def test_validate_and_fill_extraction_result_with_schema(
        self, medication_schema: dict[str, Any], incomplete_medication_data: list[dict[str, Any]]
    ) -> None:
        """Test validate_and_fill_extraction_result with medication schema."""
        result = validate_and_fill_extraction_result(incomplete_medication_data, medication_schema)

        # Verify all required fields are present in all items
        for item in result:
            assert "Medication Name" in item
            assert "NDC" in item
            assert "quantity" in item
            assert "facility" in item
            assert "recoverydate" in item
            assert "isAllocation" in item
            assert "ErrorMessage" in item

    def test_validate_and_fill_extraction_result_no_schema(self) -> None:
        """Test that data passes through unchanged when no schema is provided."""
        data = {"some": "data"}
        result = validate_and_fill_extraction_result(data, None)
        assert result == data

    def test_validate_and_fill_extraction_result_with_exception(self, medication_schema: dict[str, Any]) -> None:
        """Test that original data is returned if validation fails."""
        # This should not raise an exception, but return original data
        invalid_data = "not a valid structure"
        result = validate_and_fill_extraction_result(invalid_data, medication_schema)
        # Should return empty array since invalid_data gets converted
        assert result == []

    def test_fill_missing_fields_preserves_existing_values(self, medication_schema: dict[str, Any]) -> None:
        """Test that existing values are preserved and not overwritten."""
        data = [
            {
                "Medication Name": "EXISTING NAME",
                "NDC": "EXISTING-NDC",
                "quantity": "999",
                "facility": "EXISTING-FACILITY",
                "recoverydate": "2024-12-31",
                "isAllocation": True,
                "ErrorMessage": "Existing error",
            }
        ]

        result = fill_missing_fields(data, medication_schema)

        # All original values should be preserved
        assert result[0]["Medication Name"] == "EXISTING NAME"
        assert result[0]["NDC"] == "EXISTING-NDC"
        assert result[0]["quantity"] == "999"
        assert result[0]["facility"] == "EXISTING-FACILITY"
        assert result[0]["recoverydate"] == "2024-12-31"
        assert result[0]["isAllocation"] is True
        assert result[0]["ErrorMessage"] == "Existing error"

    def test_fill_missing_fields_nullable_object_with_null(self) -> None:
        """Test handling of nullable object type when data is null."""
        schema = {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }

        # When data is null, it should remain null (valid for nullable type)
        result = fill_missing_fields(None, schema)
        assert result is None

    def test_fill_missing_fields_nullable_object_with_object(self) -> None:
        """Test handling of nullable object type when data is an object with missing fields."""
        schema = {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }

        # When data is an object (not null), missing required fields should be filled
        data = {"name": "John"}  # Missing 'age'
        result = fill_missing_fields(data, schema)

        # With the fix, missing required fields should be filled
        assert result == {"name": "John", "age": 0}
        assert "name" in result
        assert "age" in result

    def test_fill_missing_fields_nullable_array_with_null(self) -> None:
        """Test handling of nullable array type when data is null."""
        schema = {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
            },
        }

        # When data is null, it should remain null
        result = fill_missing_fields(None, schema)
        assert result is None

    def test_fill_missing_fields_nullable_array_with_array(self) -> None:
        """Test handling of nullable array type when data is an array."""
        schema = {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["id", "name"],
            },
        }

        # When data is an array (not null), items should be validated
        data = [{"id": "1"}]  # Missing 'name'
        result = fill_missing_fields(data, schema)

        # With the fix, missing fields in array items should be filled
        assert len(result) == 1
        assert result[0] == {"id": "1", "name": None}
        assert "id" in result[0]
        assert "name" in result[0]

    def test_validate_schema_valid(self) -> None:
        """Test that valid schemas return True."""
        valid_schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        assert validate_schema(valid_schema) is True

    def test_validate_schema_invalid(self) -> None:
        """Test that invalid schemas return False."""
        invalid_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": "not_a_number",  # Should be a number
                }
            },
        }
        # Should return False for invalid schema
        assert validate_schema(invalid_schema) is False

    def test_validate_schema_none(self) -> None:
        """Test that None schema is considered valid."""
        assert validate_schema(None) is True

    def test_validate_schema_string(self) -> None:
        """Test that string schema is considered valid (permissive)."""
        assert validate_schema("some_string") is True

    def test_validate_schema_list(self) -> None:
        """Test that list schema is considered valid (permissive)."""
        assert validate_schema([]) is True

    def test_validate_data_against_schema_valid(self) -> None:
        """Test validation of valid data against schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        data = {"name": "John", "age": 30}
        errors = validate_data_against_schema(data, schema)
        assert errors == []

    def test_validate_data_against_schema_missing_required(self) -> None:
        """Test validation when required fields are missing."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        data = {"name": "John"}  # Missing 'age'
        errors = validate_data_against_schema(data, schema)
        assert len(errors) > 0
        assert any("age" in error for error in errors)

    def test_validate_data_against_schema_wrong_type(self) -> None:
        """Test validation when data has wrong type."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        }
        data = {"name": "John", "age": "thirty"}  # age should be integer
        errors = validate_data_against_schema(data, schema)
        assert len(errors) > 0
        assert any("age" in error for error in errors)

    def test_validate_data_against_schema_array(self) -> None:
        """Test validation of array data."""
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
            },
        }
        data = [{"id": "1"}, {"id": "2"}]
        errors = validate_data_against_schema(data, schema)
        assert errors == []

    def test_validate_and_fill_with_jsonschema_validation(self, medication_schema: dict[str, Any]) -> None:
        """Test that validate_and_fill uses jsonschema for validation."""
        # Data with all required fields filled correctly
        data = [
            {
                "Medication Name": "TEST MED",
                "NDC": "12345",
                "quantity": "10",
                "facility": "TEST",
                "recoverydate": None,
                "isAllocation": False,
                "ErrorMessage": None,
            }
        ]

        result = validate_and_fill_extraction_result(data, medication_schema)
        assert result == data

    def test_validate_and_fill_with_invalid_schema(self) -> None:
        """Test that validate_and_fill returns data as-is for invalid schemas."""
        # Create a schema that will fail Draft202012Validator.check_schema
        invalid_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": "not_a_number",  # Should be a number
                }
            },
        }

        data = {"name": "test"}
        # Should return data as-is without transformations when schema is invalid
        result = validate_and_fill_extraction_result(data, invalid_schema)
        assert result == data

    def test_filter_invalid_array_items_with_string(self, medication_schema: dict[str, Any]) -> None:
        """Test that array items created from invalid data (strings) are filtered out."""
        # Simulate LLM response with a string mixed in the array
        data = [
            {
                "Medication Name": "ACETAMINOPHEN 500MG",
                "NDC": "12345-678-90",
                "quantity": "100",
                "facility": "TEST-FACILITY",
                "recoverydate": None,
                "isAllocation": False,
                "ErrorMessage": None,
            },
            "This is an invalid string that should be filtered out",
            {
                "Medication Name": "IBUPROFEN 200MG",
                "NDC": "98765-432-10",
                "quantity": "50",
                "facility": "TEST-FACILITY",
                "recoverydate": "2024-01-01",
                "isAllocation": True,
                "ErrorMessage": None,
            },
        ]

        result = validate_and_fill_extraction_result(data, medication_schema)

        # Should have only 2 valid records (the string should be filtered out)
        assert len(result) == 2
        assert result[0]["Medication Name"] == "ACETAMINOPHEN 500MG"
        assert result[1]["Medication Name"] == "IBUPROFEN 200MG"

    def test_filter_invalid_array_items_preserves_valid_defaults(self, medication_schema: dict[str, Any]) -> None:
        """Test that records with some valid data are preserved even if some fields are defaults."""
        data = [
            {
                "Medication Name": "VALID MEDICATION",
                "NDC": "12345-678-90",
                # Missing other required fields - should be filled with defaults but NOT filtered
            }
        ]

        result = validate_and_fill_extraction_result(data, medication_schema)

        # Should preserve the record because it has meaningful data
        assert len(result) == 1
        assert result[0]["Medication Name"] == "VALID MEDICATION"
        assert result[0]["NDC"] == "12345-678-90"
        assert result[0]["quantity"] is None  # Filled with default
        assert result[0]["facility"] is None  # Filled with default
