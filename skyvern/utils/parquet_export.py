"""Schema-directed Parquet serialization for workflow extraction records."""

from __future__ import annotations

import io
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

INVALID_VALUE_COUNTS_METADATA_KEY = b"skyvern.invalid_value_counts"


class ParquetExportError(ValueError):
    """The configured schema cannot describe a Parquet table."""


def export_parquet_records(records: Any, data_schema: Mapping[str, Any]) -> bytes:
    """Write a JSON array of object records to a schema-directed Parquet file.

    Missing properties and values incompatible with their declared JSON Schema type
    become null. Incompatible-value counts are written as Parquet metadata without
    preserving raw extracted values.
    """
    item_schema = _record_schema(data_schema)
    properties = _properties(item_schema, path="items")
    warnings: Counter[str] = Counter()

    if not isinstance(records, list):
        raise ParquetExportError("data must resolve to a JSON array of object records")

    malformed_record_count = sum(not isinstance(record, Mapping) for record in records)
    if malformed_record_count:
        warnings["$[]"] = malformed_record_count
    columns = {
        field_name: [_coerce_record_value(record, field_name, field_schema, warnings) for record in records]
        for field_name, field_schema in properties.items()
    }
    fields = [
        pa.field(name, _arrow_type(field_schema, path=name), nullable=True) for name, field_schema in properties.items()
    ]
    metadata = {INVALID_VALUE_COUNTS_METADATA_KEY: json.dumps(dict(sorted(warnings.items()))).encode("utf-8")}
    table = pa.Table.from_arrays(
        [pa.array(columns[field.name], type=field.type) for field in fields],
        schema=pa.schema(fields, metadata=metadata),
    )
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


def _record_schema(data_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    if _schema_type(data_schema, path="data_schema") != "array":
        raise ParquetExportError("data_schema must be a JSON Schema array")
    items = data_schema.get("items")
    if not isinstance(items, Mapping) or _schema_type(items, path="data_schema.items") != "object":
        raise ParquetExportError("data_schema.items must be a JSON Schema object")
    return items


def _properties(schema: Mapping[str, Any], *, path: str) -> dict[str, Mapping[str, Any]]:
    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, Mapping):
        raise ParquetExportError(f"{path}.properties must be an object")
    properties: dict[str, Mapping[str, Any]] = {}
    for name, field_schema in raw_properties.items():
        if not isinstance(name, str) or not isinstance(field_schema, Mapping):
            raise ParquetExportError(f"{path}.properties must map field names to JSON Schema objects")
        properties[name] = field_schema
    if not properties:
        raise ParquetExportError(f"{path}.properties must declare at least one property")
    return properties


def _schema_type(schema: Mapping[str, Any], *, path: str) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        return raw_type
    if isinstance(raw_type, Sequence) and not isinstance(raw_type, str):
        types = [value for value in raw_type if value != "null"]
        if len(types) == 1 and isinstance(types[0], str):
            return types[0]
    raise ParquetExportError(f"{path} must declare exactly one non-null JSON Schema type")


def _arrow_type(schema: Mapping[str, Any], *, path: str) -> pa.DataType:
    schema_type = _schema_type(schema, path=path)
    if schema_type == "string":
        if schema.get("format") == "date":
            return pa.date32()
        if schema.get("format") == "date-time":
            return pa.timestamp("us", tz="UTC")
        return pa.string()
    if schema_type == "integer":
        return pa.int64()
    if schema_type == "number":
        return pa.float64()
    if schema_type == "boolean":
        return pa.bool_()
    if schema_type == "object":
        return pa.struct(
            [
                pa.field(name, _arrow_type(field_schema, path=f"{path}.{name}"), nullable=True)
                for name, field_schema in _properties(schema, path=path).items()
            ]
        )
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise ParquetExportError(f"{path}.items must be a JSON Schema object")
        return pa.list_(_arrow_type(items, path=f"{path}[]"))
    raise ParquetExportError(f"{path} has unsupported JSON Schema type {schema_type!r}")


def _coerce_record_value(
    record: Any,
    field_name: str,
    field_schema: Mapping[str, Any],
    warnings: Counter[str],
) -> Any:
    if not isinstance(record, Mapping):
        return None
    if field_name not in record:
        return None
    return _coerce_value(record[field_name], field_schema, path=field_name, warnings=warnings)


def _coerce_value(value: Any, schema: Mapping[str, Any], *, path: str, warnings: Counter[str]) -> Any:
    if value is None:
        return None

    schema_type = _schema_type(schema, path=path)
    if schema_type == "string":
        if not isinstance(value, str):
            return _invalid(path, warnings)
        if schema.get("format") == "date":
            try:
                return date.fromisoformat(value)
            except ValueError:
                return _invalid(path, warnings)
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except (OverflowError, ValueError):
                return _invalid(path, warnings)
        return value
    if schema_type == "integer":
        if isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63:
            return value
        return _invalid(path, warnings)
    if schema_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except OverflowError:
                return _invalid(path, warnings)
            if math.isfinite(number):
                return number
        return _invalid(path, warnings)
    if schema_type == "boolean":
        return value if isinstance(value, bool) else _invalid(path, warnings)
    if schema_type == "object":
        if not isinstance(value, Mapping):
            return _invalid(path, warnings)
        return {
            field_name: _coerce_value(value[field_name], field_schema, path=f"{path}.{field_name}", warnings=warnings)
            if field_name in value
            else None
            for field_name, field_schema in _properties(schema, path=path).items()
        }
    if schema_type == "array":
        if not isinstance(value, list):
            return _invalid(path, warnings)
        item_schema = schema.get("items")
        if not isinstance(item_schema, Mapping):
            raise ParquetExportError(f"{path}.items must be a JSON Schema object")
        return [_coerce_value(item, item_schema, path=f"{path}[]", warnings=warnings) for item in value]
    raise ParquetExportError(f"{path} has unsupported JSON Schema type {schema_type!r}")


def _invalid(path: str, warnings: Counter[str]) -> None:
    warnings[path] += 1
    return None
