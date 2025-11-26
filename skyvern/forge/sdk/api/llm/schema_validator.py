from typing import Any

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from skyvern.exceptions import InvalidSchemaError

LOG = structlog.get_logger()


_TYPE_DEFAULT_FACTORIES: dict[str, Any] = {
    "string": lambda: "",
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
        non_null_types = [str(t) for t in schema_type if t != "null"]
        if not non_null_types:
            return "null"

        if len(non_null_types) > 1:
            LOG.warning(
                "Multiple non-null types in schema, using first one",
                path=path,
                types=non_null_types,
            )
        return non_null_types[0]

    return str(schema_type) if schema_type is not None else None


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
                "Schema expects object type but received incompatible type, creating empty object to satisfy schema requirements and continue validation",
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


def validate_schema(schema: dict[str, Any] | list | str | None) -> None:
    """
    Validate that the schema itself is a valid JSON Schema.

    Args:
        schema: The JSON schema to validate

    Raises:
        InvalidSchemaError: If the schema is invalid with details about why
    """
    if schema is None or isinstance(schema, (str, list)):
        return

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        error_message = f"Invalid JSON schema: {str(e)}"
        LOG.error("Invalid JSON schema", error=str(e), schema=schema)
        raise InvalidSchemaError(error_message, [str(e)])


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


def validate_and_fill_extraction_result(
    extraction_result: dict[str, Any],
    schema: dict[str, Any] | list | str | None,
) -> dict[str, Any]:
    """
    Validate extraction result against schema and fill missing fields with defaults.

    This function handles malformed JSON responses from LLMs by:
    1. Validating the schema itself is valid JSON Schema (raises InvalidSchemaError if not)
    2. Filling in missing required fields with appropriate default values
    3. Validating the filled structure against the provided schema using jsonschema
    4. Preserving optional fields that are present

    Args:
        extraction_result: The extraction result from the LLM
        schema: The JSON schema that defines the expected structure

    Returns:
        The validated and filled extraction result

    Raises:
        InvalidSchemaError: If the provided schema is invalid. This allows the FE to notify
                           the user about schema issues and that type correctness cannot be guaranteed.
    """
    if schema is None:
        LOG.debug("No schema provided, returning extraction result as-is")
        return extraction_result

    validate_schema(schema)
    LOG.info("Validating and filling extraction result against schema")

    try:
        filled_result = fill_missing_fields(extraction_result, schema)

        if isinstance(schema, dict):
            validation_errors = validate_data_against_schema(filled_result, schema)
            if validation_errors:
                LOG.warning(
                    "Validation errors found after filling",
                    errors=validation_errors,
                )

        LOG.info("Successfully validated and filled extraction result")
        return filled_result
    except InvalidSchemaError:
        raise
    except Exception as e:
        LOG.error(
            "Failed to validate and fill extraction result",
            error=str(e),
            exc_info=True,
        )
        return extraction_result
