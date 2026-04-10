from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from skyvern.config import settings
from skyvern.forge.sdk.core.security import create_access_token
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.org_auth_service import (
    _get_api_key_debug_fields,
    _normalize_api_key_with_flags,
)


def test_normalize_api_key_strips_whitespace() -> None:
    raw_api_key = "  token.value.parts  \n"
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_had_whitespace_padding"] is True
    assert debug_fields["api_key_was_normalized"] is True


def test_normalize_api_key_strips_outer_quotes() -> None:
    raw_api_key = '"token.value.parts"'
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_had_outer_quotes"] is True
    assert debug_fields["api_key_was_normalized"] is True


def test_normalize_api_key_strips_bearer_prefix() -> None:
    raw_api_key = "Bearer token.value.parts"
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_had_bearer_prefix"] is True
    assert debug_fields["api_key_normalized_segment_count"] == 3


def test_normalize_api_key_handles_quoted_bearer_value() -> None:
    raw_api_key = '"Bearer token.value.parts"'
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_had_bearer_prefix"] is True
    assert debug_fields["api_key_had_outer_quotes"] is True


def test_normalize_api_key_tracks_whitespace_removed_after_wrapper_stripping() -> None:
    raw_api_key = 'Bearer " token.value.parts "'
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_had_whitespace_padding"] is True
    assert debug_fields["api_key_had_bearer_prefix"] is True
    assert debug_fields["api_key_had_outer_quotes"] is True


def test_debug_fields_report_no_shadow_decode_for_unchanged_value() -> None:
    raw_api_key = "token.value.parts"
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "token.value.parts"
    assert debug_fields["api_key_was_normalized"] is False
    assert debug_fields["normalized_api_key_decodes"] is None
    assert debug_fields["normalized_api_key_would_be_expired"] is None
    assert debug_fields["normalized_api_key_error_type"] is None


def test_debug_fields_show_when_normalized_token_would_decode(monkeypatch) -> None:
    token = create_access_token("o_test")
    monkeypatch.setattr(org_auth_service.time, "time", lambda: 0)
    raw_api_key = f"Bearer {token}"
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert debug_fields["api_key_had_bearer_prefix"] is True
    assert debug_fields["normalized_api_key_decodes"] is True
    assert debug_fields["normalized_api_key_would_be_expired"] is False
    assert debug_fields["normalized_api_key_error_type"] is None


def test_debug_fields_show_when_normalized_token_still_fails() -> None:
    raw_api_key = '"Bearer definitely-not-a-jwt"'
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == "definitely-not-a-jwt"
    assert debug_fields["normalized_api_key_decodes"] is False
    assert debug_fields["normalized_api_key_error_type"] == "DecodeError"
    assert debug_fields["normalized_api_key_error_reason"] == "Not enough segments"


def test_normalize_api_key_handles_empty_string() -> None:
    raw_api_key = ""
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == ""
    assert debug_fields["api_key_raw_segment_count"] == 0
    assert debug_fields["normalized_api_key_decodes"] is None


def test_normalize_api_key_handles_single_character() -> None:
    raw_api_key = '"'
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert normalized == '"'
    assert debug_fields["api_key_had_outer_quotes"] is False
    assert debug_fields["normalized_api_key_decodes"] is None


def test_debug_fields_reports_validation_error_for_missing_claims() -> None:
    raw_api_key = f"Bearer {jwt.encode({}, settings.SECRET_KEY, algorithm='HS256')}"
    normalized, flags = _normalize_api_key_with_flags(raw_api_key)
    debug_fields = _get_api_key_debug_fields(raw_api_key, normalized, flags)

    assert debug_fields["normalized_api_key_decodes"] is False
    assert debug_fields["normalized_api_key_error_type"] == "ValidationError"
    assert debug_fields["normalized_api_key_error_reason"] == "2 validation error(s): [('sub',), ('exp',)]"


def test_debug_fields_handles_none_inputs() -> None:
    debug_fields = _get_api_key_debug_fields(None, None, None)

    assert debug_fields["api_key_original_length"] is None
    assert debug_fields["normalized_api_key_decodes"] is None
    assert debug_fields["normalized_api_key_error_type"] is None
    assert debug_fields["normalized_api_key_error_reason"] is None


@pytest.mark.asyncio
async def test_resolve_org_from_api_key_logs_decode_error_reason(monkeypatch) -> None:
    logged: dict[str, object] = {}

    def fake_error(_message: str, **kwargs: object) -> None:
        logged.update(kwargs)

    monkeypatch.setattr(org_auth_service.LOG, "error", fake_error)

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.resolve_org_from_api_key("definitely-not-a-jwt", SimpleNamespace(), ())

    assert exc_info.value.status_code == 403
    assert logged["error_type"] == "DecodeError"
    assert logged["error_reason"] == "Not enough segments"


@pytest.mark.asyncio
async def test_resolve_org_from_api_key_returns_403_when_diagnostic_helper_fails(monkeypatch) -> None:
    warnings: dict[str, object] = {}

    def fake_warning(_message: str, **kwargs: object) -> None:
        warnings.update(kwargs)

    monkeypatch.setattr(org_auth_service.LOG, "warning", fake_warning)

    def fail_helper(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(org_auth_service, "_get_api_key_debug_fields", fail_helper)

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.resolve_org_from_api_key("definitely-not-a-jwt", SimpleNamespace(), ())

    assert exc_info.value.status_code == 403
    assert warnings["diagnostic_error_type"] == "RuntimeError"
