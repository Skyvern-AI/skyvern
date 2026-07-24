from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from fastapi import BackgroundTasks

from skyvern.forge.sdk.routes import credentials


def _setup_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    browser_profile_id: str | None,
    has_refs: bool = False,
    reap_raises: bool = False,
    db_raises: bool = False,
    engine_enabled: bool = True,
) -> SimpleNamespace:
    get_credential = AsyncMock(
        return_value=SimpleNamespace(
            vault_type=credentials.CredentialVaultType.BITWARDEN,
            item_id="item_1",
            organization_id="o_test",
            browser_profile_id=browser_profile_id,
        )
    )
    has_live = AsyncMock(return_value=has_refs)
    db_delete_profile = AsyncMock(side_effect=RuntimeError("db down") if db_raises else None)
    # reap_raises fails the S3 erasure (the security-critical path), which must be best-effort.
    storage_delete_profile = AsyncMock(side_effect=RuntimeError("boom") if reap_raises else None)
    vault_service = SimpleNamespace(delete_credential=AsyncMock(), post_delete_credential_item=AsyncMock())

    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            credentials=SimpleNamespace(get_credential=get_credential),
            browser_sessions=SimpleNamespace(
                has_live_browser_profile_references=has_live,
                delete_browser_profile=db_delete_profile,
            ),
        ),
        STORAGE=SimpleNamespace(delete_browser_profile=storage_delete_profile),
        CREDENTIAL_VAULT_SERVICES={credentials.CredentialVaultType.BITWARDEN: vault_service},
        AGENT_FUNCTION=SimpleNamespace(is_browser_memory_engine_enabled_for_org=AsyncMock(return_value=engine_enabled)),
    )
    monkeypatch.setattr(credentials, "app", fake_app)
    monkeypatch.setattr(credentials, "_clear_cached_totp_code_preview", lambda **kwargs: None)
    return SimpleNamespace(
        has_live=has_live,
        db_delete_profile=db_delete_profile,
        storage_delete_profile=storage_delete_profile,
        vault_service=vault_service,
    )


async def _delete() -> None:
    await credentials.delete_credential(
        background_tasks=BackgroundTasks(),
        credential_id="cred_1",
        current_org=SimpleNamespace(organization_id="o_test"),
    )


@pytest.mark.asyncio
async def test_reaps_profile_when_no_live_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _setup_app(monkeypatch, browser_profile_id="bp_1", has_refs=False)

    await _delete()

    mocks.vault_service.delete_credential.assert_awaited_once()
    mocks.db_delete_profile.assert_awaited_once_with(profile_id="bp_1", organization_id="o_test")
    # hard_delete=True is the security contract: purge all versions so cookies are truly erased.
    mocks.storage_delete_profile.assert_awaited_once_with(organization_id="o_test", profile_id="bp_1", hard_delete=True)


@pytest.mark.asyncio
async def test_skips_reap_when_engine_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-off orgs must not hit the irreversible S3 archive delete on credential delete (v1 has no
    # unlink to recover with). The credential itself is still deleted.
    mocks = _setup_app(monkeypatch, browser_profile_id="bp_1", has_refs=False, engine_enabled=False)

    await _delete()

    mocks.vault_service.delete_credential.assert_awaited_once()
    mocks.has_live.assert_not_awaited()
    mocks.storage_delete_profile.assert_not_awaited()
    mocks.db_delete_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_keeps_profile_when_live_owner_references_it(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _setup_app(monkeypatch, browser_profile_id="bp_1", has_refs=True)

    with structlog.testing.capture_logs() as logs:
        await _delete()

    mocks.db_delete_profile.assert_not_awaited()
    mocks.storage_delete_profile.assert_not_awaited()
    assert any(r.get("event") == "browser_memory.credential_profile_kept" for r in logs)


@pytest.mark.asyncio
async def test_reap_failure_never_fails_the_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _setup_app(monkeypatch, browser_profile_id="bp_1", has_refs=False, reap_raises=True)

    with structlog.testing.capture_logs() as logs:
        # Must not raise even though the S3 erasure blows up.
        await _delete()

    mocks.vault_service.delete_credential.assert_awaited_once()
    events = {r.get("event") for r in logs}
    # Failed erasure must log reap_failed and must NOT falsely claim the profile was reaped.
    assert "browser_memory.credential_profile_reap_failed" in events
    assert "browser_memory.credential_profile_reaped" not in events
    # No silent orphan: the row is NOT soft-deleted when the archive erasure failed, so the profile
    # stays discoverable (row + loud log) for a retry/sweep instead of a gone-row + surviving archive.
    mocks.db_delete_profile.assert_not_awaited()
    # The failure carries a structured error field so it is alertable (retained-archive signature).
    reap_failed = next(r for r in logs if r.get("event") == "browser_memory.credential_profile_reap_failed")
    assert reap_failed.get("error")


@pytest.mark.asyncio
async def test_row_delete_failure_after_erasure_logs_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    # S3 erasure succeeded but the DB row soft-delete failed: log row_delete_failed (not reap_failed)
    # and still log reaped, since the security-critical cookie erasure did happen.
    _setup_app(monkeypatch, browser_profile_id="bp_1", has_refs=False, db_raises=True)

    with structlog.testing.capture_logs() as logs:
        await _delete()

    events = {r.get("event") for r in logs}
    assert "browser_memory.credential_profile_row_delete_failed" in events
    assert "browser_memory.credential_profile_reaped" in events
    assert "browser_memory.credential_profile_reap_failed" not in events


@pytest.mark.asyncio
async def test_no_profile_skips_reap_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _setup_app(monkeypatch, browser_profile_id=None)

    await _delete()

    mocks.has_live.assert_not_awaited()
    mocks.storage_delete_profile.assert_not_awaited()
