from typing import Any

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

LOG = structlog.get_logger()


_TYPE_DEFAULT_FACTORIES: dict[str, Any] = {
    "string": lambda: None,
    "number": lambda: 0,
    "integer": lambda: 0,
    "boolean": lambda: False,
    "array": list,
    "object": dict,
    "null": lambda: None,
}


def _resolve_schema_type(schema_type: str | list[Any] | None, path: str) -> str | None:
    """Normalize a schema type definition to a single string value."""
    if isinstance(schema_type, list):
        non_null_types = [str(t).lower() for t in schema_type if str(t).lower() != "null"]
        if not non_null_types:
            return "null"

        if len(non_null_types) > 1:
            LOG.warning(
                "Multiple non-null types in schema, using first one",
                path=path,
                types=non_null_types,
            )
        return non_null_types[0]

    return str(schema_type).lower() if schema_type is not None else None


def get_default_value_for_type(schema_type: str | list[Any] | None, path: str = "root") -> Any:
    """Get a default value based on JSON schema type."""
    normalized_type = _resolve_schema_type(schema_type, path)
    if normalized_type is None:
        return None

    factory = _TYPE_DEFAULT_FACTORIES.get(normalized_type)
    return factory() if callable(factory) else None


def fill_missing_fields(data: Any, schema: dict[str, Any] | list | str | None, path: str = "root") -> Any:
    """
    Recursively fill missing fields in data based on the schema.

    Args:
        data: The data to validate and fill
        schema: The JSON schema to validate against
        path: Current path in the data structure (for logging)

    Returns:
        The data with missing fields filled with default values
    """
    if schema is None:
        return data

    if isinstance(schema, (str, list)):
        LOG.debug("Schema is permissive", path=path, schema=schema)
        return data

    schema_type = _resolve_schema_type(schema.get("type"), path)
    raw_schema_type = schema.get("type")

    if schema_type == "null" and data is None:
        LOG.debug("Data is None and schema allows null type, keeping as None", path=path)
        return None

    # Check if null is allowed in the schema type
    is_nullable = isinstance(raw_schema_type, list) and "null" in raw_schema_type

    if schema_type == "object" or "properties" in schema:
        # If data is None and schema allows null, keep it as None
        if data is None and is_nullable:
            LOG.debug("Data is None and schema allows null, keeping as None", path=path)
            return None

        if not isinstance(data, dict):
            LOG.warning(
                "Expected object but got different type, creating empty object",
                path=path,
                data_type=type(data).__name__,
            )
            data = {}

        properties = schema.get("properties", {})
        required_fields = set(schema.get("required", []))

        for field_name, field_schema in properties.items():
            field_path = f"{path}.{field_name}"

            if field_name not in data:
                if field_name in required_fields:
                    default_value = field_schema.get(
                        "default", get_default_value_for_type(field_schema.get("type"), field_path)
                    )
                    LOG.info(
                        "Filling missing required field with default value",
                        path=field_path,
                        default_value=default_value,
                    )
                    data[field_name] = default_value
                else:
                    LOG.debug("Skipping optional missing field", path=field_path)
                    continue

            data[field_name] = fill_missing_fields(data[field_name], field_schema, field_path)

        return data

    if schema_type == "array":
        # If data is None and schema allows null, keep it as None
        if data is None and is_nullable:
            LOG.debug("Data is None and schema allows null, keeping as None", path=path)
            return None

        if not isinstance(data, list):
            LOG.warning(
                "Expected array but got different type, creating empty array",
                path=path,
                data_type=type(data).__name__,
            )
            return []

        items_schema = schema.get("items")
        if not items_schema:
            return data

        return [fill_missing_fields(item, items_schema, f"{path}[{idx}]") for idx, item in enumerate(data)]

    return data


def validate_schema(schema: dict[str, Any] | list | str | None) -> bool:
    """
    Validate that the schema itself is a valid JSON Schema.

    Args:
        schema: The JSON schema to validate

    Returns:
        True if the schema is valid, False otherwise
    """
    if schema is None or isinstance(schema, (str, list)):
        return True

    try:
        Draft202012Validator.check_schema(schema)
        return True
    except SchemaError as e:
        LOG.warning("Invalid JSON schema, will return data as-is", error=str(e), schema=schema)
        return False


def validate_data_against_schema(data: Any, schema: dict[str, Any]) -> list[str]:
    """
    Validate data against a JSON schema using Draft202012Validator.

    Args:
        data: The data to validate
        schema: The JSON schema to validate against

    Returns:
        List of validation error messages (empty if valid)
    """
    validator = Draft202012Validator(schema)
    errors = []

    for error in validator.iter_errors(data):
        error_path = ".".join(str(p) for p in error.path) if error.path else "root"
        errors.append(f"{error_path}: {error.message}")

    return errors


def _is_all_default_values(data: dict[str, Any], schema: dict[str, Any]) -> bool:
    """
    Check if a dict contains only default values (indicating it was created from invalid data).

    Args:
        data: The data object to check
        schema: The schema defining the expected structure

    Returns:
        True if all values are defaults, False otherwise
    """
    if not isinstance(data, dict):
        return False

    properties = schema.get("properties", {})
    if not properties:
        return False

    # Check each property against its default value
    for field_name, field_schema in properties.items():
        if field_name not in data:
            continue

        field_value = data[field_name]
        field_type = _resolve_schema_type(field_schema.get("type"), f"check.{field_name}")
        default_value = get_default_value_for_type(field_type)

        # If any field has a non-default value, the record is meaningful
        if field_value != default_value:
            return False

    return True


def _filter_invalid_array_items(data: list[Any], schema: dict[str, Any]) -> list[Any]:
    """
    Filter out array items that are all default values (created from invalid data like strings).

    Args:
        data: The array data to filter
        schema: The array schema

    Returns:
        Filtered array with invalid items removed
    """
    items_schema = schema.get("items")
    if not items_schema or not isinstance(items_schema, dict):
        return data

    # Only filter if items are objects
    if items_schema.get("type") not in ("object", ["object", "null"]):
        return data

    filtered = []
    removed_count = 0

    for item in data:
        if isinstance(item, dict) and _is_all_default_values(item, items_schema):
            removed_count += 1
            LOG.info("Filtering out invalid array item with all default values", item=item)
        else:
            filtered.append(item)

    if removed_count > 0:
        LOG.info(f"Removed {removed_count} invalid array items")

    return filtered


def validate_and_fill_extraction_result(
    extraction_result: dict[str, Any],
    schema: dict[str, Any] | list | str | None,
) -> dict[str, Any]:
    """
    Validate extraction result against schema and fill missing fields with defaults.

    This function handles malformed JSON responses from LLMs by:
    1. Validating the schema itself is valid JSON Schema (returns data as-is if invalid)
    2. Filling in missing required fields with appropriate default values
    3. Validating the filled structure against the provided schema using jsonschema
    4. Preserving optional fields that are present

    Args:
        extraction_result: The extraction result from the LLM
        schema: The JSON schema that defines the expected structure

    Returns:
        The validated and filled extraction result, or the original data if schema is invalid
    """
    if schema is None:
        LOG.debug("No schema provided, returning extraction result as-is")
        return extraction_result

    if not validate_schema(schema):
        LOG.info("Schema is invalid, returning extraction result as-is without transformations")
        return extraction_result

    LOG.info("Validating and filling extraction result against schema")

    try:
        filled_result = fill_missing_fields(extraction_result, schema)

        # Filter out invalid array items if the schema is for an array
        if isinstance(schema, dict) and schema.get("type") == "array" and isinstance(filled_result, list):
            filled_result = _filter_invalid_array_items(filled_result, schema)

        if isinstance(schema, dict):
            validation_errors = validate_data_against_schema(filled_result, schema)
            if validation_errors:
                LOG.warning(
                    "Validation errors found after filling",
                    errors=validation_errors,
                )

        LOG.info("Successfully validated and filled extraction result")
        return filled_result
    except Exception as e:
        LOG.error(
            "Failed to validate and fill extraction result",
            error=str(e),
            exc_info=True,
        )
        return extraction_result
