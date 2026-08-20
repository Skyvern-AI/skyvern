"""Unit tests for schema_validator helpers."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.api.llm.schema_validator import extraction_shape_matches


@pytest.mark.parametrize(
    "value, schema, expected",
    [
        # object schemas — explicit, nullable root, and inferred-from-properties (no explicit type)
        ({"a": 1}, {"type": "object", "properties": {"a": {"type": "string"}}}, True),
        ({"a": 1}, {"type": ["object", "null"], "properties": {"a": {"type": "string"}}}, True),
        ({"a": 1}, {"properties": {"a": {"type": "string"}}}, True),
        ("just a string", {"type": "object", "properties": {"a": {"type": "string"}}}, False),
        ([1, 2], {"type": "object", "properties": {"a": {"type": "string"}}}, False),
        # array schemas — explicit and nullable root
        ([1, 2], {"type": "array", "items": {"type": "number"}}, True),
        ([1, 2], {"type": ["array", "null"], "items": {"type": "number"}}, True),
        ({"a": 1}, {"type": "array", "items": {"type": "number"}}, False),
        # `items` alone does NOT imply array (mirrors fill_missing_fields, which only fills on type=="array")
        ([1, 2], {"items": {"type": "number"}}, False),
        # permissive / non-dict schemas: nothing to fill, so never a match
        ({"a": 1}, "any", False),
        ({"a": 1}, ["object", "null"], False),
        ({"a": 1}, None, False),
    ],
)
def test_extraction_shape_matches(value: object, schema: object, expected: bool) -> None:
    assert extraction_shape_matches(value, schema) is expected
