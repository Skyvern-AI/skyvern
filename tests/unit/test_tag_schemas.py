"""Schema-level validation tests for ``skyvern/schemas/tags.py``.

Covers the list-shaped (key-optional) tag apply request — standalone labels,
grouped labels, and the delete-target identity rules — plus construction paths
that JSON-over-HTTP can't easily exercise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skyvern.schemas.tags import TagApplyRequest, TagKeyUpdate


def test_standalone_label_parses_with_null_key() -> None:
    parsed = TagApplyRequest.model_validate({"tags": [{"value": "production"}]})
    assert len(parsed.tags) == 1
    assert parsed.tags[0].key is None
    assert parsed.tags[0].value == "production"


def test_grouped_label_parses() -> None:
    parsed = TagApplyRequest.model_validate({"tags": [{"key": "env", "value": "prod"}]})
    assert parsed.tags[0].key == "env"
    assert parsed.tags[0].value == "prod"


def test_blank_key_normalizes_to_standalone() -> None:
    parsed = TagApplyRequest.model_validate({"tags": [{"key": "   ", "value": "prod"}]})
    assert parsed.tags[0].key is None


def test_tags_must_be_a_list_not_dict() -> None:
    # The old dict shape must fail cleanly, not coerce.
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": {"env": "prod"}})


def test_tag_missing_value_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"key": "env"}]})


def test_tag_non_string_value_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"key": "env", "value": 1}]})


def test_tag_reserved_namespace_key_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"key": "skyvern.managed", "value": "v"}]})


def test_tag_comma_value_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"value": "a,b"}]})


def test_standalone_label_colon_value_rejected() -> None:
    # A group-less label can't contain ':' (it would be unfilterable as a
    # value-only term, which parses ':' as group:label).
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"value": "release:2026"}]})


def test_grouped_value_may_contain_colon() -> None:
    # With a group, ':' in the value is fine — the key disambiguates.
    parsed = TagApplyRequest.model_validate({"tags": [{"key": "url", "value": "http://x:8000"}]})
    assert parsed.tags[0].value == "http://x:8000"


def test_grouped_star_value_rejected() -> None:
    # `key:*` is the group filter wildcard, so a literal '*' value would be
    # unfilterable exactly.
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"key": "env", "value": "*"}]})


def test_standalone_star_value_allowed() -> None:
    # A bare `*` term (no colon) is a value-only filter, so a standalone '*'
    # label is still filterable and therefore allowed.
    parsed = TagApplyRequest.model_validate({"tags": [{"value": "*"}]})
    assert parsed.tags[0].key is None
    assert parsed.tags[0].value == "*"


def test_tags_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags": [{"value": f"v{i}"} for i in range(21)]})


def test_delete_target_by_key() -> None:
    parsed = TagApplyRequest.model_validate({"tags_to_delete": [{"key": "env"}]})
    assert parsed.tags_to_delete[0].key == "env"
    assert parsed.tags_to_delete[0].value is None


def test_delete_target_by_value() -> None:
    parsed = TagApplyRequest.model_validate({"tags_to_delete": [{"value": "production"}]})
    assert parsed.tags_to_delete[0].key is None
    assert parsed.tags_to_delete[0].value == "production"


def test_delete_target_requires_key_or_value() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": [{}]})


def test_delete_target_rejects_both_key_and_value() -> None:
    # Ambiguous: a grouped delete uses key, a standalone delete uses value.
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": [{"key": "env", "value": "prod"}]})


def test_delete_target_reserved_namespace_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": [{"key": "skyvern.managed"}]})


def test_tags_to_delete_must_be_a_list() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": "env"})


def test_tags_to_delete_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        TagApplyRequest.model_validate({"tags_to_delete": [{"value": f"v{i}"} for i in range(21)]})


def test_tag_key_update_rejects_non_string_description() -> None:
    with pytest.raises(ValidationError):
        TagKeyUpdate.model_validate({"description": 123})


def test_tag_key_update_accepts_none() -> None:
    parsed = TagKeyUpdate.model_validate({"description": None})
    assert parsed.description is None
