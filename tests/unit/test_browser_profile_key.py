from __future__ import annotations

from hashlib import sha256

import pytest
from jinja2 import TemplateSyntaxError

from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
    render_browser_profile_key,
    validate_browser_profile_key,
)


def test_template_browser_profile_key_reads_parameter_value() -> None:
    assert render_browser_profile_key("{{ credential_id }}", {"credential_id": "cred_123"}) == "cred_123"


def test_plain_browser_profile_key_is_literal_text() -> None:
    assert render_browser_profile_key("credential_id", {"credential_id": "cred_123"}) == "credential_id"


def test_template_browser_profile_key_returns_none_when_missing_or_empty() -> None:
    assert render_browser_profile_key("{{ credential_id }}", {}) is None
    assert render_browser_profile_key("{{ credential_id }}", {"credential_id": "   "}) is None


def test_jinja_browser_profile_key_renders_from_parameters() -> None:
    assert (
        render_browser_profile_key("{{ region }}:{{ credential_id }}", {"region": "us", "credential_id": "cred_123"})
        == "us:cred_123"
    )


def test_validate_browser_profile_key_rejects_invalid_jinja() -> None:
    with pytest.raises(TemplateSyntaxError):
        validate_browser_profile_key("{{ credential_id")


def test_segmented_storage_key_uses_stable_hashed_suffix() -> None:
    digest = sha256(b"cred_123").hexdigest()[:24]
    assert build_workflow_browser_session_storage_key("wpid_test", "cred_123") == f"wpid_test/profile_segments/{digest}"
    assert build_browser_profile_key_digest("cred_123") == digest


def test_empty_storage_key_keeps_legacy_workflow_key() -> None:
    assert build_workflow_browser_session_storage_key("wpid_test", None) == "wpid_test"
    assert build_browser_profile_key_digest(None) == ""
