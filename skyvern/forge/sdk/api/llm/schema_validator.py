from typing import Any

import structlog

LOG = structlog.get_logger()


def get_default_value_for_type(schema_type: str | list[Any]) -> Any:
    """
    Get a default value based on JSON schema type.

    Args:
        schema_type: The JSON schema type (string or list of types)

    Returns:
        An appropriate default value for the type
    """
    # Handle type as list (e.g., ["string", "null"])
    if isinstance(schema_type, list):
        # Use the first non-null type
        for t in schema_type:
            if t != "null":
                schema_type = str(t)
                break
        else:
            # All types are null
            return None

    type_defaults: dict[str, Any] = {
        "string": "",
        "number": 0,
        "integer": 0,
        "boolean": False,
        "array": [],
        "object": {},
        "null": None,
    }

    return type_defaults.get(str(schema_type), None)


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

    if isinstance(schema, str):
        LOG.debug("Schema is a string, treating as permissive", path=path, schema=schema)
        return data

    if isinstance(schema, list):
        LOG.debug("Schema is a list, treating as permissive", path=path)
        return data

    schema_type = schema.get("type")

    if schema_type == "object" or "properties" in schema:
        if not isinstance(data, dict):
            LOG.warning(
                "Expected object but got different type, creating empty object",
                path=path,
                data_type=type(data).__name__,
            )
            data = {}

        properties = schema.get("properties", {})
        required_fields = schema.get("required", [])

        for field_name, field_schema in properties.items():
            field_path = f"{path}.{field_name}"

            if field_name not in data:
                if field_name in required_fields:
                    default_value = get_default_value_for_type(field_schema.get("type"))
                    LOG.info(
                        "Filling missing required field with default value",
                        path=field_path,
                        default_value=default_value,
                    )
                    data[field_name] = default_value
                else:
                    LOG.debug("Skipping optional missing field", path=field_path)
                    continue

            if field_name in data:
                data[field_name] = fill_missing_fields(data[field_name], field_schema, field_path)

        return data

    elif schema_type == "array":
        if not isinstance(data, list):
            LOG.warning(
                "Expected array but got different type, creating empty array",
                path=path,
                data_type=type(data).__name__,
            )
            return []

        items_schema = schema.get("items")
        if items_schema:
            validated_items = []
            for idx, item in enumerate(data):
                item_path = f"{path}[{idx}]"
                validated_item = fill_missing_fields(item, items_schema, item_path)
                validated_items.append(validated_item)
            return validated_items

        return data
    else:
        return data


def validate_and_fill_extraction_result(
    extraction_result: dict[str, Any],
    schema: dict[str, Any] | list | str | None,
) -> dict[str, Any]:
    """
    Validate extraction result against schema and fill missing fields with defaults.

    This function handles malformed JSON responses from LLMs by:
    1. Validating the structure against the provided schema
    2. Filling in missing required fields with appropriate default values
    3. Preserving optional fields that are present

    Args:
        extraction_result: The extraction result from the LLM
        schema: The JSON schema that defines the expected structure

    Returns:
        The validated and filled extraction result
    """
    if schema is None:
        LOG.debug("No schema provided, returning extraction result as-is")
        return extraction_result

    LOG.info("Validating and filling extraction result against schema")

    try:
        filled_result = fill_missing_fields(extraction_result, schema)
        LOG.info("Successfully validated and filled extraction result")
        return filled_result
    except Exception as e:
        LOG.error(
            "Failed to validate and fill extraction result",
            error=str(e),
            exc_info=True,
        )
        # Return original result if validation fails
        return extraction_result
