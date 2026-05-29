"""Schema-level validation tests for ``skyvern/schemas/tags.py``.

Covers Python-construction paths that JSON-over-HTTP can't exercise. For
example, JSON object keys are always strings on the wire (json.dumps coerces
int keys to ``"1"``), so the non-string-KEY test has to call
``model_validate`` directly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skyvern.schemas.tags import TagApplyRequest, TagKeyUpdate


def test_tag_apply_request_rejects_non_string_dict_key() -> None:
    """COMP-1: this path can only be constructed in Python — JSON coerces
    int keys to strings before serialization."""
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": {1: "prod"}})


def test_tag_apply_request_rejects_non_string_dict_value() -> None:
    """Same guard at the value side, also exercised by the endpoint test
    (this assertion is the unit-level equivalent)."""
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": {"env": 1}})


def test_tag_apply_request_rejects_string_tags_to_delete() -> None:
    """Without the explicit list/tuple check, a string body would silently
    iterate char-by-char."""
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": "env"})


def test_tag_apply_request_rejects_reserved_namespace_in_deletes() -> None:
    """``skyvern.*`` is reserved on SET; the same boundary must hold on the
    body DELETE path."""
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": ["skyvern.managed"]})


def test_tag_apply_request_caps_tags_to_delete() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": [f"k{i}" for i in range(21)]})


def test_tag_key_update_rejects_non_string_description() -> None:
    with pytest.raises(ValidationError):
        TagKeyUpdate.model_validate({"description": 123})


def test_tag_key_update_accepts_none() -> None:
    parsed = TagKeyUpdate.model_validate({"description": None})
    assert parsed.description is None
