import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from time import time as current_time
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from freezegun import freeze_time

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.core.security import create_access_token
from skyvern.forge.sdk.routes.routers import legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken, OrganizationAuthTokenType
from skyvern.forge.sdk.services import org_auth_service, org_auth_token_service
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

    def fake_warning(_message: str, **kwargs: object) -> None:
        logged.update(kwargs)

    monkeypatch.setattr(org_auth_service.LOG, "warning", fake_warning)

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


def _make_org(organization_id: str, name: str = "test-org") -> Organization:
    now = datetime.utcnow()
    return Organization(
        organization_id=organization_id,
        organization_name=name,
        created_at=now,
        modified_at=now,
    )


def _make_auth_token(token: str, *, valid: bool) -> OrganizationAuthToken:
    now = datetime.utcnow()
    return OrganizationAuthToken(
        id="oat_test",
        organization_id="org-a",
        token_type=OrganizationAuthTokenType.api,
        token=token,
        valid=valid,
        created_at=now,
        modified_at=now,
    )


class _RotatingAuthOrganizations:
    def __init__(self, api_keys: dict[str, bool]) -> None:
        self.api_keys = api_keys
        self.organization = _make_org("org-a")
        self.validate_calls = 0

    async def get_organization(self, organization_id: str) -> Organization | None:
        return self.organization if organization_id == self.organization.organization_id else None

    async def validate_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
        valid: bool | None = True,
    ) -> OrganizationAuthToken | None:
        self.validate_calls += 1
        assert organization_id == "org-a"
        assert token_type == OrganizationAuthTokenType.api
        assert valid is None
        token_valid = self.api_keys.get(token)
        return _make_auth_token(token, valid=token_valid) if token_valid is not None else None

    def rotate_api_key(self, old_api_key: str, replacement_api_key: str) -> None:
        self.api_keys[old_api_key] = False
        self.api_keys[replacement_api_key] = True


class _AuthDatabase:
    def __init__(self, organizations: _RotatingAuthOrganizations) -> None:
        self.organizations = organizations


@pytest.mark.asyncio
async def test_get_current_org_cached_rejects_rotated_key_after_invalidation() -> None:
    old_api_key = create_access_token("org-a", expires_delta=timedelta(hours=1))
    replacement_api_key = create_access_token("org-a", expires_delta=timedelta(hours=2))
    organizations = _RotatingAuthOrganizations({old_api_key: True})
    db = _AuthDatabase(organizations)

    original_organization = await org_auth_service.get_current_org_cached(old_api_key, db)
    organizations.rotate_api_key(old_api_key, replacement_api_key)

    # Control: this establishes the positive cache is active rather than a guard
    # that always revalidates. Rotation alone does not invalidate this process.
    cached_organization = await org_auth_service.get_current_org_cached(old_api_key, db)
    assert cached_organization.organization_id == original_organization.organization_id == "org-a"
    assert organizations.validate_calls == 1

    org_auth_service.invalidate_cached_org("org-a")

    replacement_organization = await org_auth_service.get_current_org_cached(replacement_api_key, db)
    assert original_organization.organization_id == replacement_organization.organization_id == "org-a"

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(old_api_key, db)

    assert exc_info.value.status_code == 403
    assert organizations.validate_calls == 3


@pytest.mark.asyncio
async def test_get_current_org_cached_coalesces_concurrent_cache_misses(monkeypatch) -> None:
    cache = org_auth_service._current_org_cache
    cache.clear()
    organization = _make_org("org-a")
    validation_started = asyncio.Event()
    release_validation = asyncio.Event()
    resolve_calls = 0

    async def resolve_api_key(_api_key: str, _db: object) -> SimpleNamespace:
        nonlocal resolve_calls
        resolve_calls += 1
        validation_started.set()
        await release_validation.wait()
        return SimpleNamespace(organization=organization)

    monkeypatch.setattr(org_auth_service, "resolve_org_from_api_key", resolve_api_key)
    db = object()
    first_request = asyncio.create_task(org_auth_service.get_current_org_cached("api-key", db))
    await validation_started.wait()
    second_request = asyncio.create_task(org_auth_service.get_current_org_cached("api-key", db))

    try:
        await asyncio.sleep(0)
        assert resolve_calls == 1
    finally:
        release_validation.set()
        results = await asyncio.gather(first_request, second_request)
        cache.clear()

    assert results == [organization, organization]


@pytest.mark.asyncio
async def test_get_current_org_cached_does_not_recache_after_invalidation_during_validation(monkeypatch) -> None:
    cache = org_auth_service._current_org_cache
    cache.clear()
    organization = _make_org("org-a")
    validation_started = asyncio.Event()
    release_validation = asyncio.Event()
    resolve_calls = 0

    async def resolve_api_key(_api_key: str, _db: object) -> SimpleNamespace:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            validation_started.set()
            await release_validation.wait()
        return SimpleNamespace(organization=organization)

    monkeypatch.setattr(org_auth_service, "resolve_org_from_api_key", resolve_api_key)
    db = object()
    in_flight_request = asyncio.create_task(org_auth_service.get_current_org_cached("api-key", db))
    await validation_started.wait()
    org_auth_service.invalidate_cached_org("org-a")
    release_validation.set()
    await in_flight_request

    await org_auth_service.get_current_org_cached("api-key", db)

    assert resolve_calls == 2
    cache.clear()


def test_invalidate_cached_org_drops_only_matching_entries() -> None:
    cache = org_auth_service._current_org_cache
    cache.clear()
    org_a = _make_org("org-a")
    org_b = _make_org("org-b")
    cache[("api-key-a", "db")] = org_a
    cache[("api-key-a-rotated", "db")] = org_a
    cache[("api-key-b", "db")] = org_b

    org_auth_service.invalidate_cached_org("org-a")

    assert ("api-key-a", "db") not in cache
    assert ("api-key-a-rotated", "db") not in cache
    assert ("api-key-b", "db") in cache
    cache.clear()


def test_invalidate_cached_org_is_noop_when_id_absent() -> None:
    cache = org_auth_service._current_org_cache
    cache.clear()
    org = _make_org("org-a")
    cache[("api-key", "db")] = org

    org_auth_service.invalidate_cached_org("never-seen")

    assert ("api-key", "db") in cache
    cache.clear()


def test_invalidate_cached_org_handles_empty_cache() -> None:
    cache = org_auth_service._current_org_cache
    cache.clear()

    # Should not raise.
    org_auth_service.invalidate_cached_org("anything")


class _FakeOrganizationsRepository:
    def __init__(self, organization: Organization) -> None:
        self.organization = organization
        self.tokens: dict[tuple[OrganizationAuthTokenType, str], OrganizationAuthToken] = {}
        self.rows: dict[str, OrganizationAuthToken] = {}
        self.create_calls = 0
        self.validation_calls = 0
        self.validation_token_types: list[OrganizationAuthTokenType] = []

    async def get_organization(self, organization_id: str) -> Organization | None:
        if organization_id == self.organization.organization_id:
            return self.organization
        return None

    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
    ) -> OrganizationAuthToken:
        now = datetime.utcnow()
        self.create_calls += 1
        auth_token = OrganizationAuthToken(
            id=f"test-token-id-{self.create_calls}",
            organization_id=organization_id,
            token_type=token_type,
            token=token,
            valid=True,
            created_at=now,
            modified_at=now,
        )
        self.tokens[(token_type, token)] = auth_token
        self.rows[auth_token.id] = auth_token
        return auth_token

    async def get_valid_org_auth_token(
        self,
        organization_id: str,
        token_type: str,
    ) -> OrganizationAuthToken | None:
        return next(
            (
                token
                for token in self.rows.values()
                if token.organization_id == organization_id and token.token_type.value == token_type and token.valid
            ),
            None,
        )

    async def get_valid_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> list[OrganizationAuthToken]:
        return sorted(
            (
                token
                for token in self.rows.values()
                if token.organization_id == organization_id and token.token_type == token_type and token.valid
            ),
            key=lambda token: token.created_at,
            reverse=True,
        )

    async def delete_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token_ids: list[str],
    ) -> None:
        for token_id in token_ids:
            token = self.rows.get(token_id)
            if token is None or token.organization_id != organization_id or token.token_type != token_type:
                continue
            self.rows.pop(token_id)
            if self.tokens.get((token_type, token.token)) is token:
                self.tokens.pop((token_type, token.token))

    async def validate_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
        valid: bool | None = None,
    ) -> OrganizationAuthToken | None:
        self.validation_calls += 1
        self.validation_token_types.append(token_type)
        auth_token = self.tokens.get((token_type, token))
        if auth_token is None or auth_token.organization_id != organization_id:
            return None
        if valid is not None and auth_token.valid is not valid:
            return None
        return auth_token


class _FakeAgentDB:
    def __init__(self, organizations: _FakeOrganizationsRepository) -> None:
        self.organizations = organizations


@pytest.fixture(autouse=True)
def _clear_current_org_cache() -> Iterator[None]:
    org_auth_service._current_org_cache.clear()
    yield
    org_auth_service._current_org_cache.clear()


async def _mint_ui_session_token(
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid: bool = True,
) -> tuple[str, _FakeAgentDB, _FakeOrganizationsRepository]:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-ui-session-secret-canary-value")
    monkeypatch.setattr(settings, "UI_SESSION_TOKEN_TTL_MINUTES", 2)
    monkeypatch.setattr(org_auth_service.app, "AGENT_FUNCTION", AgentFunction())
    organization = _make_org("org-ui-session")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    monkeypatch.setattr(org_auth_token_service.app, "DATABASE", db)

    auth_token, _ = await org_auth_token_service.create_org_ui_session_token(organization.organization_id)
    auth_token.valid = valid
    return auth_token.token, db, repository


async def _mint_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, _FakeAgentDB, _FakeOrganizationsRepository]:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-api-route-secret-canary")
    monkeypatch.setattr(org_auth_service.app, "AGENT_FUNCTION", AgentFunction())
    organization = _make_org("org-api-route")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    token = create_access_token(organization.organization_id, expires_delta=timedelta(hours=1))
    await repository.create_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
        token=token,
    )
    monkeypatch.setattr(org_auth_service.app, "DATABASE", db)
    return token, db, repository


def _organization_routes_client() -> TestClient:
    fastapi_app = FastAPI()
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    return TestClient(fastapi_app)


@pytest.mark.asyncio
async def test_freshly_minted_ui_session_token_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    issued_after = current_time()

    token, db, repository = await _mint_ui_session_token(monkeypatch)
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.SIGNATURE_ALGORITHM],
        options={"verify_exp": False},
    )

    assert payload["token_type"] == OrganizationAuthTokenType.ui_session.value
    assert payload["exp"] == pytest.approx(issued_after + 120, abs=2)
    assert repository.tokens[(OrganizationAuthTokenType.ui_session, token)].valid is True

    organization = await org_auth_service.get_current_org_cached(token, db)

    assert organization.organization_id == "org-ui-session"
    assert repository.validation_token_types == [OrganizationAuthTokenType.ui_session]


@pytest.mark.asyncio
async def test_expired_ui_session_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token, db, repository = await _mint_ui_session_token(monkeypatch)
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.SIGNATURE_ALGORITHM],
        options={"verify_exp": False},
    )
    monkeypatch.setattr(org_auth_service.time, "time", lambda: payload["exp"] + 1)

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(token, db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Auth token is expired"
    assert repository.validation_calls == 0


@pytest.mark.asyncio
async def test_ui_session_expiry_is_checked_before_cached_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    token, db, repository = await _mint_ui_session_token(monkeypatch)
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.SIGNATURE_ALGORITHM],
        options={"verify_exp": False},
    )

    organization = await org_auth_service.get_current_org_cached(token, db)
    validation_calls = repository.validation_calls
    assert organization.organization_id == "org-ui-session"

    monkeypatch.setattr(org_auth_service.time, "time", lambda: payload["exp"] + 1)
    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(token, db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Auth token is expired"
    assert repository.validation_calls == validation_calls


@pytest.mark.asyncio
async def test_expired_api_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-api-token-secret-canary-value")
    organization = _make_org("org-api-token")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    token = create_access_token(organization.organization_id, expires_delta=timedelta(seconds=-1))
    await repository.create_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
        token=token,
    )

    with pytest.raises(HTTPException) as excinfo:
        await org_auth_service.get_current_org_cached(token, db)

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_unexpired_api_token_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-api-token-secret-canary-value")
    organization = _make_org("org-api-token-valid")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    token = create_access_token(organization.organization_id, expires_delta=timedelta(hours=1))
    await repository.create_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
        token=token,
    )

    resolved_organization = await org_auth_service.get_current_org_cached(token, db)

    assert resolved_organization.organization_id == organization.organization_id


@pytest.mark.asyncio
async def test_cached_api_authentication_decodes_once_before_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-cached-api-secret-canary-value")
    organization = _make_org("org-api-token-cached")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    token = create_access_token(organization.organization_id, expires_delta=timedelta(hours=1))
    await repository.create_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
        token=token,
    )
    await org_auth_service.get_current_org_cached(token, db)

    decode = org_auth_service.jwt.decode
    decode_calls = 0

    def count_decode(*args: object, **kwargs: object) -> dict:
        nonlocal decode_calls
        decode_calls += 1
        return decode(*args, **kwargs)

    monkeypatch.setattr(org_auth_service.jwt, "decode", count_decode)

    resolved_organization = await org_auth_service.get_current_org_cached(token, db)

    assert resolved_organization.organization_id == organization.organization_id
    assert decode_calls == 1


@pytest.mark.asyncio
async def test_repeated_ui_session_mints_keep_token_rows_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-bounded-ui-session-secret-canary")
    monkeypatch.setattr(settings, "UI_SESSION_TOKEN_TTL_MINUTES", 2)
    organization = _make_org("org-ui-session-bounded")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    monkeypatch.setattr(org_auth_token_service.app, "DATABASE", db)

    with freeze_time("2030-01-01T00:00:00Z") as frozen_time:
        first_token, _ = await org_auth_token_service.create_org_ui_session_token(organization.organization_id)
        for _ in range(5):
            reused_token, _ = await org_auth_token_service.create_org_ui_session_token(organization.organization_id)
            assert reused_token.token == first_token.token
        assert repository.create_calls == 1

        for _ in range(6):
            frozen_time.tick(delta=timedelta(seconds=61))
            await org_auth_token_service.create_org_ui_session_token(organization.organization_id)

    assert len(repository.rows) <= 2


@pytest.mark.asyncio
async def test_revoking_a_ui_session_token_takes_effect_despite_the_auth_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, db, repository = await _mint_ui_session_token(monkeypatch)

    # Authenticate once so a cached entry would exist for this token.
    organization = await org_auth_service.get_current_org_cached(token, db)
    assert organization.organization_id == "org-ui-session"

    for auth_token in repository.tokens.values():
        auth_token.valid = False

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(token, db)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_revoked_ui_session_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token, db, _ = await _mint_ui_session_token(monkeypatch, valid=False)

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(token, db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid credentials"


@pytest.mark.asyncio
async def test_ui_session_token_is_not_accepted_by_api_only_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    token, db, _ = await _mint_ui_session_token(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.resolve_org_from_api_key(token, db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid credentials"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/"])
async def test_self_hosted_api_key_route_rejects_ui_session_token(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    token, _, _ = await _mint_ui_session_token(monkeypatch)

    with _organization_routes_client() as client:
        response = client.get(
            f"/api/v1/organizations/org-ui-session/apikeys{suffix}",
            headers={"x-api-key": token},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/"])
async def test_api_key_route_accepts_api_token(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    token, _, _ = await _mint_api_token(monkeypatch)

    with _organization_routes_client() as client:
        response = client.get(
            f"/api/v1/organizations/org-api-route/apikeys{suffix}",
            headers={"x-api-key": token},
        )

    assert response.status_code == 200
    assert len(response.json()["api_keys"]) == 1


@pytest.mark.asyncio
async def test_self_hosted_credential_gate_accepts_ui_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token, db, _ = await _mint_ui_session_token(monkeypatch)
    monkeypatch.setattr(org_auth_service.app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(org_auth_service.app, "DATABASE", db)

    organization = await org_auth_service.get_current_org_for_credential_routes(x_api_key=token)

    assert organization.organization_id == "org-ui-session"


@pytest.mark.asyncio
async def test_ordinary_organization_route_accepts_ui_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token, _, _ = await _mint_ui_session_token(monkeypatch)

    with _organization_routes_client() as client:
        response = client.get(
            "/api/v1/organizations/me",
            headers={"x-api-key": token},
        )

    assert response.status_code == 200
    assert response.json()["organization_id"] == "org-ui-session"


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_ui_session_expiration_rejects_non_finite_values(
    monkeypatch: pytest.MonkeyPatch,
    expires_at: float,
) -> None:
    monkeypatch.setattr(
        org_auth_token_service.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": "org-ui-session",
            "token_type": OrganizationAuthTokenType.ui_session.value,
            "exp": expires_at,
        },
    )

    result = org_auth_token_service._ui_session_expiration(
        SimpleNamespace(token="non-finite-exp-token-canary"),
        "org-ui-session",
    )

    assert result is None


def test_ui_session_expiration_accepts_large_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    expires_at = 10**1000
    monkeypatch.setattr(
        org_auth_token_service.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": "org-ui-session",
            "token_type": OrganizationAuthTokenType.ui_session.value,
            "exp": expires_at,
        },
    )

    result = org_auth_token_service._ui_session_expiration(
        SimpleNamespace(token="large-exp-token-canary"),
        "org-ui-session",
    )

    assert result == expires_at


@pytest.mark.asyncio
async def test_ui_session_row_without_signed_type_claim_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "unit-test-missing-claim-secret-canary")
    organization = _make_org("org-ui-session")
    repository = _FakeOrganizationsRepository(organization)
    db = _FakeAgentDB(repository)
    token = create_access_token(organization.organization_id, expires_delta=timedelta(minutes=2))
    await repository.create_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.ui_session,
        token=token,
    )

    with pytest.raises(HTTPException) as exc_info:
        await org_auth_service.get_current_org_cached(token, db)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid credentials"


def test_credential_bearing_routes_use_the_deployment_aware_gate() -> None:
    import inspect
    from typing import Annotated, get_args, get_origin

    from skyvern.forge.sdk.routes import agent_protocol
    from skyvern.forge.sdk.routes import credentials as credentials_routes
    from skyvern.forge.sdk.routes import custom_llms, google_oauth, microsoft_oauth

    # Every module holding credential- or credential-config-bearing routes. Enumerated from the
    # modules rather than listed by name, so a newly added route fails this test instead of silently
    # defaulting to the permissive dependency. Intentional exemptions must be named with a reason.
    # cloud/ modules are covered by tests/cloud/; this file syncs to the OSS repo where they do
    # not exist.
    CREDENTIAL_ROUTE_MODULES = (
        credentials_routes,
        custom_llms,
        google_oauth,
        microsoft_oauth,
    )
    EXEMPT_FROM_CREDENTIAL_GATE: dict[str, str] = {}

    def route_dependencies(handler: object) -> set[object]:
        # Both declaration styles count: `x = Depends(dep)` and `Annotated[T, Depends(dep)]`. Reading
        # only defaults makes every Annotated route invisible, which is how the OAuth modules stayed
        # ungated while this test reported green.
        dependencies: set[object] = set()
        for parameter in inspect.signature(handler).parameters.values():
            if parameter.default is not inspect.Parameter.empty:
                dependencies.add(getattr(parameter.default, "dependency", None))
            if get_origin(parameter.annotation) is Annotated:
                for metadata in get_args(parameter.annotation)[1:]:
                    dependencies.add(getattr(metadata, "dependency", None))
        return dependencies

    def is_org_authed_route(handler: object, module: object) -> bool:
        if not callable(handler) or getattr(handler, "__module__", None) != module.__name__:
            return False
        return bool(
            route_dependencies(handler)
            & {
                org_auth_service.get_current_org,
                org_auth_service.get_current_org_for_credential_routes,
            }
        )

    credential_route_handlers = {
        f"{module.__name__}.{name}": handler
        for module in CREDENTIAL_ROUTE_MODULES
        for name, handler in vars(module).items()
        if is_org_authed_route(handler, module)
    }
    assert credential_route_handlers, "found no credential routes to check — the enumeration broke"

    for name, handler in sorted(credential_route_handlers.items()):
        if name in EXEMPT_FROM_CREDENTIAL_GATE:
            continue
        dependencies = route_dependencies(handler)
        assert org_auth_service.get_current_org_for_credential_routes in dependencies, (
            f"{name} must use the deployment-aware credential gate"
        )
        assert org_auth_service.get_current_org not in dependencies, (
            f"{name} still accepts a ui_session token via the ungated dependency"
        )

    api_key_route_dependencies = {
        getattr(parameter.default, "dependency", None)
        for parameter in inspect.signature(agent_protocol.get_api_keys).parameters.values()
        if parameter.default is not inspect.Parameter.empty
    }
    assert org_auth_service.get_current_org_with_api_token in api_key_route_dependencies
    assert org_auth_service.get_current_org_for_credential_routes not in api_key_route_dependencies

    api_key_resolver_dependencies = {
        getattr(parameter.default, "dependency", None)
        for parameter in inspect.signature(org_auth_service.get_current_org_with_api_token).parameters.values()
        if parameter.default is not inspect.Parameter.empty
    }
    assert org_auth_service.get_current_org_for_credential_routes not in api_key_resolver_dependencies


@pytest.mark.asyncio
async def test_get_current_org_forwards_optional_attribution_header_to_authentication_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = _make_org("org-attribution")
    calls: list[tuple[str, str | None]] = []

    async def authentication_callback(
        token: str,
        attribution_header: str | None = None,
    ) -> Organization:
        calls.append((token, attribution_header))
        return organization

    monkeypatch.setattr(
        org_auth_service,
        "app",
        SimpleNamespace(authentication_function=authentication_callback),
    )

    resolved = await org_auth_service.get_current_org(
        authorization="Bearer clerk-token",
        x_posthog_attribution="encoded-attribution",
    )

    assert resolved == organization
    assert calls == [("clerk-token", "encoded-attribution")]


@pytest.mark.asyncio
async def test_authenticate_helper_requires_attribution_aware_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = _make_org("org-legacy")
    tokens: list[str] = []

    async def authentication_callback(token: str) -> Organization:
        tokens.append(token)
        return organization

    monkeypatch.setattr(
        org_auth_service,
        "app",
        SimpleNamespace(authentication_function=authentication_callback),
    )

    with pytest.raises(TypeError, match="positional argument"):
        await org_auth_service.authenticate_helper(
            "Bearer clerk-token",
            attribution_header="encoded-attribution",
        )

    assert tokens == []
