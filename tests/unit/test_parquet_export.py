from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skyvern.utils.parquet_export import ParquetExportError, export_parquet_records

_RECORD_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "label": {"type": "string"},
            "price": {"type": "number"},
            "active": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "meta": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "rank": {"type": "integer"},
                },
            },
        },
    },
}


def test_parquet_round_trips_declared_types_and_ragged_records() -> None:
    payload = export_parquet_records(
        [
            {
                "id": 1,
                "label": "first",
                "price": 3.5,
                "active": True,
                "tags": ["kept"],
                "meta": {"source": "page", "rank": 2},
            },
            {"id": 2, "tags": []},
            {
                "id": "not-an-integer",
                "label": 3,
                "price": "not-a-number",
                "active": "yes",
                "tags": ["kept", 7],
                "meta": {"source": 9},
            },
        ],
        _RECORD_SCHEMA,
    )

    table = pq.read_table(pa.BufferReader(payload))

    assert str(table.schema.field("id").type) == "int64"
    assert str(table.schema.field("label").type) == "string"
    assert str(table.schema.field("price").type) == "double"
    assert str(table.schema.field("active").type) == "bool"
    assert str(table.schema.field("tags").type) == "list<element: string>"
    assert str(table.schema.field("meta").type) == "struct<source: string, rank: int64>"
    assert table.to_pylist() == [
        {
            "id": 1,
            "label": "first",
            "price": 3.5,
            "active": True,
            "tags": ["kept"],
            "meta": {"source": "page", "rank": 2},
        },
        {
            "id": 2,
            "label": None,
            "price": None,
            "active": None,
            "tags": [],
            "meta": None,
        },
        {
            "id": None,
            "label": None,
            "price": None,
            "active": None,
            "tags": ["kept", None],
            "meta": {"source": None, "rank": None},
        },
    ]
    assert json.loads(table.schema.metadata[b"skyvern.invalid_value_counts"]) == {
        "active": 1,
        "id": 1,
        "label": 1,
        "meta.source": 1,
        "price": 1,
        "tags[]": 1,
    }


def test_empty_export_keeps_the_declared_parquet_schema() -> None:
    payload = export_parquet_records([], _RECORD_SCHEMA)

    table = pq.read_table(pa.BufferReader(payload))

    assert table.num_rows == 0
    assert table.schema.names == ["id", "label", "price", "active", "tags", "meta"]


def test_parquet_counts_each_malformed_record_once() -> None:
    payload = export_parquet_records([None, "not-a-record"], _RECORD_SCHEMA)

    table = pq.read_table(pa.BufferReader(payload))

    assert table.to_pylist() == [
        {"id": None, "label": None, "price": None, "active": None, "tags": None, "meta": None},
        {"id": None, "label": None, "price": None, "active": None, "tags": None, "meta": None},
    ]
    assert json.loads(table.schema.metadata[b"skyvern.invalid_value_counts"]) == {"$[]": 2}


def test_parquet_coerces_overflowing_numbers_and_datetimes_to_null() -> None:
    payload = export_parquet_records(
        [{"number": 10**400, "timestamp": "9999-12-31T23:59:59-23:59"}],
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "number"},
                    "timestamp": {"type": "string", "format": "date-time"},
                },
            },
        },
    )

    table = pq.read_table(pa.BufferReader(payload))

    assert table.to_pylist() == [{"number": None, "timestamp": None}]
    assert json.loads(table.schema.metadata[b"skyvern.invalid_value_counts"]) == {
        "number": 1,
        "timestamp": 1,
    }


def test_parquet_rejects_schemas_without_columns() -> None:
    with pytest.raises(ParquetExportError, match="must declare at least one property"):
        export_parquet_records(
            [{}],
            {"type": "array", "items": {"type": "object", "properties": {}}},
        )


@pytest.mark.parametrize(
    "data_schema",
    [
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"meta": {"type": "object", "properties": {}}},
            },
        },
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"meta": {"type": "array", "items": {"type": "object", "properties": {}}}},
            },
        },
    ],
)
def test_parquet_rejects_nested_empty_object_schemas(data_schema: dict[str, object]) -> None:
    with pytest.raises(ParquetExportError, match="must declare at least one property"):
        export_parquet_records([{"meta": {}}], data_schema)
