import pytest

from skyvern.forge.sdk.api.llm.schema_validator import (
    fill_missing_fields,
    get_default_value_for_type,
    validate_and_fill_extraction_result,
)


class TestSchemaValidator:
    @pytest.fixture
    def medication_schema(self):
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
    def complete_medication_data(self):
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
    def incomplete_medication_data(self):
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

    def test_get_default_value_for_string(self):
        """Test default value generation for string type."""
        assert get_default_value_for_type("string") == ""

    def test_get_default_value_for_boolean(self):
        """Test default value generation for boolean type."""
        assert get_default_value_for_type("boolean") is False

    def test_get_default_value_for_array(self):
        """Test default value generation for array type."""
        assert get_default_value_for_type("array") == []

    def test_get_default_value_for_object(self):
        """Test default value generation for object type."""
        assert get_default_value_for_type("object") == {}

    def test_get_default_value_for_null(self):
        """Test default value generation for null type."""
        assert get_default_value_for_type("null") is None

    def test_get_default_value_for_type_list_with_null(self):
        """Test default value generation for type list containing null."""
        assert get_default_value_for_type(["string", "null"]) == ""
        assert get_default_value_for_type(["null", "string"]) == ""

    def test_get_default_value_for_type_list_all_null(self):
        """Test default value generation for type list with only null."""
        assert get_default_value_for_type(["null"]) is None

    def test_fill_missing_fields_complete_data(self, medication_schema, complete_medication_data):
        """Test that complete data passes through unchanged."""
        result = fill_missing_fields(complete_medication_data, medication_schema)
        assert result == complete_medication_data

    def test_fill_missing_fields_incomplete_data(self, medication_schema, incomplete_medication_data):
        """Test that missing required fields are filled with defaults."""
        result = fill_missing_fields(incomplete_medication_data, medication_schema)

        # First item should have missing fields filled
        assert result[0]["Medication Name"] == "ACETAMINOPHEN 1000MG-30ML CHRY LIQ 237ML"
        assert result[0]["NDC"] == "00904-7481-59"
        assert result[0]["quantity"] == "0"
        assert result[0]["facility"] == "CHI-IL"
        assert result[0]["recoverydate"] == ""  # Default for ["string", "null"]
        assert result[0]["isAllocation"] is False  # Default for boolean
        assert result[0]["ErrorMessage"] == ""  # Default for ["string", "null"]

        # Second item should have all missing fields filled
        assert result[1]["Medication Name"] == "AMOXICILLIN 500MG CAPSULE 500"
        assert result[1]["NDC"] == ""  # Default for string
        assert result[1]["quantity"] == ""  # Default for string
        assert result[1]["facility"] == ""  # Default for string
        assert result[1]["recoverydate"] == ""  # Default for ["string", "null"]
        assert result[1]["isAllocation"] is False  # Default for boolean
        assert result[1]["ErrorMessage"] == ""  # Default for ["string", "null"]

    def test_fill_missing_fields_with_error_message(self, medication_schema):
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

    def test_fill_missing_fields_empty_array(self, medication_schema):
        """Test handling of empty array."""
        result = fill_missing_fields([], medication_schema)
        assert result == []

    def test_fill_missing_fields_invalid_data_type(self, medication_schema):
        """Test handling when data is not an array."""
        # When data is not a list, it should be converted to empty array
        result = fill_missing_fields("not an array", medication_schema)
        assert result == []

    def test_fill_missing_fields_nested_object_missing_fields(self, medication_schema):
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

    def test_validate_and_fill_extraction_result_with_schema(self, medication_schema, incomplete_medication_data):
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

    def test_validate_and_fill_extraction_result_no_schema(self):
        """Test that data passes through unchanged when no schema is provided."""
        data = {"some": "data"}
        result = validate_and_fill_extraction_result(data, None)
        assert result == data

    def test_validate_and_fill_extraction_result_with_exception(self, medication_schema):
        """Test that original data is returned if validation fails."""
        # This should not raise an exception, but return original data
        invalid_data = "not a valid structure"
        result = validate_and_fill_extraction_result(invalid_data, medication_schema)
        # Should return empty array since invalid_data gets converted
        assert result == []

    def test_fill_missing_fields_preserves_existing_values(self, medication_schema):
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

    def test_fill_missing_fields_nullable_object_with_null(self):
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

    def test_fill_missing_fields_nullable_object_with_object(self):
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

    def test_fill_missing_fields_nullable_array_with_null(self):
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

    def test_fill_missing_fields_nullable_array_with_array(self):
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
        assert result[0] == {"id": "1", "name": ""}
        assert "id" in result[0]
        assert "name" in result[0]
